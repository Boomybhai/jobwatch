"""Re-check the jobs jobwatch already notified you about (new-jobs.json, last 30 days)
against the CURRENT filters in config.yaml:

  1. title rules (include / exclude words, field-word gate)
  2. location (Netherlands / Belgium / India), reading the job page if needed
  3. generic titles: does the description ask for a chemical/process or any-engineering background?
  4. fluent Dutch required?            (skip_if_fluent_dutch_required)
  5. too much experience asked?        (max_years_experience)

Nothing is sent and state.json is not touched. Results:
  eligible-old-jobs.html   - open in your browser
  eligible-old-jobs.csv    - every posting with the reason it was kept or dropped

Run from the jobwatch folder:  python scripts\\recheck_old_alerts.py
(or double-click recheck_old_alerts.bat). Takes roughly 1 hour - it waits 2 s between
requests to be polite. You can stop with Ctrl+C; what's done so far is written out.
"""
from __future__ import annotations

import csv
import html
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobwatch.config import load_settings  # noqa: E402
from jobwatch.enrich import enrich  # noqa: E402
from jobwatch.experience import annotate_experience, job_description  # noqa: E402
from jobwatch.fetch import Fetcher  # noqa: E402
from jobwatch.filters import (  # noqa: E402
    description_fit, dutch_requirement, experience_ok, is_favourite, is_relevant,
    location_verdict, resolve_location, title_ok,
)
from jobwatch.models import Job  # noqa: E402

log = logging.getLogger("recheck")


def check(f: Fetcher, j: Job, st) -> str:
    """'' = eligible, otherwise the reason it's dropped."""
    if not title_ok(j, st):
        return "title rules"
    if location_verdict(j, st) == "drop":
        return "location (outside NL/BE/India)"
    page_read = False
    if location_verdict(j, st) == "unknown":
        enrich(f, j)
        page_read = True
        if not is_relevant(j, st):
            return "title/location after reading the page"
        if not resolve_location(j, st):
            return "location (outside NL/BE/India or unknown)"
    if not j.description:
        try:
            j.description = job_description(f, j)
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                return "posting removed"
            if not page_read:
                log.debug("couldn't read %s: %s", j.url, exc)
    j.favourite = is_favourite(j, st)
    if st.only_favourites and not j.favourite:
        if not (st.check_description and st.description_keywords):
            return "generic title"
        j.fit = description_fit(j.description, st)
        if not j.fit:
            return "generic title; description doesn't ask for chemical/process or any-engineering background"
    if st.skip_if_fluent_dutch_required:
        d = dutch_requirement(j.description)
        if d == "required":
            return "asks fluent Dutch"
        if d == "lenient":
            j.dutch = "basic Dutch OK"
    if st.check_experience:
        annotate_experience(f, j)
        if not experience_ok(j, st):
            return f"asks {j.years}+ years"
    return ""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    st = load_settings(ROOT / "config.yaml")
    hist = json.loads((ROOT / "new-jobs.json").read_text(encoding="utf-8"))
    seen, jobs = set(), []
    for x in hist:
        key = (x["company"], x["url"])
        if key in seen:
            continue
        seen.add(key)
        jobs.append((x.get("found", ""), Job(company=x["company"], title=x["title"].replace("⭐ ", "").strip(),
                                            url=x["url"])))
    log.info("%d postings you were notified about. Checking them with the current filters...", len(jobs))

    f = Fetcher(st, allow_render=False)
    rows = []
    try:
        for i, (found, j) in enumerate(jobs, 1):
            try:
                reason = check(f, j, st)
            except Exception as exc:  # noqa: BLE001
                reason = f"couldn't check ({type(exc).__name__})"
            rows.append((found, j, reason))
            if i % 25 == 0 or not reason:
                log.info("[%d/%d] %s %s - %s", i, len(jobs), "KEEP" if not reason else "    ", j.company[:30], j.title[:60])
    except KeyboardInterrupt:
        log.info("Stopped - writing what's done so far (%d of %d).", len(rows), len(jobs))
    finally:
        f.close()

    with open(ROOT / "eligible-old-jobs.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["eligible", "company", "title", "location", "years asked", "why kept", "dropped because",
                    "first notified", "url"])
        for found, j, reason in rows:
            why = "; ".join(x for x in ["title field word" if j.favourite else "",
                                        f"description: {j.fit}" if j.fit else "", j.dutch] if x)
            w.writerow(["yes" if not reason else "no", j.company, j.title, j.location,
                        "" if j.years is None else j.years, why if not reason else "", reason, found, j.url])

    keep = [(found, j) for found, j, reason in rows if not reason]
    keep.sort(key=lambda x: (not x[1].favourite, x[1].company.lower()))
    items = []
    for found, j in keep:
        bits = [j.location, "starter / 0 yrs" if j.years == 0 else (f"asks {j.years}+ yrs" if j.years else ""),
                j.dutch, f"description: {j.fit}" if j.fit else "", f"notified {found}" if found else ""]
        items.append(f"<p><b>{html.escape(j.company)}</b> — <a href='{html.escape(j.url, quote=True)}'>"
                     f"{'⭐ ' if j.favourite else ''}{html.escape(j.title)}</a> "
                     f"<span>{html.escape(' · '.join(b for b in bits if b))}</span></p>")
    from collections import Counter
    reasons = Counter(r for _, _, r in rows if r)
    summary = "".join(f"<li>{n} — {html.escape(r)}</li>" for r, n in reasons.most_common())
    page = ("<!doctype html><meta charset='utf-8'><title>Eligible jobs (re-check)</title>"
            "<style>body{font-family:Arial;max-width:900px;margin:24px auto;padding:0 16px}"
            "p{margin:6px 0}span{color:#777;font-size:13px}</style>"
            f"<h1>{len(keep)} of {len(rows)} earlier alerts pass the current filters</h1>"
            + "".join(items) + f"<h2>Dropped</h2><ul>{summary}</ul>"
            "<p><span>Full list with reasons: eligible-old-jobs.csv</span></p>")
    (ROOT / "eligible-old-jobs.html").write_text(page, encoding="utf-8")
    log.info("Done: %d eligible. Open eligible-old-jobs.html in the jobwatch folder.", len(keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
