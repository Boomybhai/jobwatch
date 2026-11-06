"""Command-line entry point: python -m jobwatch [run|list|test-email|test-phone]."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import notify, wintask
from .app_paths import app_dir, is_frozen, load_env_file
from .adapters import check_company, detect_adapter, live_page_extras
from .discover import invalidate, resolve
from .enrich import enrich
from .config import load_companies, load_settings, passes_priority
from .fetch import Fetcher
from .experience import annotate_experience, job_description
from .filters import description_fit, dutch_requirement, experience_ok, is_favourite, is_relevant, location_verdict, resolve_location
from .models import Job
from .state import State

log = logging.getLogger("jobwatch")


LIVE_PAGE_FIRST_RUN_MAX = 5  # more extras than this on the first live-page check = old jobs, not new ones


def run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    companies = [c for c in load_companies(args.companies) if c.enabled]
    companies = [c for c in companies if passes_priority(c, args.min_priority or settings.min_priority)]
    if args.only:
        wanted = args.only.lower()
        companies = [c for c in companies if wanted in c.name.lower()]
    state = State(args.state)
    run_no = state.bump_run()
    fetcher = Fetcher(settings, allow_render=not args.no_render)

    new_relevant: list[Job] = []
    repeats: list[Job] = []
    failing: list[tuple[str, str]] = []
    try:
        for i, company in enumerate(companies, 1):
            cache = state.cache(company.name)
            source = resolve(fetcher, company, cache, force=args.rediscover)
            result = check_company(fetcher, source, settings.max_jobs_per_api)
            if (result.error or not result.jobs) and source.adapter != "html":
                invalidate(cache)  # feed moved or broke -> look again next run
            if result.error:
                streak = state.record_error(company.name, result.error)
                log.info("[%d/%d] %-45s ERROR (%s) %s", i, len(companies), company.name[:45], result.method, result.error[:80])
                if streak >= 3:
                    failing.append((company.name, result.error[:120]))
                continue
            extra_ids: set = set()
            if source.adapter == "sitemap" and not result.error and company.url.rstrip("/") != source.url.rstrip("/"):
                # sitemaps are often rebuilt only once a day -> also read the live vacancy page
                try:
                    extras = live_page_extras(fetcher, company, result.jobs)
                except Exception as exc:
                    log.debug("%s: live page check failed: %s", company.name, exc)
                    extras = []
                if extras:
                    log.info("    %s: %d job(s) on the live page that the sitemap doesn't list yet",
                             company.name, len(extras))
                    result.jobs = result.jobs + extras
                    if not cache.get("live_page_baselined") and len(extras) > LIVE_PAGE_FIRST_RUN_MAX:
                        extra_ids = {j.id for j in extras}  # first time: record old ones quietly
                cache["live_page_baselined"] = True
            fresh = state.new_jobs(company.name, result.jobs, source=source.url)
            if extra_ids:
                fresh = [j for j in fresh if j.id not in extra_ids]
            relevant = []
            for j in fresh:
                if not is_relevant(j, settings):  # title, and location if the listing gives one
                    continue
                if location_verdict(j, settings) == "unknown":
                    # where is it? read the job page (real title + location + description)
                    enrich(fetcher, j)
                    if not j.description:
                        try:
                            j.description = job_description(fetcher, j)
                        except Exception as exc:
                            log.debug("couldn't read description for %s: %s", j.title, exc)
                    if not is_relevant(j, settings):
                        log.info("    skipped (%s): %s", j.location or "title", j.title)
                        continue
                    if not resolve_location(j, settings):
                        log.info("    skipped (not in NL/BE/India: %s): %s", j.location or "location unknown", j.title)
                        continue
                j.favourite = is_favourite(j, settings)
                if settings.only_favourites and not j.favourite:
                    # Generic title ("Engineer", "Junior ...", "Trainee"): keep it only if the
                    # job description asks for a chemical / process background.
                    if not (settings.check_description and settings.description_keywords):
                        continue
                    if not j.description:
                        try:
                            j.description = job_description(fetcher, j)
                        except Exception as exc:
                            log.debug("couldn't read description for %s: %s", j.title, exc)
                    j.fit = description_fit(j.description, settings)
                    if not j.fit:
                        log.info("    skipped (description doesn't ask for a chemical/process background): %s", j.title)
                        continue
                if (settings.check_experience or settings.skip_if_fluent_dutch_required) and not j.description:
                    try:
                        j.description = job_description(fetcher, j)   # read once, used by both checks
                    except Exception as exc:
                        log.debug("couldn't read description for %s: %s", j.title, exc)
                if settings.skip_if_fluent_dutch_required:
                    dutch = dutch_requirement(j.description)
                    if dutch == "required":
                        log.info("    skipped (asks fluent Dutch): %s", j.title)
                        continue
                    if dutch == "lenient":
                        j.dutch = "basic Dutch OK"
                if settings.check_experience:
                    annotate_experience(fetcher, j)
                    if not experience_ok(j, settings):
                        log.info("    skipped (asks %s+ years): %s", j.years, j.title)
                        continue
                relevant.append(j)
            new_relevant.extend(relevant)
            log.info(
                "[%d/%d] %-45s %3d listed, %2d new, %2d relevant (%s)",
                i, len(companies), company.name[:45], len(result.jobs), len(fresh), len(relevant), result.method,
            )
        new_relevant = _dedupe(new_relevant)
        # jobs shown in earlier alerts within repeat_hours are listed again (marked) in this one
        new_keys = {(j.company, j.id) for j in new_relevant}
        repeats = [j for j in state.shown_within(settings.repeat_hours, exclude=new_keys)
                   if is_relevant(j, settings) and resolve_location(j, settings)]
        state.remember_shown(new_relevant, keep_hours=max(settings.repeat_hours * 2, 24))
    finally:
        fetcher.close()
        if not args.dry_run:
            state.save()

    include_errors = settings.notify_errors_every_runs and run_no % settings.notify_errors_every_runs == 0
    errors = failing if include_errors else []

    new_relevant.sort(key=lambda j: (not j.favourite, j.company.lower()))
    log.info("Done: %d new relevant postings (%d favourites).", len(new_relevant),
             sum(j.favourite for j in new_relevant))
    if new_relevant and not args.dry_run:
        try:
            notify.save_list(new_relevant, Path(args.state).parent)
        except Exception as exc:
            log.warning("Couldn't write new-jobs.html: %s", exc)
    if new_relevant or errors:
        if repeats:
            log.info("Repeating %d posting(s) already shown in the last %d hours.", len(repeats), settings.repeat_hours)
        subject, text, html_body = notify.build_email(new_relevant, errors, repeats=repeats)
        print("\n" + subject + "\n" + text)
        if args.dry_run:
            return 0
        if "email" in args.notify:
            notify.send_email(subject, text, html_body)
        if "desktop" in args.notify and new_relevant:
            notify.desktop_notify(new_relevant, repeats=repeats)
        if "phone" in args.notify and new_relevant:
            notify.phone_notify(new_relevant, repeats=repeats)
    return 0


def _dedupe(jobs: list[Job]) -> list[Job]:
    """One alert per job: the same link listed by two companies (Tata Steel IJmuiden / Technology),
    or the same vacancy in several site languages (lambweston.eu/nl/..., /de/..., /fr/...)."""
    import re as _re
    out, seen = [], set()
    for j in jobs:
        url = _re.sub(r"/[a-z]{2}(?:[-_][a-z]{2})?(?=/)", "", j.url.split("#")[0].rstrip("/").lower(), count=1)
        keys = {("url", url), ("job", j.company.lower(), j.title.lower().strip(), j.location.lower())}
        if keys & seen:
            continue
        seen |= keys
        out.append(j)
    return out


def list_companies(args: argparse.Namespace) -> int:
    companies = load_companies(args.companies)
    on = [c for c in companies if c.enabled]
    print(f"{len(on)} of {len(companies)} companies enabled\n")
    for c in companies:
        how = (c.adapter if c.adapter != "auto" else detect_adapter(c.url)) if c.enabled else "skipped"
        print(f"{'✓' if c.enabled else '✗'} {c.name[:50]:50} {how:15} {c.note or c.url}")
    return 0


def discover_cmd(args: argparse.Namespace) -> int:
    """Show which job feed is used per company (and refresh the cache)."""
    settings = load_settings(args.config)
    companies = [c for c in load_companies(args.companies) if c.enabled]
    if args.only:
        companies = [c for c in companies if args.only.lower() in c.name.lower()]
    state = State(args.state)
    fetcher = Fetcher(settings, allow_render=not args.no_render)
    counts: dict[str, int] = {}
    try:
        for i, c in enumerate(companies, 1):
            src = resolve(fetcher, c, state.cache(c.name), force=True)
            counts[src.adapter] = counts.get(src.adapter, 0) + 1
            print(f"[{i}/{len(companies)}] {c.name[:45]:45} {src.adapter:15} {src.url}", flush=True)
    finally:
        fetcher.close()
        if not args.dry_run:
            state.save()
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    return 0


def test_phone(args: argparse.Namespace) -> int:
    demo = [Job("Example B.V.", "Graduate Process Engineer", "https://example.com/job/1", "Rotterdam")]
    ok = notify.phone_notify(demo, title_prefix="[test] ")
    print("Test notification sent — check the ntfy app on your phone." if ok else "Not sent — see the warning above.")
    return 0 if ok else 1


def test_email(args: argparse.Namespace) -> int:
    demo = [Job("Example B.V.", "Graduate Process Engineer", "https://example.com/job/1", "Rotterdam")]
    subject, text, html_body = notify.build_email(demo)
    ok = notify.send_email("[test] " + subject, text, html_body)
    if "desktop" in args.notify:
        notify.desktop_notify(demo)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    root = app_dir()
    load_env_file(root / "email.env")
    if sys.stdout is None:  # windowless .exe has no console: send output to a log file
        sys.stdout = sys.stderr = open(root / "jobwatch.log", "a", encoding="utf-8", buffering=1)

    raw = sys.argv[1:] if argv is None else argv
    double_clicked = is_frozen() and not raw and sys.stdin is not None and sys.stdin.isatty()

    p = argparse.ArgumentParser(prog="jobwatch", description="Get alerts for new jobs on careers pages.")
    p.add_argument(
        "command", nargs="?", default="run",
        choices=["run", "list", "discover", "test-email", "test-phone", "install-task", "uninstall-task"],
    )
    p.add_argument("--config", default=root / "config.yaml")
    p.add_argument("--companies", default=root / "companies.csv")
    p.add_argument("--state", default=root / "state.json")
    p.add_argument(
        "--notify", default="desktop,phone,email" if is_frozen() else "email",
        help="comma list of: desktop, phone, email, none (default: email; desktop,phone,email for the .exe)",
    )
    p.add_argument("--every-hours", type=int, default=3, help="for install-task (default 3)")
    p.add_argument("--min-priority", default=None, help="Low | Medium | High (overrides config)")
    p.add_argument("--only", default=None, help="only check companies whose name contains this text")
    p.add_argument("--no-render", action="store_true", help="never use a headless browser")
    p.add_argument("--rediscover", action="store_true", help="look for each company's job feed again now")
    p.add_argument("--dry-run", action="store_true", help="print results; don't save state or notify")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(raw)
    args.notify = {x.strip() for x in str(args.notify).split(",")}

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    try:  # so notification titles carry the copy's label (e.g. "jobwatch Belgium")
        load_settings(args.config)
    except Exception:
        pass
    if args.command == "install-task":
        return wintask.install(args.every_hours)
    if args.command == "uninstall-task":
        return wintask.uninstall()

    if double_clicked:
        print("jobwatch: checking careers pages for new jobs. This takes a few minutes...\n")
    code = {"run": run, "list": list_companies, "discover": discover_cmd, "test-email": test_email, "test-phone": test_phone}[args.command](args)
    if double_clicked:
        print("\nFinished. Tip: run 'jobwatch.exe install-task' once to check automatically every 3 hours.")
        try:
            input("Press Enter to close this window.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
