"""Read a job page once to get its real title, location and description.

Most careers sites embed a schema.org JobPosting block (for Google Jobs), which
gives the exact title and city/country. Used for jobs found via a sitemap or
plain HTML, where the listing doesn't tell us the location.
"""
from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .models import Job

log = logging.getLogger(__name__)

COUNTRIES = {
    "NL": "Netherlands", "NLD": "Netherlands", "BE": "Belgium", "BEL": "Belgium", "DE": "Germany",
    "DEU": "Germany", "FR": "France", "FRA": "France", "GB": "United Kingdom", "UK": "United Kingdom",
    "US": "United States", "USA": "United States", "CA": "Canada", "IE": "Ireland", "ES": "Spain",
    "IT": "Italy", "PL": "Poland", "SE": "Sweden", "FI": "Finland", "NO": "Norway", "DK": "Denmark",
    "CH": "Switzerland", "AT": "Austria", "IN": "India", "CN": "China", "SG": "Singapore", "JP": "Japan",
    "KR": "Korea", "AU": "Australia", "BR": "Brazil", "MX": "Mexico", "LU": "Luxembourg",
    "CZ": "Czech Republic", "PT": "Portugal", "SA": "Saudi Arabia", "AE": "United Arab Emirates",
    "QA": "Qatar", "ZA": "South Africa",
}


def _postings(data):
    if isinstance(data, list):
        for d in data:
            yield from _postings(d)
    elif isinstance(data, dict):
        t = data.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield data
        if "@graph" in data:
            yield from _postings(data["@graph"])


def _text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("@value") or "")
    if isinstance(v, list):
        return _text(v[0]) if v else ""
    return str(v or "")


def _location(posting: dict) -> str:
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    out = []
    for loc in locs if isinstance(locs, list) else []:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            out.append(addr)
            continue
        city = _text(addr.get("addressLocality"))
        country = _text(addr.get("addressCountry")).strip()
        country = COUNTRIES.get(country.upper(), country)
        out.append(", ".join(x for x in [city, country] if x))
    if not out and posting.get("jobLocationType") == "TELECOMMUTE":
        req = _text(posting.get("applicantLocationRequirements"))
        out.append(COUNTRIES.get(req.upper(), req) or "Remote")
    return "; ".join(dict.fromkeys(x for x in out if x))


def parse_job_page(html: str) -> dict:
    """{'title','location','description'} from a job page (empty strings when unknown)."""
    soup = BeautifulSoup(html, "lxml")
    found = {"title": "", "location": "", "description": ""}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        for p in _postings(data):
            found["title"] = _text(p.get("title")).strip()
            found["location"] = _location(p)
            desc = _text(p.get("description"))
            found["description"] = BeautifulSoup(desc, "lxml").get_text(" ", strip=True) if desc else ""
            break
        if found["title"]:
            break
    if not found["title"]:
        h1 = soup.find("h1")
        og = soup.find("meta", property="og:title")
        found["title"] = (h1.get_text(" ", strip=True) if h1 else "") or (og.get("content", "") if og else "")
    if not found["description"]:
        for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            t.decompose()
        found["description"] = " ".join(soup.get_text(" ", strip=True).split())
    found["title"] = re.sub(r"\s+", " ", found["title"])[:140]
    return found


def enrich(f: Fetcher, job: Job) -> None:
    """Fill job.location / title / description from the job page (best effort)."""
    try:
        info = parse_job_page(f.get(job.url).text)
    except Exception as exc:
        log.debug("couldn't read %s: %s", job.url, exc)
        return
    if info["title"] and 4 <= len(info["title"]) <= 140:
        job.title = info["title"]
    if info["location"] and not job.location:
        job.location = info["location"]
    if len(info["description"]) >= 400:
        job.description = info["description"]
