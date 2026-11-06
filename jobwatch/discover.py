"""Find the most complete job feed behind a careers page.

Many careers pages only show the first 10-25 jobs and load the rest with
JavaScript, so reading the page itself misses postings. Behind almost every
page there is a better source:

1. A job system with a JSON feed (Workday, Greenhouse, Lever, Recruitee,
   SmartRecruiters, Workable, Personio, Oracle Cloud). Their links are usually somewhere in
   the careers page, e.g. the "Apply" buttons point to myworkdayjobs.com.
2. A job sitemap (sitemap.xml) - published so Google can index every job.
3. Otherwise the page itself, following "next page" links.

The result is cached in state.json and refreshed every couple of weeks.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .models import Company

log = logging.getLogger(__name__)

API_ADAPTERS = ("workday", "greenhouse", "lever", "recruitee", "smartrecruiters", "workable", "personio",
                "oracle", "successfactors", "eightfold", "ashby")

# (adapter, regex) - each regex captures a canonical feed URL in group 1
ATS_PATTERNS = [
    ("workday", re.compile(
        r"(https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday\b)[A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("recruitee", re.compile(r"(https?://(?!careers-analytics\.|app\.|api\.|cdn\.)[a-z0-9-]+\.recruitee\.com)")),
    ("smartrecruiters", re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"https?://(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"https?://apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"(https?://jobs\.ashbyhq\.com/[A-Za-z0-9_.%-]+)")),
    ("personio", re.compile(r"(https?://[a-z0-9-]+\.jobs\.personio\.(?:de|com))")),
    ("eightfold", re.compile(r"(https?://[a-z0-9-]+\.eightfold\.ai/careers\?domain=[A-Za-z0-9.-]+)")),
    ("oracle", re.compile(
        r"(https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com/hcmUI/CandidateExperience/[A-Za-z_-]+/sites/[A-Za-z0-9_]+)")),
]
NOT_A_BOARD = {"embed", "api", "j", "jobs", "v1", "o", "en", "nl", "de", "fr", "careers", "static", "assets",
               "login", "apply", "oneclick-ui", "embed.js", "js"}

JOB_PATH_RE = re.compile(
    r"/(job|jobs|vacature|vacatures|vacancy|vacancies|vacatures-en-stages|stelle|stellen|emploi|offre|offres|"
    r"career|careers|carriere|position|positions|opening|openings|werken-bij|find-your-job|search-jobs|"
    r"jobdetail|job-detail|requisition|posting)s?/",
    re.I,
)
LISTING_TAIL_RE = re.compile(r"^(search|results|all|page|category|categories|location|locations|team|teams|"
                             r"department|departments|alerts?|apply|login|faq|students?|graduates?|"
                             r"internships?|benefits|about|nl|en|de|fr)$", re.I)


def canonical_feed(adapter: str, match: str) -> str:
    if adapter == "greenhouse":
        return f"https://boards.greenhouse.io/{match}"
    if adapter == "lever":
        return f"https://jobs.lever.co/{match}"
    if adapter == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{match}"
    if adapter == "workable":
        return f"https://apply.workable.com/{match}"
    return match.rstrip("/")


def find_ats_links(html: str) -> tuple[str, str] | None:
    """Most frequently referenced job-system feed in a page's HTML (incl. scripts/iframes)."""
    counts: Counter = Counter()
    for adapter, rx in ATS_PATTERNS:
        for m in rx.finditer(html):
            key = m.group(1)
            last = key.rstrip("/").rsplit("/", 1)[-1]
            if adapter not in ("recruitee", "personio", "workday", "oracle", "eightfold", "ashby") and last.lower() in NOT_A_BOARD:
                continue
            counts[(adapter, canonical_feed(adapter, key))] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


# ----------------------------------------------------------------- sitemaps
def is_job_url(url: str) -> bool:
    path = urlsplit(url).path
    m = JOB_PATH_RE.search(path)
    if not m:
        return False
    tail = [s for s in path[m.end():].split("/") if s]
    if not tail:
        return False  # the listing page itself
    last = tail[-1]
    if LISTING_TAIL_RE.match(last) and len(tail) == 1:
        return False
    # a job page has an id (digits) or a descriptive slug
    return bool(re.search(r"\d", "/".join(tail)) or any(s.count("-") + s.count("_") >= 2 for s in tail))


def _sitemap_urls(xml: str) -> tuple[list[str], list[str]]:
    """(child sitemaps, page urls) from a sitemap or sitemap index."""
    soup = BeautifulSoup(xml, "xml")
    children = [loc.get_text(strip=True) for sm in soup.find_all("sitemap") for loc in sm.find_all("loc")]
    pages = [loc.get_text(strip=True) for u in soup.find_all("url") for loc in u.find_all("loc")]
    if not pages:  # RSS job feed instead of a sitemap
        pages = [ln.get_text(strip=True) for it in soup.find_all("item") for ln in it.find_all("link")]
    return children, pages


def sitemap_candidates(f: Fetcher, host_url: str) -> list[str]:
    parts = urlsplit(host_url)
    base = f"{parts.scheme}://{parts.netloc}"
    cands = []
    try:
        robots = f.get(base + "/robots.txt")
        if "sitemap" in robots.text.lower():
            cands += [line.split(":", 1)[1].strip() for line in robots.text.splitlines()
                      if line.lower().startswith("sitemap:")]
    except Exception:
        pass
    cands += [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def collect_sitemap_jobs(f: Fetcher, sitemap_url: str, limit_files: int = 25, strict: bool = False) -> list[str]:
    """Job-page URLs listed in a sitemap (walking a sitemap index, job sitemaps first).

    strict=True: a failure on the top sitemap is raised (shows up as an error) instead of giving 0 jobs.
    """
    queue, done, jobs = [sitemap_url], set(), []
    while queue and len(done) < limit_files:
        sm = queue.pop(0)
        if sm in done:
            continue
        done.add(sm)
        try:
            xml = f.get(sm).text
        except Exception as exc:
            if strict and sm == sitemap_url:
                raise
            log.debug("sitemap %s: %s", sm, exc)
            continue
        children, pages = _sitemap_urls(xml)
        jobby = [c for c in children if re.search(r"job|vacan|vacat|career|position|stelle|posting", c, re.I)]
        queue = jobby + queue + [c for c in children if c not in jobby]
        jobs += [p for p in pages if is_job_url(p)]
    return list(dict.fromkeys(jobs))


def find_sitemap(f: Fetcher, page_urls: list[str], min_jobs: int = 3) -> tuple[str, int] | None:
    """A sitemap on one of these hosts that lists job pages."""
    tried = set()
    for url in page_urls:
        host = urlsplit(url).netloc
        if not host or host in tried:
            continue
        tried.add(host)
        for sm in sitemap_candidates(f, url):
            jobs = collect_sitemap_jobs(f, sm)
            if len(jobs) >= min_jobs:
                return sm, len(jobs)
    return None


# ------------------------------------------------------------------ discover
def platform_of_page(html: str, url: str) -> tuple[str, str] | None:
    """Job platforms recognisable from the careers page's own HTML."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if "rmkcdn.successfactors.com" in html or "xweb/rmk-jobs-search" in html:
        return "successfactors", base           # SAP SuccessFactors career site
    if "tbcdn.talentbrew.com" in html or "radancy.net" in html:
        return "sitemap", base + "/sitemap.xml"  # Radancy/TalentBrew: every job is in the sitemap
    m = re.search(r"static\.vscdn\.net/images/careers/demo/", html)
    if m and "domain=" in html:                 # Eightfold on the company's own domain
        dm = re.search(r"[?&]domain=([A-Za-z0-9.-]+)", html)
        if dm:
            return "eightfold", f"{base}/careers?domain={dm.group(1)}"
    return None


VACANCY_LINK_RE = re.compile(r"vacature|vacancies|vacancy|job-?search|search-?jobs|open-?positions|openings|"
                             r"/jobs/?$|/jobs\b|werken-?bij|werkenbij|career|carriere|vacatures", re.I)
VACANCY_TEXT_RE = re.compile(r"vacature|vacanc|bekijk.*(vacature|job)|all jobs|alle vacatures|open positions|"
                             r"job openings|search jobs|view jobs|find (a |your )?job|current openings|zoek|"
                             r"werken bij|careers?\b|jobs\b", re.I)


LANG_SEG_RE = re.compile(r"^/([a-z]{2}(?:[-_][a-z]{2})?)(?:/|$)", re.I)


def _lang_of(path: str) -> str | None:
    """'/nl-nl/vacatures' -> 'nl-nl', '/careers' -> None."""
    m = LANG_SEG_RE.match(path or "/")
    return m.group(1).lower() if m else None


# the last path segment of a page that lists vacancies (not a single job): /careers, /nl/vacatures/, /join-us ...
LISTING_PAGE_RE = re.compile(
    r"/(careers?|career-opportunities|vacatures?|vacatures-overzicht|vacanc(?:y|ies)|jobs?|job-?openings|job-?offers|"
    r"job-?listings|openings|open-?positions|positions|werken-?bij[^/]*|join-?us|join|hiring|work-with-us|"
    r"working-at[^/]*|carri[eè]res?|karriere|stellenangebote)/?$", re.I)
WERKENBIJ_HOST_RE = re.compile(r"^(www\.)?(werkenbij|werken-bij|careers?\.|jobs\.)", re.I)


def vacancy_link(html: str, url: str) -> str | None:
    """Best 'see all vacancies' link on a homepage/landing page, on the same site or a jobs subdomain."""
    soup = BeautifulSoup(html, "lxml")
    here = urlsplit(url)
    root = ".".join(here.netloc.split(".")[-2:])
    best, best_score = None, 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https") or href.rstrip("/") == url.rstrip("/"):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())[:80]
        score = 0
        if VACANCY_LINK_RE.search(parts.netloc + parts.path):
            score += 2
        if VACANCY_TEXT_RE.search(text):
            score += 2
        if score == 0:
            continue
        same_site = parts.netloc.endswith(root)
        if not same_site and not any(rx.search(href) for _, rx in ATS_PATTERNS):
            continue  # don't wander off to LinkedIn, news sites, ...
        if same_site and _lang_of(parts.path) and _lang_of(parts.path) != _lang_of(here.path) \
                and _lang_of(here.path) is not None:
            continue  # the same page in another language (/fr/carrieres when we're on /en/careers)
        if re.search(r"vacature|vacanc|jobs?\b|openings|positions", text + parts.path, re.I):
            score += 1
        if score > best_score:
            best, best_score = href, score
    return best


def discover(f: Fetcher, company: Company, _depth: int = 0) -> tuple[str, str]:
    """Return (adapter, url) for the best feed behind company.url."""
    from .adapters import JOBLINK_RE, detect_adapter, parse_html_jobs  # local import avoids a cycle

    if company.adapter != "auto":
        return company.adapter, company.url
    known = detect_adapter(company.url)
    if known != "html":
        return known, company.url

    try:
        resp = f.get(company.url)
        html, final = resp.text, resp.url
    except Exception:
        if not f.allow_render:
            raise
        html = f.render(company.url)   # blocked for plain requests (403, SSL, ...) -> real browser
        final = getattr(f, "last_render_url", None) or company.url
    hit = platform_of_page(html, final) or find_ats_links(html)
    if not hit and f.allow_render:
        try:  # job-system links are often only in the JavaScript-built page
            html = f.render(company.url)
            final = getattr(f, "last_render_url", None) or final
            hit = platform_of_page(html, final) or find_ats_links(html)
        except Exception as exc:
            log.debug("%s: render failed during discovery: %s", company.name, exc)
    if hit:
        return hit

    # hosts worth checking for a job sitemap: the careers page and where its job links point
    soup = BeautifulSoup(html, "lxml")
    hosts = Counter()
    for a in soup.find_all("a", href=True):
        absolute = urljoin(final, a["href"])
        if JOBLINK_RE.search(urlsplit(absolute).path):
            hosts[urlsplit(absolute).scheme + "://" + urlsplit(absolute).netloc] += 1
    candidates = [final] + [h for h, _ in hosts.most_common(2)]
    on_page = len(parse_html_jobs(html, final, company.name))
    sm = find_sitemap(f, candidates, min_jobs=max(3, on_page))
    if sm:  # only when the sitemap lists at least as many jobs as the page shows
        return "sitemap", sm[0]
    if on_page == 0 and _depth == 0:
        # a homepage or "working here" page: follow its "vacancies" link once
        nxt = vacancy_link(html, final)
        if nxt:
            import dataclasses
            log.info("    %s: no jobs on %s - following %s", company.name, company.url, nxt)
            adapter, url = discover(f, dataclasses.replace(company, url=nxt), _depth=1)
            if adapter != "html" or url:  # found a job system, a sitemap or a page with jobs
                return adapter, url
            log.info("    %s: no jobs on %s either - staying on %s", company.name, nxt, company.url)
    if on_page == 0 and _depth == 1:
        if LISTING_PAGE_RE.search(urlsplit(final).path or "/") or WERKENBIJ_HOST_RE.search(urlsplit(final).netloc):
            return "html", final  # a real vacancies page that just has no openings right now: watch it
        return "html", ""  # tell the caller this page wasn't better (a single job, a news item, ...)
    return "html", company.url


# -------------------------------------------------------------------- cache
REDISCOVER_DAYS = 14
DISCOVER_VERSION = 4  # bump when discovery learns new tricks, so cached results are refreshed


def resolve(f: Fetcher, company: Company, cache: dict, force: bool = False) -> Company:
    """Company with url/adapter pointing at the best feed, using/refreshing the cache entry."""
    import dataclasses
    from datetime import datetime, timedelta, timezone

    if company.adapter != "auto":  # set by hand in companies.csv -> always use it
        return company
    now = datetime.now(timezone.utc)
    hit = cache.get("discovered")
    fresh = False
    # companies that already produce jobs keep an older-version result; ones that never did look again
    version_ok = hit and (hit.get("v") == DISCOVER_VERSION or (cache.get("seen") and hit.get("v", 0) >= 2))
    if hit and hit.get("from") == company.url and version_ok and not force:
        try:
            fresh = now - datetime.fromisoformat(hit["at"]) < timedelta(days=REDISCOVER_DAYS)
        except Exception:
            fresh = False
    if not fresh:
        try:
            adapter, url = discover(f, company)
        except Exception as exc:
            log.debug("%s: discovery failed: %s", company.name, exc)
            adapter, url = (hit["adapter"], hit["url"]) if hit and hit.get("from") == company.url else ("html", company.url)
        cache["discovered"] = {"from": company.url, "adapter": adapter, "url": url,
                               "at": now.isoformat(timespec="seconds"), "v": DISCOVER_VERSION}
        hit = cache["discovered"]
        if adapter != "html" or url != company.url:
            log.info("    %s: using %s feed %s", company.name, adapter, url)
    return dataclasses.replace(company, adapter=hit["adapter"], url=hit["url"])


def invalidate(cache: dict) -> None:
    """Force rediscovery next run (after errors or an empty result)."""
    if "discovered" in cache:
        cache["discovered"]["at"] = "2000-01-01T00:00:00+00:00"
