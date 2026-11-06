"""Turn a careers page into a list of Job objects.

Specialised adapters talk to the JSON feeds that common applicant-tracking
systems expose (these are the same requests the careers page makes in your
browser). Everything else falls back to reading links out of the HTML.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .models import CheckResult, Company, Job

log = logging.getLogger(__name__)

LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.I)


# --------------------------------------------------------------- detection
def detect_adapter(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if "myworkdayjobs.com" in host:
        return "workday"
    if "greenhouse.io" in host:
        return "greenhouse"
    if host.endswith(".recruitee.com"):
        return "recruitee"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if host == "apply.workable.com":
        return "workable"
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        return "lever"
    if ".jobs.personio." in host:
        return "personio"
    if host.endswith(".oraclecloud.com") and "/candidateexperience/" in urlsplit(url).path.lower():
        return "oracle"
    if host.endswith(".eightfold.ai"):
        return "eightfold"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if urlsplit(url).path.lower().endswith(".xml") and "sitemap" in url.lower():
        return "sitemap"
    return "html"


# ----------------------------------------------------------------- Workday
def workday_endpoint(url: str) -> tuple[str, str, str]:
    """Return (api_url, public_base, site) for a Workday careers URL."""
    parts = urlsplit(url)
    host = parts.netloc
    tenant = host.split(".")[0]
    segs = [s for s in parts.path.split("/") if s and not LOCALE_RE.match(s)]
    if not segs:
        raise ValueError(f"can't find Workday site name in {url}")
    site = segs[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    return api, f"https://{host}/{site}", site


def fetch_workday(f: Fetcher, company: Company, max_jobs: int) -> list[Job]:
    api, public, _ = workday_endpoint(company.url)
    jobs: list[Job] = []
    offset = 0
    total = None
    while offset < max_jobs:
        data = f.post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
        postings = data.get("jobPostings") or []
        if total is None:  # Workday only sends the total on the first page (later pages say 0)
            total = data.get("total") or 0
        for p in postings:
            loc = p.get("locationsText") or ""
            if MULTI_LOC_RE.match(loc.strip()):
                loc = ""  # "3 Locations" tells us nothing -> don't filter on it
            path = p.get("externalPath") or ""
            jobs.append(
                Job(
                    company=company.name,
                    title=(p.get("title") or "").strip(),
                    url=public + path,
                    location=loc,
                    posted=p.get("postedOn") or "",
                    id=path or p.get("title", ""),
                )
            )
        offset += 20
        if not postings or (total and offset >= total):
            break
    return jobs


# -------------------------------------------------------------- Greenhouse
def fetch_greenhouse(f: Fetcher, company: Company) -> list[Job]:
    segs = [s for s in urlsplit(company.url).path.split("/") if s]
    board = segs[0]
    data = f.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    return [
        Job(
            company=company.name,
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted=j.get("updated_at", ""),
            id=str(j.get("id")),
        )
        for j in data.get("jobs", [])
    ]


# --------------------------------------------------------------- Recruitee
def fetch_recruitee(f: Fetcher, company: Company) -> list[Job]:
    host = urlsplit(company.url).netloc
    data = f.get_json(f"https://{host}/api/offers/")
    return [
        Job(
            company=company.name,
            title=o.get("title", ""),
            url=o.get("careers_url") or o.get("url", ""),
            location=", ".join(x for x in [o.get("city"), o.get("country")] if x),
            posted=o.get("published_at", ""),
            id=str(o.get("id")),
        )
        for o in data.get("offers", [])
    ]


# -------------------------------------------------------------------- Ashby
def fetch_ashby(f: Fetcher, company: Company) -> list[Job]:
    """jobs.ashbyhq.com/<board> - the page is built with JavaScript, the public posting API isn't."""
    segs = [s for s in urlsplit(company.url).path.split("/") if s]
    if not segs:
        raise ValueError(f"Ashby URL needs the board name: {company.url}")
    data = f.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{segs[0]}?includeCompensation=false")
    jobs = []
    for p in data.get("jobs", []):
        if p.get("isListed") is False:
            continue
        locs = [p.get("location") or ""] + [(x.get("location") or "") for x in p.get("secondaryLocations") or []]
        addr = ((p.get("address") or {}).get("postalAddress") or {})
        country = addr.get("addressCountry") or ""
        jobs.append(Job(
            company=company.name,
            title=(p.get("title") or "").strip(),
            url=p.get("jobUrl") or f"https://jobs.ashbyhq.com/{segs[0]}/{p.get('id')}",
            location="; ".join(x for x in locs + [country] if x),
            posted=p.get("publishedAt") or "",
            id=str(p.get("id") or p.get("jobUrl")),
        ))
    return jobs


# ---------------------------------------------------------- SmartRecruiters
def fetch_smartrecruiters(f: Fetcher, company: Company) -> list[Job]:
    segs = [s for s in urlsplit(company.url).path.split("/") if s]
    slug = segs[0]
    data = f.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    jobs = []
    for p in data.get("content", []):
        loc = p.get("location") or {}
        jobs.append(
            Job(
                company=company.name,
                title=p.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{slug}/{p.get('id')}",
                location=", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                posted=p.get("releasedDate", ""),
                id=str(p.get("id")),
            )
        )
    return jobs


# ---------------------------------------------------------------- Workable
def fetch_workable(f: Fetcher, company: Company) -> list[Job]:
    segs = [s for s in urlsplit(company.url).path.split("/") if s]
    account = segs[0]
    data = f.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{account}")
    return [
        Job(
            company=company.name,
            title=j.get("title", ""),
            url=j.get("url") or j.get("application_url", ""),
            location=", ".join(x for x in [j.get("city"), j.get("country")] if x),
            id=str(j.get("shortcode") or j.get("url")),
        )
        for j in data.get("jobs", [])
    ]


# ------------------------------------------------------------------- Lever
def fetch_lever(f: Fetcher, company: Company) -> list[Job]:
    parts = urlsplit(company.url)
    slug = [s for s in parts.path.split("/") if s][0]
    api_host = "api.eu.lever.co" if ".eu." in parts.netloc else "api.lever.co"
    data = f.get_json(f"https://{api_host}/v0/postings/{slug}?mode=json")
    jobs = []
    for p in data if isinstance(data, list) else []:
        cat = p.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        jobs.append(Job(
            company=company.name,
            title=(p.get("text") or "").strip(),
            url=p.get("hostedUrl") or "",
            location=", ".join(x for x in locs if x) + (f", {p['country']}" if p.get("country") else ""),
            id=str(p.get("id") or p.get("hostedUrl")),
        ))
    return jobs


# ---------------------------------------------------------------- Personio
def fetch_personio(f: Fetcher, company: Company) -> list[Job]:
    parts = urlsplit(company.url)
    base = f"{parts.scheme or 'https'}://{parts.netloc}"
    soup = BeautifulSoup(f.get(base + "/xml?language=en").text, "xml")
    jobs = []
    for pos in soup.find_all("position"):
        pid = pos.find("id").get_text(strip=True) if pos.find("id") else ""
        title = pos.find("name").get_text(strip=True) if pos.find("name") else ""
        office = pos.find("office").get_text(strip=True) if pos.find("office") else ""
        jobs.append(Job(company=company.name, title=title, url=f"{base}/job/{pid}?language=en",
                        location=office, posted=(pos.find("createdAt").get_text(strip=True) if pos.find("createdAt") else ""),
                        id=pid or title))
    return jobs


# ------------------------------------------------------------ Oracle Cloud
ORACLE_SITE_RE = re.compile(r"/hcmUI/CandidateExperience/([A-Za-z_-]+)/sites/([A-Za-z0-9_]+)", re.I)
ORACLE_PAGE = 25  # Oracle returns at most 25 postings per request


def oracle_site(url: str) -> tuple[str, str, str]:
    """Return (host, language, site number) for an Oracle Cloud careers URL."""
    parts = urlsplit(url)
    m = ORACLE_SITE_RE.search(parts.path)
    if not m:
        raise ValueError(f"can't find Oracle site number in {url}")
    return parts.netloc, m.group(1), m.group(2)


def oracle_job_url(host: str, lang: str, site: str, req_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/{lang}/sites/{site}/job/{req_id}"


def fetch_oracle(f: Fetcher, company: Company, max_jobs: int) -> list[Job]:
    """Oracle Cloud HCM careers sites (McDermott and others) - built by JavaScript,
    so read the same JSON feed the page itself loads."""
    host, lang, site = oracle_site(company.url)
    jobs: list[Job] = []
    offset = 0
    while offset < max_jobs:
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit={ORACLE_PAGE},offset={offset},sortBy=POSTING_DATES_DESC")
        data = f.get_json(api)
        block = (data.get("items") or [{}])[0]
        reqs = block.get("requisitionList") or []
        for r in reqs:
            rid = str(r.get("Id") or "")
            locs = [r.get("PrimaryLocation") or ""]
            locs += [(x or {}).get("Name", "") for x in r.get("secondaryLocations") or []]
            jobs.append(Job(
                company=company.name,
                title=(r.get("Title") or "").strip(),
                url=oracle_job_url(host, lang, site, rid),
                location="; ".join(x for x in locs if x),
                posted=r.get("PostedDate") or "",
                id=rid or (r.get("Title") or ""),
            ))
        offset += ORACLE_PAGE
        if len(reqs) < ORACLE_PAGE or offset >= (block.get("TotalJobsCount") or 0):
            break
    return jobs


# ------------------------------------------------------- SAP SuccessFactors
SF_LOC_TAIL_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def _country_names(loc: str) -> str:
    """'Utrecht, NL' -> 'Utrecht, NL, Netherlands' so the country filters can match."""
    from .enrich import COUNTRIES  # local import keeps module load order simple
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if parts and parts[-1].upper() in COUNTRIES:
        return ", ".join(parts + [COUNTRIES[parts[-1].upper()]])
    return loc


def fetch_successfactors(f: Fetcher, company: Company, max_jobs: int) -> list[Job]:
    """SAP SuccessFactors career sites (jobs.<company>.com with rmkcdn.successfactors.com).

    Their /sitemap.xml is really an RSS feed with every open job: title, link,
    location and the full description - so one request covers the whole site.
    """
    parts = urlsplit(company.url)
    base = f"{parts.scheme or 'https'}://{parts.netloc}"
    # ?employer=DFE keeps only that employer's jobs on a site shared by a group of companies
    employer = ((parse_qs(parts.query).get("employer") or [""])[0]).lower()
    soup, last_exc = None, None
    for path in ("/sitemap.xml", "/sitemal.xml"):
        try:
            cand = BeautifulSoup(f.get(base + path).text, "xml")
        except Exception as exc:
            last_exc = exc
            continue
        if cand.find("item") or any("/job/" in (l.get_text() or "") for l in cand.find_all("loc")[:2000]):
            soup = cand
            break
    if soup is None:
        if last_exc:
            raise last_exc
        raise ValueError(f"no SuccessFactors job feed at {base}/sitemap.xml or /sitemal.xml")
    jobs: list[Job] = []
    if not soup.find("item"):  # plain sitemap: job URLs only
        for loc in soup.find_all("loc"):
            u = loc.get_text(strip=True)
            if "/job/" in u:
                jobs.append(Job(company=company.name, title=title_from_url(u), url=u.split("?")[0], id=u.split("?")[0]))
        return jobs
    for item in soup.find_all("item"):
        link = (item.find("link").get_text(strip=True) if item.find("link") else "")
        if "/job/" not in link:
            continue
        emp = item.find("g:employer") or item.find("employer")
        if employer and employer not in (emp.get_text(" ", strip=True).lower() if emp else ""):
            continue
        title = item.find("title").get_text(" ", strip=True) if item.find("title") else ""
        loc_tag = item.find("g:location") or item.find("location")
        loc = loc_tag.get_text(" ", strip=True) if loc_tag else ""
        m = SF_LOC_TAIL_RE.search(title)
        if m and (not loc or m.group(1).strip() == loc):
            loc = loc or m.group(1).strip()
            title = title[: m.start()].strip()
        desc_tag = item.find("description")
        desc = ""
        if desc_tag:
            desc = BeautifulSoup(BeautifulSoup(desc_tag.get_text(), "lxml").get_text(" "), "lxml").get_text(" ", strip=True)
        gid = item.find("g:id") or item.find("guid")
        pub = item.find("pubDate")
        jobs.append(Job(
            company=company.name,
            title=title,
            url=link.split("?")[0],
            location=_country_names(loc),
            posted=pub.get_text(strip=True) if pub else "",
            id=(gid.get_text(strip=True) if gid else "") or link.split("?")[0],
            description=desc if len(desc) >= 400 else "",
        ))
    return jobs


# ---------------------------------------------------------------- Eightfold
EIGHTFOLD_PAGE = 10  # the search API returns 10 positions per request


def fetch_eightfold(f: Fetcher, company: Company, max_jobs: int) -> list[Job]:
    """Eightfold career sites (<x>.eightfold.ai or a custom domain like careers.teva).

    The careers URL must carry ?domain=<company domain>; add &location=Netherlands
    to only read jobs in one country (much faster for big companies).
    """
    parts = urlsplit(company.url)
    q = parse_qs(parts.query)
    domain = (q.get("domain") or [""])[0]
    if not domain:
        raise ValueError(f"Eightfold URL needs ?domain=...: {company.url}")
    location = (q.get("location") or [""])[0]
    host = f"{parts.scheme or 'https'}://{parts.netloc}"
    jobs: list[Job] = []
    start = 0
    legacy = False
    while start < max_jobs:
        params = {"domain": domain, "start": start, "num": EIGHTFOLD_PAGE, "query": "",
                  "location": location, "sort_by": "timestamp"}
        if not legacy:
            try:
                data = f.get_json(f"{host}/api/pcsx/search?" + urlencode(params))
                block = data.get("data") or {}
            except requests.HTTPError:
                legacy = True  # tenant not migrated yet -> older API
        if legacy:
            block = f.get_json(f"{host}/api/apply/v2/jobs?" + urlencode(params))
        positions = block.get("positions") or []
        for p in positions:
            path = p.get("positionUrl") or p.get("canonicalPositionUrl") or f"/careers/job/{p.get('id')}"
            url = path if path.startswith("http") else f"{host}{path}"
            if "domain=" not in url:
                url += ("&" if "?" in url else "?") + f"domain={domain}"
            locs = p.get("locations") or [p.get("location") or ""]
            jobs.append(Job(
                company=company.name,
                title=(p.get("name") or "").strip(),
                url=url,
                location="; ".join(x for x in locs if x),
                posted=str(p.get("postedTs") or p.get("t_create") or ""),
                id=str(p.get("id") or url),
            ))
        start += len(positions) or EIGHTFOLD_PAGE
        if not positions or start >= (block.get("count") or 0):
            break
    return jobs


# ----------------------------------------------------------------- sitemap
ID_TOKEN_RE = re.compile(r"^(?:[a-z]{0,4}\d[\w]*|[0-9a-f]{8,}|[0-9a-f-]{20,})$", re.I)


def title_from_url(url: str) -> str:
    """'…/jobs/12345/process-engineer-rotterdam-r0123' -> 'Process Engineer Rotterdam'."""
    segs = [s for s in urlsplit(url).path.split("/") if s]
    best = ""
    for seg in segs:
        seg = re.sub(r"\.(html?|aspx|php)$", "", seg, flags=re.I)
        words = [w for w in re.split(r"[-_+]|%20", seg) if w and not ID_TOKEN_RE.match(w)]
        cand = " ".join(words)
        if len(cand) > len(best) and len(words) >= 2:
            best = cand
    if not best and segs:
        best = segs[-1]
    return best[:1].upper() + best[1:]


def fetch_sitemap(f: Fetcher, company: Company) -> list[Job]:
    from .discover import collect_sitemap_jobs  # avoid import cycle
    urls = collect_sitemap_jobs(f, company.url, strict=True)[:5000]
    return [Job(company=company.name, title=title_from_url(u), url=u, id=u) for u in urls]


# -------------------------------------------------------------- plain HTML
JOBLINK_RE = re.compile(
    r"(job|jobs|vacat|vacanc|position|opening|requisition|stelle|/o/|/vacature|careers?[^/]*/[^/?]+/?(?:\?|$)|werken-bij[^/]*/[^/?]+/?(?:\?|$)|join-?us/[^/?]+/?(?:\?|$))",
    re.I,
)
NAV_WORDS = {
    "jobs", "vacancies", "vacatures", "careers", "carrière", "all jobs", "alle vacatures",
    "search jobs", "view all jobs", "job search", "apply", "apply now", "solliciteer",
    "solliciteren", "read more", "lees meer", "meer info", "more", "home", "next", "previous",
    "volgende", "vorige", "login", "sign in", "inloggen", "job alert", "job alerts",
    "open sollicitatie", "open application", "students", "graduates", "werken bij",
}


HOMERUN_JOB_RE = re.compile(r"^/[a-z0-9][a-z0-9-]*[a-z0-9](?:/[a-z]{2})?/?$", re.I)


def _is_homerun(soup, base_url: str) -> bool:
    """Homerun job boards (x.homerun.co or a custom jobs.company.com domain): jobs live at /<slug>[/en]."""
    return urlsplit(base_url).netloc.endswith("homerun.co") or bool(
        soup.find("meta", attrs={"name": "homerun:company_id"}) or soup.find("meta", attrs={"property": "homerun:company_id"}))


def parse_html_jobs(html: str, base_url: str, company: str) -> list[Job]:
    soup = BeautifulSoup(html, "lxml")
    homerun = _is_homerun(soup, base_url)
    base_tag = soup.find("base", href=True)
    if base_tag:  # <base href> changes how relative links resolve
        base_url = urljoin(base_url, base_tag["href"].strip())
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    jobs: dict[str, Job] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        if not text:
            text = (a.get("title") or a.get("aria-label") or "").strip()
        if not (6 <= len(text) <= 140) or text.lower() in NAV_WORDS:
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if absolute.rstrip("/") == base_url.rstrip("/"):
            continue
        parts = urlsplit(absolute)
        homerun_job = homerun and parts.netloc == urlsplit(base_url).netloc and HOMERUN_JOB_RE.match(parts.path)
        if not homerun_job and not JOBLINK_RE.search(parts.path + "?" + parts.query):
            continue
        if homerun_job:  # /operator/en and /operator are the same job
            absolute = f"{parts.scheme}://{parts.netloc}" + re.sub(r"/[a-z]{2}/?$", "", parts.path.rstrip("/"))
        jobs.setdefault(absolute, Job(company=company, title=text, url=absolute, id=absolute))
    return list(jobs.values())


NEXT_WORDS = {"next", "next page", "volgende", "volgende pagina", "suivant", "page suivante",
              "weiter", "nächste", "›", "»", ">", "→"}
MAX_PAGES = 6


def find_next_page(html: str, base_url: str) -> str | None:
    """URL of the next results page on a paginated careers page, if any."""
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"].strip())
    cand = soup.find(["a", "link"], rel=lambda r: r and "next" in (r if isinstance(r, list) else [r]))
    if not cand:
        for a in soup.find_all("a", href=True):
            label = " ".join(a.get_text(" ", strip=True).split()).lower()
            aria = (a.get("aria-label") or a.get("title") or "").lower()
            if label in NEXT_WORDS or aria.startswith(("next", "volgende", "suivant", "go to next")):
                cand = a
                break
    if not cand or not cand.get("href") or cand["href"].startswith(("#", "javascript:")):
        return None
    nxt = urljoin(base_url, cand["href"].strip()).split("#")[0]
    if urlsplit(nxt).netloc != urlsplit(base_url).netloc or nxt.rstrip("/") == base_url.rstrip("/"):
        return None
    return nxt


def fetch_html(f: Fetcher, company: Company) -> tuple[list[Job], str]:
    method = "html"
    jobs: list[Job] = []
    if company.render != "always":
        try:
            resp = f.get(company.url)
        except (requests.HTTPError, requests.exceptions.SSLError, requests.ConnectionError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            blocked = status in (401, 403, 429, 503) or not isinstance(exc, requests.HTTPError)
            if not (blocked and company.render != "never" and f.allow_render):
                raise
            log.info("    %s: plain request failed (%s) - using the browser", company.name, status or type(exc).__name__)
            html = f.render(company.url)
            jobs = parse_html_jobs(html, getattr(f, "last_render_url", None) or company.url, company.name)
            return jobs, "rendered"
        found = {j.url: j for j in parse_html_jobs(resp.text, resp.url, company.name)}
        # follow "next page" links so jobs beyond page 1 aren't missed
        url, html, seen_pages = resp.url, resp.text, {resp.url}
        for _ in range(MAX_PAGES - 1):
            nxt = find_next_page(html, url)
            if not nxt or nxt in seen_pages:
                break
            seen_pages.add(nxt)
            try:
                r = f.get(nxt)
            except Exception as exc:
                log.debug("%s: next page failed: %s", company.name, exc)
                break
            url, html = r.url, r.text
            page_jobs = parse_html_jobs(html, url, company.name)
            if not any(j.url not in found for j in page_jobs):
                break  # same jobs again -> pagination isn't really advancing
            for j in page_jobs:
                found.setdefault(j.url, j)
        jobs = list(found.values())
        if len(seen_pages) > 1:
            method = f"html ({len(seen_pages)} pages)"
    if company.render == "always" or (company.render == "auto" and not jobs and f.allow_render):
        html = f.render(company.url)
        jobs = parse_html_jobs(html, getattr(f, "last_render_url", None) or company.url, company.name)
        method = "rendered"
    return jobs, method


# ------------------------------------------------- live page next to a sitemap
def _job_path_prefix(url: str) -> tuple[str, str]:
    """(host, path up to the first segment with a digit / the last one):
    '/vacature/786/young-professional' -> '/vacature', '/nl/vacatures/process-engineer' -> '/nl/vacatures'."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    keep = []
    for i, seg in enumerate(segs):
        if i == len(segs) - 1 or re.search(r"\d", seg):
            break
        keep.append(seg.lower())
    return parts.netloc.lower().removeprefix("www."), "/" + "/".join(keep)


def live_page_extras(f: Fetcher, company: Company, sitemap_jobs: list[Job]) -> list[Job]:
    """Jobs on the company's own careers page that its sitemap doesn't list (yet).

    Many sites rebuild their sitemap only once a day, so a job posted this morning
    is on the vacancy page but not in the sitemap until tonight. Only links that
    look like the sitemap's job pages (same host + path prefix) are taken, so menu
    and news links on the page don't turn into fake jobs. Plain HTTP only - no browser.
    """
    import dataclasses
    from collections import Counter

    if not sitemap_jobs or not company.url or company.url.lower().endswith(".xml"):
        return []
    counts = Counter(_job_path_prefix(j.url) for j in sitemap_jobs)
    floor = max(2, len(sitemap_jobs) // 5)
    patterns = {p for p, n in counts.items() if n >= floor} or {counts.most_common(1)[0][0]}
    known = {j.url.rstrip("/") for j in sitemap_jobs}
    page_jobs, _ = fetch_html(f, dataclasses.replace(company, render="never"))
    extras = []
    for j in page_jobs:
        if j.url.rstrip("/") in known:
            continue
        if _job_path_prefix(j.url) in patterns:
            extras.append(j)
    return extras


# ---------------------------------------------------------------- dispatch
def check_company(f: Fetcher, company: Company, max_jobs: int = 200) -> CheckResult:
    adapter = company.adapter if company.adapter != "auto" else detect_adapter(company.url)
    try:
        if adapter == "workday":
            return CheckResult(company, fetch_workday(f, company, max_jobs), method=adapter)
        if adapter == "greenhouse":
            return CheckResult(company, fetch_greenhouse(f, company), method=adapter)
        if adapter == "recruitee":
            return CheckResult(company, fetch_recruitee(f, company), method=adapter)
        if adapter == "smartrecruiters":
            return CheckResult(company, fetch_smartrecruiters(f, company), method=adapter)
        if adapter == "workable":
            return CheckResult(company, fetch_workable(f, company), method=adapter)
        if adapter == "lever":
            return CheckResult(company, fetch_lever(f, company), method=adapter)
        if adapter == "personio":
            return CheckResult(company, fetch_personio(f, company), method=adapter)
        if adapter == "oracle":
            return CheckResult(company, fetch_oracle(f, company, max_jobs), method=adapter)
        if adapter == "successfactors":
            return CheckResult(company, fetch_successfactors(f, company, max_jobs), method=adapter)
        if adapter == "eightfold":
            return CheckResult(company, fetch_eightfold(f, company, max_jobs), method=adapter)
        if adapter == "ashby":
            return CheckResult(company, fetch_ashby(f, company), method=adapter)
        if adapter == "sitemap":
            return CheckResult(company, fetch_sitemap(f, company), method=adapter)
        jobs, method = fetch_html(f, company)
        return CheckResult(company, jobs, method=method)
    except Exception as exc:  # one broken site must never stop the run
        log.warning("%s: %s", company.name, exc)
        return CheckResult(company, error=f"{type(exc).__name__}: {exc}", method=adapter)
