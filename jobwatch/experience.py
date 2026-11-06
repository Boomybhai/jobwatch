"""Read a job's description and work out how many years of experience it asks for."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .adapters import detect_adapter, oracle_site, workday_endpoint
from .fetch import Fetcher
from .models import Job

log = logging.getLogger(__name__)

WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
    "nul": 0, "een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5, "zes": 6,
    "zeven": 7, "acht": 8, "negen": 9, "tien": 10,
    # French (Belgium)
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10,
}
NUM = r"(\d{1,2}|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
YRS = r"(?:years?|yrs?|jaar|jaren|ans|années|annees)"
# words allowed between "N years" and "experience"
GAP = r"(?:['’]s?\s+)?(?:\s*\w+[\s,/-]+){0,6}?"
EXP = r"(?:experience|ervaring|werkervaring|work experience|\s*d['’]\s*exp[ée]rience|exp[ée]rience)"

PATTERNS = [
    # "2-4 years of experience", "2 to 5 years' relevant experience", "3 à 5 jaar ervaring"
    re.compile(NUM + r"\s*\+?\s*(?:-|–|—|to|tot|à|a)\s*" + NUM + r"\s*\+?\s*" + YRS + GAP + EXP, re.I),
    # "at least 3 years", "minimum of 5 years", "minimaal 3 jaar"
    re.compile(r"(?:at least|minimum(?: of| de)?|min\.?|minimaal|ten minste|tenminste|au moins)\s+" + NUM + r"\s*\+?\s*" + YRS, re.I),
    # "5+ years of experience", "3 years' experience", "3 jaar werkervaring"
    re.compile(NUM + r"\s*(?:\+|or more|plus|of meer)?\s*" + YRS + GAP + EXP, re.I),
    # "experience: 3+ years", "ervaring van 2 jaar"
    re.compile(EXP + r"\s*(?:of|van|de|:)?\s*(?:at least\s+|minimaal\s+|au moins\s+)?" + NUM + r"\s*\+?\s*" + YRS, re.I),
]
NO_EXPERIENCE = re.compile(
    r"(no (?:prior |previous |work )?experience (?:is )?(?:required|needed|necessary)"
    r"|recent(?:ly)? graduat|fresh graduate|starter|entry[- ]level|graduate (?:programme|program)"
    r"|0\s*(?:-|–|to)\s*\d\s*" + YRS + r"|geen (?:werk)?ervaring (?:vereist|nodig)|jeune dipl[ôo]m|d[ée]butant|aucune exp[ée]rience)",
    re.I,
)


def _n(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else WORDS[token]


def min_years_required(text: str) -> int | None:
    """Smallest number of years the text asks for, or None if it doesn't say.

    "3-5 years" -> 3, "at least 5 years" -> 5, "starter / recent graduate" -> 0.
    When several requirements appear we take the smallest one, so a posting
    isn't dropped because of a "nice to have".
    """
    found: list[int] = []
    for pat in PATTERNS:
        for m in pat.finditer(text):
            nums = [g for g in m.groups() if g]
            try:
                first = _n(nums[0])
            except (KeyError, IndexError):
                continue
            if first <= 25:
                found.append(first)
    if found:
        return min(found)
    if NO_EXPERIENCE.search(text):
        return 0
    return None


# ------------------------------------------------------------- descriptions
def job_description(f: Fetcher, job: Job) -> str:
    url = job.url
    if job.description:
        return job.description
    if detect_adapter(url) == "workday":
        # Workday pages are built by JavaScript; their JSON has the description.
        api, public, site = workday_endpoint(url)
        path = urlsplit(url).path.split(site, 1)[-1]
        data = f.get_json(api.rsplit("/jobs", 1)[0] + path)
        html = (data.get("jobPostingInfo") or {}).get("jobDescription", "")
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    if detect_adapter(url) == "oracle":
        # Oracle Cloud pages are built by JavaScript too; ask its details feed.
        host, _, site = oracle_site(url)
        rid = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        data = f.get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                          f"?expand=all&onlyData=true&finder=ById;Id=%22{rid}%22,siteNumber={site}")
        item = (data.get("items") or [{}])[0]
        html = " ".join(item.get(k) or "" for k in
                        ("ExternalDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr"))
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    resp = f.get(url)
    text = _visible_text(resp.text)
    if len(text) < 400 and f.allow_render:   # probably a JavaScript page
        try:
            text = _visible_text(f.render(url))
        except Exception as exc:
            log.debug("render failed for %s: %s", url, exc)
    return text


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def annotate_experience(f: Fetcher, job: Job) -> None:
    """Fill job.years (None when unknown or the page couldn't be read)."""
    try:
        job.years = min_years_required(job_description(f, job))
    except Exception as exc:
        log.info("Couldn't read description for %s (%s) — keeping it", job.title, exc)
        job.years = None
