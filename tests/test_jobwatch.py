"""Offline tests: no network needed."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jobwatch import notify
from jobwatch.adapters import (
    check_company,
    detect_adapter,
    parse_html_jobs,
    workday_endpoint,
    workday_facets,
)
from jobwatch.cli import main
from jobwatch.config import Settings, load_companies, load_settings
from jobwatch.filters import is_relevant
from jobwatch.models import Company, Job
from jobwatch.state import State

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ config
def test_shipped_config_and_companies_load():
    s = load_settings(ROOT / "config.yaml")
    assert "engineer" in s.include_keywords and "senior" in s.exclude_keywords
    companies = load_companies(ROOT / "companies.csv")
    assert len(companies) >= 150
    assert all(c.url for c in companies if c.enabled)
    # job boards must be switched off
    assert not any("linkedin.com" in c.url for c in companies if c.enabled)


# --------------------------------------------------------------- detection
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://huntsman.wd1.myworkdayjobs.com/Huntsman", "workday"),
        ("https://job-boards.greenhouse.io/avantium", "greenhouse"),
        ("https://resatohydrogentechnology.recruitee.com/", "recruitee"),
        ("https://careers.smartrecruiters.com/aviko", "smartrecruiters"),
        ("https://apply.workable.com/circularise/", "workable"),
        ("https://www.werkenbijtauw.nl/vacatures/", "html"),
    ],
)
def test_detect_adapter(url, expected):
    assert detect_adapter(url) == expected


def test_workday_endpoint_skips_locale():
    api, public, site = workday_endpoint("https://iff.wd5.myworkdayjobs.com/en-US/IFF_Careers")
    assert api == "https://iff.wd5.myworkdayjobs.com/wday/cxs/iff/IFF_Careers/jobs"
    assert public == "https://iff.wd5.myworkdayjobs.com/IFF_Careers"
    assert site == "IFF_Careers"


# -------------------------------------------------------------------- HTML
HTML = """
<html><body>
<nav><a href="/vacatures">Vacatures</a></nav>
<h1>Onze vacatures</h1>
<ul>
 <li><a href="/vacatures/graduate-process-engineer-123">Graduate Process Engineer</a></li>
 <li><a href="https://example.com/job/senior-hse-manager">Senior HSE Manager</a></li>
 <li><a href="/vacatures/operator-5-ploegen">Operator 5-ploegen</a></li>
 <li><a href="/over-ons">Over ons en onze geschiedenis</a></li>
 <li><a href="/vacatures/graduate-process-engineer-123#apply">Apply</a></li>
 <li><a href="mailto:hr@example.com">Mail HR about jobs</a></li>
</ul>
<footer><a href="/jobs/privacy-statement">Privacy statement jobs</a></footer>
</body></html>
"""


def test_parse_html_jobs_finds_postings_only():
    jobs = parse_html_jobs(HTML, "https://example.com/vacatures", "Example")
    titles = sorted(j.title for j in jobs)
    assert titles == ["Graduate Process Engineer", "Operator 5-ploegen", "Senior HSE Manager"]
    assert all(j.url.startswith("https://example.com/") for j in jobs)


# ------------------------------------------------------------------ filter
@pytest.fixture
def settings():
    return load_settings(ROOT / "config.yaml")


@pytest.mark.parametrize(
    "title,location,ok",
    [
        ("Graduate Process Engineer", "", True),
        ("Junior Procestechnoloog", "Gorinchem", True),
        ("Process Safety Engineer", "Koog aan de Zaan, Netherlands", True),
        ("Senior Process Engineer", "", False),
        ("Operator 5-ploegen", "", False),
        ("Internship Chemical Engineering", "", False),
        ("Chemical Engineer", "Houston, TX", False),            # US state
            ("Chemical Engineer", "Ludwigshafen, Germany", False),  # foreign country
            ("Chemical Engineer", "Lyon, France", False),           # foreign country
            ("Chemical Engineer", "Kalundborg, Denmark", False),    # foreign country
            ("Chemical Engineer", "Tilburg Plant", True),           # unknown town -> keep
            ("Chemical Engineer", "Bergen op Zoom, NB", True),      # Dutch province code
        ("Chemical Engineer", "", True),                       # unknown location -> keep
        ("International Sales Manager", "", False),
        ("Finance Business Partner", "", False),               # no include keyword
    ],
)
def test_is_relevant(settings, title, location, ok):
    assert is_relevant(Job("X", title, "u", location=location), settings) is ok


# ------------------------------------------------------------------- state
def test_state_baseline_then_detects_new(tmp_path):
    st = State(tmp_path / "s.json")
    a, b = Job("Co", "A", "u/a"), Job("Co", "B", "u/b")
    assert st.new_jobs("Co", [a]) == []          # first check = baseline, no alerts
    assert st.new_jobs("Co", [a, b]) == [b]      # B is new
    assert st.new_jobs("Co", [a, b]) == []       # nothing new
    st.save()
    st2 = State(tmp_path / "s.json")
    assert st2.new_jobs("Co", [a, b]) == []      # survives reload


def test_state_error_streak(tmp_path):
    st = State(tmp_path / "s.json")
    assert st.record_error("Co", "boom") == 1
    assert st.record_error("Co", "boom") == 2
    st.new_jobs("Co", [])
    assert st.data["companies"]["Co"]["consecutive_errors"] == 0


# ---------------------------------------------------------------- adapters
class FakeFetcher:
    allow_render = False

    def __init__(self, html=None, json_by_url=None, post=None):
        self.html, self.json_by_url, self.post = html, json_by_url or {}, post
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return type("R", (), {"text": self.html, "url": url})()

    def get_json(self, url, **kw):
        self.calls.append(url)
        return self.json_by_url[url]

    def post_json(self, url, payload):
        self.calls.append((url, payload["offset"]))
        return self.post(payload["offset"])

    def render(self, url):
        raise RuntimeError("no browser")

    def close(self):
        pass


def test_workday_paging_and_locations():
    pages = {
        0: {"total": 25, "jobPostings": [
            {"title": f"Engineer {i}", "externalPath": f"/job/NL/E{i}", "locationsText": "Tilburg, Netherlands"}
            for i in range(20)]},
        20: {"total": 25, "jobPostings": [
            {"title": "Chemical Engineer", "externalPath": "/job/x/1", "locationsText": "3 Locations"}]},
    }
    f = FakeFetcher(post=lambda off: pages[off])
    res = check_company(f, Company("IFF", "https://iff.wd5.myworkdayjobs.com/en-US/IFF_Careers"))
    assert not res.error and res.method == "workday"
    assert len(res.jobs) == 21
    assert res.jobs[-1].location == ""            # "3 Locations" blanked
    assert res.jobs[0].url == "https://iff.wd5.myworkdayjobs.com/IFF_Careers/job/NL/E0"


def test_workday_filters_from_link():
    assert workday_facets("https://nxp.wd3.myworkdayjobs.com/careers") == ({}, "")
    assert workday_facets("https://nxp.wd3.myworkdayjobs.com/careers?Location_Country=abc&Location_Country=def&q=process+engineer") == (
        {"Location_Country": ["abc", "def"]}, "process engineer")
    sent = []

    class F(FakeFetcher):
        def post_json(self, url, payload):
            sent.append((url, payload))
            return {"total": 1, "jobPostings": [{"title": "Process Engineer", "externalPath": "/job/Nijmegen/P_R1"}]}

    res = check_company(F(), Company("NXP", "https://nxp.wd3.myworkdayjobs.com/careers?Location_Country=abc"))
    assert not res.error and len(res.jobs) == 1
    assert sent[0][0] == "https://nxp.wd3.myworkdayjobs.com/wday/cxs/nxp/careers/jobs"
    assert sent[0][1]["appliedFacets"] == {"Location_Country": ["abc"]} and sent[0][1]["searchText"] == ""
    assert res.jobs[0].url == "https://nxp.wd3.myworkdayjobs.com/careers/job/Nijmegen/P_R1"


def test_workday_reads_only_wanted_countries():
    from jobwatch.adapters import workday_country_facet
    answer = {"total": 900, "jobPostings": [{"title": "Engineer US", "externalPath": "/job/US/1"}], "facets": [
        {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Engineering", "id": "fam"}]},
        {"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "Netherlands", "id": "nl-id", "count": 40},
                {"descriptor": "Belgium", "id": "be-id", "count": 5},
                {"descriptor": "United States of America", "id": "us-id", "count": 800}]},
            {"facetParameter": "locations", "values": [{"descriptor": "Netherlands", "id": "city-ish"}]}]}]}
    assert workday_country_facet(answer, ["Netherlands", "Belgium"]) == {"locationCountry": ["nl-id", "be-id"]}
    assert workday_country_facet(answer, []) == {}
    assert workday_country_facet({"facets": []}, ["Netherlands"]) == {}

    sent = []

    class F(FakeFetcher):
        settings = Settings(workday_countries=["Netherlands", "Belgium"])

        def post_json(self, url, payload):
            sent.append(payload["appliedFacets"])
            if not payload["appliedFacets"]:
                return answer
            return {"total": 1, "jobPostings": [{"title": "Process Engineer", "externalPath": "/job/Delft/P_1"}]}

    res = check_company(F(), Company("Acme", "https://acme.wd3.myworkdayjobs.com/Ext"), 300)
    assert [j.title for j in res.jobs] == ["Process Engineer"]
    assert sent == [{}, {"locationCountry": ["nl-id", "be-id"]}]

    # no country filter on the site -> everything, as before; a filter in the link wins
    sent.clear()
    plain = {"total": 1, "jobPostings": [{"title": "Chemist", "externalPath": "/job/NL/C_1"}]}
    F.post_json = lambda self, url, payload: sent.append(payload["appliedFacets"]) or plain
    assert [j.title for j in check_company(F(), Company("Acme", "https://acme.wd3.myworkdayjobs.com/Ext")).jobs] == ["Chemist"]
    assert sent == [{}]
    sent.clear()
    check_company(F(), Company("Acme", "https://acme.wd3.myworkdayjobs.com/Ext?Location_Country=x"))
    assert sent == [{"Location_Country": ["x"]}]


DANONE_LIKE = """<html><head><meta property="og:title" content="Project Engineer"></head><body>
<select name="countryISO2C"><option>Select a location</option><option>Brazil</option><option>Netherlands</option></select>
<ul role="listbox"><li role="option">India</li><li role="option">Netherlands</li></ul>
<h1>Manufacturing</h1><h1>Project Engineer</h1>
<ul><li><span class="dn-jobdetails__countries"><img src="x.svg"> {country} </span></li>
<li><span class="dn-jobdetails__procedure-icon--city"> {city} </span></li></ul>
<p>{body}</p><p>No Brasil, recentemente unificamos nossas unidades.</p></body></html>"""


def test_job_page_location_from_detail_markup_not_country_picker():
    from jobwatch.enrich import parse_job_page
    from jobwatch.filters import location_verdict
    body = "We are looking for a Project Engineer to lead improvement projects in the factory. " * 8
    s = Settings(location_keywords=["netherlands", "haps"], foreign_location_keywords=["brazil", "south africa"])
    abroad = parse_job_page(DANONE_LIKE.format(country="South Africa", city="Boksburg", body=body))
    assert abroad["title"] == "Project Engineer"            # the h1 that og:title names, not the job family
    assert abroad["location"] == "Boksburg, South Africa"
    assert "Netherlands" not in abroad["description"]       # the country picker is not job text
    url = "https://careers.danone.com/br/pt/jobs/project-engineer-29198-en-us.html"
    job = Job("Danone", abroad["title"], url, location=abroad["location"], description=abroad["description"])
    assert location_verdict(job, s, use_page=True) == "drop"
    haps = parse_job_page(DANONE_LIKE.format(country="Netherlands", city="HAPS", body=body))
    assert haps["location"] == "HAPS, Netherlands"
    assert location_verdict(Job("Danone", "Project Engineer", url, location=haps["location"]), s) == "keep"
    # a page whose "location" element holds no place keeps location unknown
    plain = parse_job_page('<h1>Engineer</h1><button class="job-location-filter">Location</button>')
    assert plain["location"] == ""
    # Danone lists each job once per country site: keep one link
    from jobwatch.adapters import dedupe_translations
    assert dedupe_translations([url, "https://careers.danone.com/en-global/jobs/project-engineer-29198-en-us.html"]) == [
        "https://careers.danone.com/en-global/jobs/project-engineer-29198-en-us.html"]


def test_sitemap_keeps_one_link_per_translated_job():
    from jobwatch.adapters import dedupe_translations
    urls = [
        "https://www.themembercompany.com/nl/vacatures/supply-chain-engineer-cTEAV",
        "https://www.themembercompany.com/careers/supply-chain-engineer-cTEAV",
        "https://www.themembercompany.com/fr/carrieres/ingenieur-supply-chain-cTEAV",
        "https://www.processionals.nl/vacatures/energie-en-afval/qa-medewerker-a0wqd00000fqaazyah",
        "https://www.processionals.nl/vacatures/qa-medewerker-a0wqd00000fqaazyah",
        "https://x.nl/jobs/Process-Engineer", "https://x.nl/jobs/Chemical-Engineer",  # words, not codes
        "https://x.nl/jobs/operator-2026", "https://x.nl/jobs/engineer-2026",
        "https://y.nl/jobs/engineer-r12345", "https://z.nl/jobs/engineer-r12345",  # other site: own job
    ]
    assert dedupe_translations(urls) == [
        "https://www.themembercompany.com/careers/supply-chain-engineer-cTEAV",
        "https://www.processionals.nl/vacatures/qa-medewerker-a0wqd00000fqaazyah",
        "https://x.nl/jobs/Process-Engineer", "https://x.nl/jobs/Chemical-Engineer",
        "https://x.nl/jobs/operator-2026", "https://x.nl/jobs/engineer-2026",
        "https://y.nl/jobs/engineer-r12345", "https://z.nl/jobs/engineer-r12345",
    ]
    # the same link wins whatever order the sitemap lists them in
    assert dedupe_translations(list(reversed(urls[:3]))) == [urls[1]]


def test_greenhouse_recruitee_smartrecruiters():
    f = FakeFetcher(json_by_url={
        "https://boards-api.greenhouse.io/v1/boards/avantium/jobs":
            {"jobs": [{"id": 1, "title": "Process Engineer", "absolute_url": "https://g/1",
                       "location": {"name": "Amsterdam"}}]},
        "https://x.recruitee.com/api/offers/":
            {"offers": [{"id": 2, "title": "Junior Engineer", "careers_url": "https://r/2",
                         "city": "Assen", "country": "Netherlands"}]},
        "https://api.smartrecruiters.com/v1/companies/aviko/postings?limit=100":
            {"content": [{"id": "3", "name": "Graduate Engineer",
                          "location": {"city": "Steenderen", "country": "nl"}}]},
    })
    g = check_company(f, Company("Avantium", "https://job-boards.greenhouse.io/avantium"))
    r = check_company(f, Company("Resato", "https://x.recruitee.com/"))
    s = check_company(f, Company("Aviko", "https://careers.smartrecruiters.com/aviko"))
    assert [j.title for j in g.jobs + r.jobs + s.jobs] == ["Process Engineer", "Junior Engineer", "Graduate Engineer"]
    assert s.jobs[0].url == "https://jobs.smartrecruiters.com/aviko/3"


def test_one_broken_site_returns_error_not_exception():
    f = FakeFetcher(json_by_url={})
    res = check_company(f, Company("Avantium", "https://job-boards.greenhouse.io/avantium"))
    assert res.error.startswith("KeyError")


# ------------------------------------------------------------------- email
def test_build_email():
    jobs = [Job("B Co", "Process Engineer", "https://b/1", "Delft"), Job("A Co", "Trainee <R&D>", "https://a/1")]
    subject, text, html = notify.build_email(jobs, [("C Co", "HTTPError 404")])
    assert subject == "jobwatch: 2 new postings at 2 companies"
    assert text.index("A Co") < text.index("B Co")
    assert "Trainee &lt;R&amp;D&gt;" in html and "C Co" in html


def test_send_email_without_credentials_is_noop(monkeypatch):
    for k in ["SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]:
        monkeypatch.delenv(k, raising=False)
    assert notify.send_email("s", "t", "h") is False


def test_send_email_uses_smtp(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, **kw):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): sent["user"] = u
        def send_message(self, m): sent["to"] = m["To"]; sent["subject"] = m["Subject"]

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("SMTP_PORT", "")            # empty GitHub secret -> default
    monkeypatch.delenv("EMAIL_TO", raising=False)
    assert notify.send_email("Hello", "t", "<b>h</b>") is True
    assert sent == {"host": "smtp.gmail.com", "port": 465, "user": "me@gmail.com",
                    "to": "me@gmail.com", "subject": "Hello"}


# --------------------------------------------------------------- end to end
def test_full_run_with_mocked_sites(tmp_path, monkeypatch, capsys):
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "name,priority,group,url,enabled,adapter,render,link_type,note\n"
        "Example,High,Chemicals,https://example.com/vacatures,yes,auto,never,Careers page,\n"
        "Broken,High,Chemicals,https://broken.example/jobs,yes,auto,never,Careers page,\n"
        "Off,High,Chemicals,https://www.linkedin.com/jobs/x,no,auto,auto,,job board\n",
        encoding="utf-8",
    )
    pages = {"https://example.com/vacatures": HTML,
             "https://example.com/vacatures/junior-chemical-engineer-9": "<p>Location: Rotterdam, Netherlands.</p>"}

    def fake_get(self, url, **kw):
        if url not in pages:
            raise ConnectionError("unreachable")
        return type("R", (), {"text": pages[url], "url": url})()

    monkeypatch.setattr("jobwatch.fetch.Fetcher.get", fake_get)
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    args = ["--companies", str(companies), "--state", str(tmp_path / "state.json"), "--no-render"]

    assert main(["run", *args]) == 0                 # run 1: baseline, silent
    assert emails == []
    pages["https://example.com/vacatures"] = HTML.replace(
        "</ul>", '<li><a href="/vacatures/junior-chemical-engineer-9">Junior Chemical Engineer</a></li>'
                 '<li><a href="/vacatures/senior-chemist-8">Senior Chemist</a></li></ul>')
    assert main(["run", *args]) == 0                 # run 2: one relevant new job
    assert len(emails) == 1
    subject, text, _ = emails[0]
    assert subject == "⭐ jobwatch: 1 new posting at 1 company (1 favourite)"
    assert "Junior Chemical Engineer" in text and "Senior Chemist" not in text
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["companies"]["Broken"]["consecutive_errors"] == 2
    assert "Off" not in state["companies"]


# ------------------------------------------------------------- .exe helpers
def test_load_env_file(tmp_path, monkeypatch):
    from jobwatch.app_paths import load_env_file

    env = tmp_path / "email.env"
    env.write_text("# comment\nSMTP_USER = me@gmail.com\nSMTP_PASSWORD='abcd efgh'\nEMAIL_TO=\n", encoding="utf-8")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("EMAIL_TO", "keep@me.nl")
    assert sorted(load_env_file(env)) == ["SMTP_PASSWORD", "SMTP_USER"]
    assert os.environ["SMTP_PASSWORD"] == "abcd efgh"
    assert os.environ["EMAIL_TO"] == "keep@me.nl"          # real env var wins
    assert load_env_file(tmp_path / "missing.env") == []


def test_app_dir_is_exe_folder_when_frozen(monkeypatch, tmp_path):
    import sys

    from jobwatch import app_paths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "jobwatch.exe"))
    assert app_paths.app_dir() == tmp_path


def test_phone_notify(monkeypatch):
    sent = []

    class R:
        def raise_for_status(self): pass

    import requests
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append((url, json)) or R())
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notify.phone_notify([Job("Coöperatie X", "Junior Engineer", "https://x/1")]) is False
    monkeypatch.setenv("NTFY_TOPIC", "manan-test")
    jobs = [Job("Coöperatie X", "Junior Engineer", "https://x/1", "Veendam"), Job("ASML", "Graduate", "https://a/1")]
    assert notify.phone_notify(jobs) is True
    assert [p["title"] for _, p in sent] == ["ASML", "Coöperatie X"]   # one push per job
    assert sent[1][0] == "https://ntfy.sh" and sent[1][1]["topic"] == "manan-test"
    assert sent[1][1]["click"] == "https://x/1" and "Junior Engineer (Veendam)" in sent[1][1]["message"]


# -------------------------------------------------------------- experience
from jobwatch.experience import min_years_required  # noqa: E402


@pytest.mark.parametrize(
    "text,years",
    [
        ("You have 2-4 years of experience in process engineering.", 2),
        ("Requirements: 0-2 years experience; MSc Chemical Engineering", 0),
        ("At least 5 years of relevant experience in a chemical plant.", 5),
        ("Minimum of 3 years’ experience in the process industry", 3),
        ("You bring 7+ years of hands-on experience with distillation.", 7),
        ("3 years' experience in GMP manufacturing is a plus", 3),
        ("two to three years of professional experience", 2),
        ("Je hebt minimaal 3 jaar werkervaring als procestechnoloog.", 3),
        ("Je hebt 2 tot 5 jaar relevante ervaring.", 2),
        ("Experience: 4+ years in project engineering", 4),
        ("We are looking for a recent graduate (MSc).", 0),
        ("No prior experience required — full training provided.", 0),
        ("Our company has 150 years of history in Rotterdam.", None),
        ("You enjoy working in a team.", None),
        ("5+ years experience required; 2 years experience with Aspen is a plus", 2),
    ],
)
def test_min_years_required(text, years):
    assert min_years_required(text) == years


def test_experience_filter_and_favourites(settings):
    from jobwatch.filters import experience_ok, is_favourite

    settings.favourite_keywords = ["chemical engineer", "process engineer"]
    settings.max_years_experience = 5
    assert is_favourite(Job("X", "Junior Process Engineer", "u"), settings)
    assert not is_favourite(Job("X", "Project Engineer", "u"), settings)
    assert experience_ok(Job("X", "a", "u", years=5), settings)
    assert not experience_ok(Job("X", "a", "u", years=6), settings)
    assert experience_ok(Job("X", "a", "u", years=None), settings)
    settings.keep_if_experience_unknown = False
    assert not experience_ok(Job("X", "a", "u", years=None), settings)


def test_email_puts_favourites_first():
    jobs = [Job("A Co", "Project Engineer", "https://a/1", years=None),
            Job("Z Co", "Process Engineer", "https://z/1", years=2, favourite=True)]
    subject, text, _ = notify.build_email(jobs)
    assert subject.startswith("⭐") and "1 favourite" in subject
    assert text.index("Z Co") < text.index("A Co")
    assert "⭐ Process Engineer (asks 2+ yrs)" in text


def test_workday_description_endpoint():
    from jobwatch.experience import job_description

    calls = []

    class F:
        allow_render = False
        def get_json(self, url):
            calls.append(url)
            return {"jobPostingInfo": {"jobDescription": "<p>You have <b>3-5 years</b> of experience.</p>"}}

    text = job_description(F(), Job("IFF", "Engineer", "https://iff.wd5.myworkdayjobs.com/IFF_Careers/job/NL/E0"))
    assert calls == ["https://iff.wd5.myworkdayjobs.com/wday/cxs/iff/IFF_Careers/job/NL/E0"]
    assert min_years_required(text) == 3


def test_run_drops_postings_asking_too_much_experience(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "name,priority,group,url,enabled,adapter,render,link_type,note\n"
        "Example,High,Chemicals,https://example.com/vacatures,yes,auto,never,Careers page,\n",
        encoding="utf-8",
    )
    pages = {
        "https://example.com/vacatures": HTML,
        "https://example.com/vacatures/junior-chemical-engineer-9": "<p>0-2 years of experience. Starter welcome. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/process-engineer-7": "<p>You have at least 8 years of experience. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/project-engineer-6": "<p>Nothing about experience here. Rotterdam, Netherlands.</p>",
    }

    def fake_get(self, url, **kw):
        if url not in pages:
            raise ConnectionError("unreachable")
        return type("R", (), {"text": pages[url], "url": url})()

    monkeypatch.setattr("jobwatch.fetch.Fetcher.get", fake_get)
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    # this test is about the experience filter, so let generic titles through
    # (the real config may require a field word via only_favourites)
    cfg = tmp_path / "config.yaml"
    cfg.write_text((ROOT / "config.yaml").read_text(encoding="utf-8")
                   .replace("only_favourites: true", "only_favourites: false"), encoding="utf-8")
    args = ["--config", str(cfg), "--companies", str(companies), "--state", str(tmp_path / "state.json"),
            "--no-render"]
    assert main(["run", *args]) == 0
    pages["https://example.com/vacatures"] = HTML.replace(
        "</ul>",
        '<li><a href="/vacatures/junior-chemical-engineer-9">Junior Chemical Engineer</a></li>'
        '<li><a href="/vacatures/process-engineer-7">Process Engineer</a></li>'
        '<li><a href="/vacatures/project-engineer-6">Project Engineer</a></li></ul>')
    assert main(["run", *args]) == 0
    subject, text, _ = emails[0]
    assert "⭐ Junior Chemical Engineer (Netherlands · starter / 0 yrs)" in text      # favourite, shown first
    assert "Project Engineer" in text                                   # experience not stated -> kept
    assert "\n  - Process Engineer" not in text and "⭐ Process Engineer" not in text  # 8 yrs -> dropped
    assert text.index("Junior Chemical Engineer") < text.index("Project Engineer")


# ------------------------------------------------------------- Belgium additions
def test_french_experience_parsing():
    from jobwatch.experience import min_years_required as m
    assert m("Vous avez au moins 3 ans d’expérience") == 3
    assert m("3 à 5 ans d'expérience en procédés") == 3
    assert m("Profil: jeune diplômé ingénieur") == 0
    assert m("une expérience de 2 ans") == 2


def test_label_in_notification_name(tmp_path, monkeypatch):
    from jobwatch.config import load_settings
    from jobwatch.notify import app_name
    cfg = tmp_path / "config.yaml"
    cfg.write_text("label: Belgium\n", encoding="utf-8")
    load_settings(cfg)
    assert app_name() == "jobwatch Belgium"
    cfg.write_text("min_priority: Low\n", encoding="utf-8")
    load_settings(cfg)
    assert app_name() == "jobwatch"


def test_task_name_follows_folder(monkeypatch, tmp_path):
    from jobwatch import wintask
    monkeypatch.setattr(wintask, "app_dir", lambda: tmp_path / "jobwatch-belgium")
    assert wintask.task_name() == "jobwatch-belgium"
    monkeypatch.setattr(wintask, "app_dir", lambda: tmp_path / "jobwatch")
    assert wintask.task_name() == "jobwatch"
    monkeypatch.setattr(wintask, "app_dir", lambda: tmp_path / "Desktop stuff")
    assert wintask.task_name() == "jobwatch"


def test_phone_notify_each_job_opens_its_own_link(monkeypatch):
    sent = []

    class R:
        def raise_for_status(self): pass

    import requests
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append(json) or R())
    monkeypatch.setenv("NTFY_TOPIC", "t")
    jobs = [Job("Dow", "Process Engineer A", "https://d/a"), Job("Dow", "Process Engineer B", "https://d/b")]
    assert notify.phone_notify(jobs) is True
    assert [p["click"] for p in sent] == ["https://d/a", "https://d/b"]


def test_relative_links_use_base_tag():
    from jobwatch.adapters import parse_html_jobs
    html = '<html><head><base href="https://x.com/careers/"></head><body>' \
           '<a href="vacature/123-process-engineer">Process Engineer Junior</a></body></html>'
    assert [j.url for j in parse_html_jobs(html, "https://x.com/nl/", "X")] == \
        ["https://x.com/careers/vacature/123-process-engineer"]


def test_changed_careers_url_rebaselines(tmp_path):
    st = State(tmp_path / "s.json")
    old = [Job("ASML", "Old", "u/1")]
    assert st.new_jobs("ASML", old, source="https://www.asml.com/find") == []
    new = [Job("ASML", "A", "w/1"), Job("ASML", "B", "w/2")]
    assert st.new_jobs("ASML", new, source="https://asml.wd3.myworkdayjobs.com/ASMLEXT1") == []  # silent switch
    assert st.new_jobs("ASML", new + [Job("ASML", "C", "w/3")],
                       source="https://asml.wd3.myworkdayjobs.com/ASMLEXT1")[0].title == "C"


def test_html_follows_next_page(monkeypatch):
    from jobwatch import adapters
    pages = {
        "https://c.com/jobs": '<a href="/jobs/vacature/1-process-engineer">Process Engineer One</a>'
                              '<a rel="next" href="/jobs?page=2">2</a>',
        "https://c.com/jobs?page=2": '<a href="/jobs/vacature/2-chemical-engineer">Chemical Engineer Two</a>'
                                     '<a href="/jobs?page=3">Volgende</a>',
        "https://c.com/jobs?page=3": '<a href="/jobs/vacature/2-chemical-engineer">Chemical Engineer Two</a>',
    }

    class Resp:
        def __init__(self, url): self.url, self.text = url, pages[url]

    class F:
        allow_render = False
        def get(self, url): return Resp(url)

    jobs, method = adapters.fetch_html(F(), Company(name="C", url="https://c.com/jobs"))
    assert sorted(j.title for j in jobs) == ["Chemical Engineer Two", "Process Engineer One"]
    assert method.startswith("html (")


# ------------------------------------------------------ feed auto-discovery
class SiteFetcher:
    """Fake fetcher serving a dict of url -> text."""
    allow_render = False

    def __init__(self, pages, json_by_url=None):
        self.pages, self.json_by_url, self.calls = pages, json_by_url or {}, []

    def get(self, url, **kw):
        self.calls.append(url)
        if url not in self.pages:
            raise ConnectionError(url)
        return type("R", (), {"text": self.pages[url], "url": url})()

    def get_json(self, url, **kw):
        self.calls.append(url)
        return self.json_by_url[url]

    def render(self, url):
        raise RuntimeError("no browser")


def test_find_ats_links_picks_most_referenced_feed():
    from jobwatch.discover import find_ats_links
    html = ('<a href="https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers/job/Delft/Engineer_R1">x</a>'
            '<a href="https://acme.wd3.myworkdayjobs.com/AcmeCareers/job/Geleen/Chemist_R2">y</a>'
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=other"></script>')
    assert find_ats_links(html) == ("workday", "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers")
    assert find_ats_links('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">') == (
        "greenhouse", "https://boards.greenhouse.io/acme")
    assert find_ats_links('<a href="https://jobs.lever.co/acme/123-abc">') == ("lever", "https://jobs.lever.co/acme")
    assert find_ats_links('<a href="https://acme.jobs.personio.de/job/42">') == ("personio", "https://acme.jobs.personio.de")
    assert find_ats_links("<p>nothing here</p>") is None


def test_discover_falls_back_to_job_sitemap():
    from jobwatch.discover import discover
    from jobwatch.adapters import check_company
    pages = {
        "https://acme.com/careers": '<a href="/careers/jobs/process-engineer-123">Process engineer</a>',
        "https://acme.com/robots.txt": "User-agent: *\nSitemap: https://acme.com/sitemap_index.xml",
        "https://acme.com/sitemap_index.xml":
            '<sitemapindex><sitemap><loc>https://acme.com/sitemap-pages.xml</loc></sitemap>'
            '<sitemap><loc>https://acme.com/sitemap-jobs.xml</loc></sitemap></sitemapindex>',
        "https://acme.com/sitemap-pages.xml": "<urlset><url><loc>https://acme.com/about</loc></url></urlset>",
        "https://acme.com/sitemap-jobs.xml": "<urlset>" + "".join(
            f"<url><loc>https://acme.com/careers/jobs/{t}-{n}</loc></url>"
            for n, t in [(1, "process-engineer"), (2, "junior-chemical-engineer"), (3, "lab-technician"),
                         (4, "plant-operator")]) + "<url><loc>https://acme.com/careers/jobs/</loc></url></urlset>",
    }
    f = SiteFetcher(pages)
    adapter, url = discover(f, Company("Acme", "https://acme.com/careers"))
    assert (adapter, url) == ("sitemap", "https://acme.com/sitemap_index.xml")
    res = check_company(f, Company("Acme", url, adapter=adapter))
    assert res.method == "sitemap" and len(res.jobs) == 4
    assert res.jobs[1].title == "Junior chemical engineer"


def test_title_from_url_strips_ids():
    from jobwatch.adapters import title_from_url
    assert title_from_url("https://x.com/jobs/12345/process-engineer-rotterdam-r0123") == "Process engineer rotterdam"
    assert title_from_url("https://x.com/vacatures/procestechnoloog-geleen.html") == "Procestechnoloog geleen"


def test_lever_and_personio_adapters():
    from jobwatch.adapters import check_company
    f = SiteFetcher({"https://acme.jobs.personio.de/xml?language=en":
                     "<workzag-jobs><position><id>7</id><office>Delft</office><name>Process Engineer</name>"
                     "</position></workzag-jobs>"},
                    {"https://api.lever.co/v0/postings/acme?mode=json":
                     [{"id": "a1", "text": "Chemical Engineer", "hostedUrl": "https://jobs.lever.co/acme/a1",
                       "categories": {"location": "Rotterdam"}, "country": "NL"}]})
    lev = check_company(f, Company("Acme", "https://jobs.lever.co/acme"))
    assert lev.method == "lever" and lev.jobs[0].location == "Rotterdam, NL"
    per = check_company(f, Company("Acme", "https://acme.jobs.personio.de/"))
    assert per.method == "personio" and per.jobs[0].url == "https://acme.jobs.personio.de/job/7?language=en"
    assert per.jobs[0].location == "Delft"


def test_resolve_caches_and_rediscovers():
    from jobwatch import discover as d
    calls = []
    orig = d.discover
    d.discover = lambda f, c: calls.append(c.url) or ("workday", "https://a.wd3.myworkdayjobs.com/Ext")
    try:
        cache = {}
        c = Company("A", "https://a.com/careers")
        assert d.resolve(None, c, cache).adapter == "workday"
        assert d.resolve(None, c, cache).url == "https://a.wd3.myworkdayjobs.com/Ext"
        assert len(calls) == 1                       # cached
        d.invalidate(cache)
        d.resolve(None, c, cache)
        assert len(calls) == 2                       # looked again after invalidate
        d.resolve(None, Company("A", "https://a.com/new-careers"), cache)
        assert len(calls) == 3                       # careers URL changed in companies.csv
    finally:
        d.discover = orig


def test_enrich_reads_jobposting_location():
    from jobwatch.enrich import enrich
    page = ('<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":'
            '"JobPosting","title":"Graduate Process Engineer","description":"<p>' + "Starter role. " * 40 +
            '</p>","jobLocation":{"@type":"Place","address":{"addressLocality":"Kallo","addressCountry":"BE"}}}]}'
            '</script><h1>ignored</h1></html>')
    j = Job("Acme", "Graduate process engineer 123", "https://acme.com/jobs/graduate-process-engineer-123")
    enrich(SiteFetcher({j.url: page}), j)
    assert j.title == "Graduate Process Engineer"
    assert j.location == "Kallo, Belgium"
    assert j.description.startswith("Starter role.")


def test_phone_notify_sends_every_posting_and_saves_list(monkeypatch, tmp_path):
    sent = []

    class R:
        status_code = 200
        def raise_for_status(self): pass

    import requests
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append(json) or R())
    monkeypatch.setattr(notify, "PUSH_GAP", 0)
    monkeypatch.setenv("NTFY_TOPIC", "t")
    jobs = [Job(f"Co{i % 4}", f"Process Engineer {i}", f"https://c/{i}") for i in range(89)]
    assert notify.phone_notify(jobs) is True
    assert len(sent) == 89 and len({p["click"] for p in sent}) == 89
    notify.save_list(jobs, tmp_path)
    page = (tmp_path / "new-jobs.html").read_text(encoding="utf-8")
    assert page.count("<a href=") == 89 and "https://c/88" in page


# ------------------------------------------------------------ Oracle Cloud
ORACLE_SITE = "https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs"


def test_detect_oracle():
    assert detect_adapter(ORACLE_SITE) == "oracle"


def test_oracle_paging_locations_and_links():
    from jobwatch.adapters import ORACLE_PAGE

    def req(i, loc="The Hague, South Holland, Netherlands"):
        return {"Id": str(26000000 + i), "Title": f"Engineer {i}", "PostedDate": "2026-10-12",
                "PrimaryLocation": loc, "secondaryLocations": []}

    class F(FakeFetcher):
        def get_json(self, url, **kw):
            self.calls.append(url)
            off = int(url.split("offset=")[1].split(",")[0])
            reqs = [req(i) for i in range(off, min(off + ORACLE_PAGE, 30))]
            if off == 25:
                reqs[-1] = req(29, "Houston, TX, United States") | {
                    "Title": "Associate Process Engineer",
                    "secondaryLocations": [{"Name": "London, United Kingdom"}]}
            return {"items": [{"TotalJobsCount": 30, "requisitionList": reqs}]}

    f = F()
    res = check_company(f, Company("McDermott", ORACLE_SITE))
    assert not res.error and res.method == "oracle"
    assert len(res.jobs) == 30 and len(f.calls) == 2
    assert "siteNumber=CX_1" in f.calls[0] and "offset=0" in f.calls[0]
    assert res.jobs[0].url == "https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26000000"
    assert res.jobs[0].id == "26000000"
    assert res.jobs[-1].location == "Houston, TX, United States; London, United Kingdom"


def test_oracle_found_from_company_careers_page():
    from jobwatch.discover import discover

    html = ('<a href="https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26003008">'
            'Project Engineer</a>')
    adapter, url = discover(FakeFetcher(html=html), Company("McDermott", "https://www.mcdermott.com/careers/search-apply"))
    assert adapter == "oracle"
    assert url == "https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"


def test_oracle_description_endpoint():
    from jobwatch.experience import job_description

    calls = []

    class F:
        allow_render = False
        def get_json(self, url):
            calls.append(url)
            return {"items": [{"ExternalDescriptionStr": "<p>Entry level.</p>",
                               "ExternalQualificationsStr": "<ul><li>0-3 years of experience</li></ul>"}]}

    url = "https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26004840"
    text = job_description(F(), Job("McDermott", "Associate Process Engineer", url))
    assert "finder=ById;Id=%2226004840%22,siteNumber=CX_1" in calls[0]
    assert min_years_required(text) == 0


# ------------------------------------------------------- SuccessFactors / Eightfold
SF_RSS = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>
<title>Tetra Pak</title>
<item><title>Process Engineer Evaporator (Heerenveen, NL)</title>
<description><![CDATA[&lt;p&gt;You have &lt;b&gt;0-2 years&lt;/b&gt; of experience.&lt;/p&gt;]]></description>
<link>https://jobs.tetrapak.com/job/Heerenveen-Process-Engineer-Evaporator/1445/?feedId=1</link>
<guid isPermaLink="false">1445</guid><g:id>1445</g:id><g:location>Heerenveen, NL</g:location></item>
<item><title>Sales Manager (Jakarta, ID)</title><description>x</description>
<link>https://jobs.tetrapak.com/job/Jakarta-Sales-Manager/1446/</link><g:id>1446</g:id><g:location>Jakarta, ID</g:location></item>
<item><title>Talent community</title><link>https://jobs.tetrapak.com/talentcommunity/</link></item>
</channel></rss>"""


def test_successfactors_rss_feed(settings):
    f = FakeFetcher(html=SF_RSS)
    res = check_company(f, Company("Tetra Pak", "https://jobs.tetrapak.com/", adapter="successfactors"))
    assert not res.error and res.method == "successfactors"
    assert f.calls == ["https://jobs.tetrapak.com/sitemap.xml"]
    nl, idn = res.jobs
    assert nl.title == "Process Engineer Evaporator"
    assert nl.url == "https://jobs.tetrapak.com/job/Heerenveen-Process-Engineer-Evaporator/1445/"
    assert nl.id == "1445" and nl.location == "Heerenveen, NL, Netherlands"
    assert idn.location == "Jakarta, ID"
    assert is_relevant(nl, settings)


def test_sitemap_parser_reads_rss_links():
    from jobwatch.discover import _sitemap_urls
    _, pages = _sitemap_urls(SF_RSS)
    assert "https://jobs.tetrapak.com/job/Jakarta-Sales-Manager/1446/" in pages


def test_discover_recognises_platform_from_page():
    from jobwatch.discover import discover
    sf = '<link rel="icon" href="https://rmkcdn.successfactors.com/0e86/x.png">'
    rad = '<img src="https://tbcdn.talentbrew.com/company/543/logo.png">'
    assert discover(FakeFetcher(html=sf), Company("X", "https://jobs.tetrapak.com/")) == \
        ("successfactors", "https://jobs.tetrapak.com")
    assert discover(FakeFetcher(html=rad), Company("H", "https://careers.halliburton.com/location/nl/543/2")) == \
        ("sitemap", "https://careers.halliburton.com/sitemap.xml")


def test_discover_ignores_recruitee_analytics_script():
    from jobwatch.discover import find_ats_links
    html = ('<script src="https://careers-analytics.recruitee.com/tracker.js"></script>'
            '<a href="https://smartphotonics.recruitee.com/o/engineer">Engineer</a>')
    assert find_ats_links(html) == ("recruitee", "https://smartphotonics.recruitee.com")


def test_discover_follows_vacancies_link_from_homepage():
    from jobwatch.discover import discover

    class F(FakeFetcher):
        def get(self, url, **kw):
            self.calls.append(url)
            if url.endswith("robots.txt") or "sitemap" in url:
                raise RuntimeError("none")
            pages = {
                "https://www.bbio.nl/": '<a href="/products">Products</a><a href="/careers">Careers</a>',
                "https://www.bbio.nl/careers": '<a href="https://bbio.recruitee.com/o/qa-officer">QA Officer</a>',
            }
            return type("R", (), {"text": pages.get(url, ""), "url": url})()

    assert discover(F(), Company("BBio", "https://www.bbio.nl/")) == ("recruitee", "https://bbio.recruitee.com")


def test_eightfold_pcsx_paging_and_location():
    calls = []

    class F(FakeFetcher):
        def get_json(self, url, **kw):
            calls.append(url)
            start = int(url.split("start=")[1].split("&")[0])
            n = 10 if start == 0 else 2
            return {"data": {"count": 12, "positions": [
                {"id": start + i, "name": f"Engineer {start + i}", "locations": ["Delft, Netherlands"],
                 "positionUrl": f"/careers/job/{start + i}", "postedTs": 1791471702} for i in range(n)]}}

    res = check_company(F(), Company("DSM", "https://dsm.eightfold.ai/careers?domain=dsm.com&location=Netherlands"))
    assert not res.error and res.method == "eightfold" and len(res.jobs) == 12 and len(calls) == 2
    assert "api/pcsx/search?domain=dsm.com&start=0" in calls[0] and "location=Netherlands" in calls[0]
    assert res.jobs[0].url == "https://dsm.eightfold.ai/careers/job/0?domain=dsm.com"
    assert res.jobs[0].location == "Delft, Netherlands"


def test_html_falls_back_to_browser_when_blocked():
    import requests as rq

    class F(FakeFetcher):
        allow_render = True
        def get(self, url, **kw):
            resp = rq.Response(); resp.status_code = 403
            raise rq.HTTPError("403 Forbidden", response=resp)
        def render(self, url):
            return '<a href="/vacatures/process-engineer-123">Process Engineer</a>'

    res = check_company(F(), Company("Stamicarbon", "https://www.stamicarbon.com/vacancies"))
    assert not res.error and res.method == "rendered"
    assert res.jobs[0].title == "Process Engineer"


# ------------------------------------------------- second-round fixes (Oct 2026)
def test_workday_reads_past_second_page_when_later_pages_report_total_zero():
    def page(off):
        n = 20 if off < 60 else 5
        return {"total": 65 if off == 0 else 0,
                "jobPostings": [{"title": f"E{off + i}", "externalPath": f"/job/{off + i}"} for i in range(n)]}
    res = check_company(FakeFetcher(post=page), Company("BP", "https://bpinternational.wd3.myworkdayjobs.com/bpCareers"))
    assert len(res.jobs) == 65


def test_explicit_adapter_beats_cached_discovery():
    from jobwatch.discover import resolve
    cache = {"discovered": {"from": "https://jobs.tetrapak.com/", "adapter": "html",
                            "url": "https://jobs.tetrapak.com/", "at": "2099-01-01T00:00:00+00:00"}}
    c = resolve(FakeFetcher(), Company("TP", "https://jobs.tetrapak.com/", adapter="successfactors"), cache)
    assert c.adapter == "successfactors"


def test_old_cache_entries_are_rediscovered():
    from jobwatch.discover import resolve
    sf = '<link rel="icon" href="https://rmkcdn.successfactors.com/x.png">'
    cache = {"discovered": {"from": "https://jobs.eastman.com/", "adapter": "html",
                            "url": "https://jobs.eastman.com/", "at": "2099-01-01T00:00:00+00:00"}}
    c = resolve(FakeFetcher(html=sf), Company("Eastman", "https://jobs.eastman.com/"), cache)
    assert c.adapter == "successfactors" and cache["discovered"]["v"] >= 2


def test_successfactors_falls_back_to_sitemal():
    class F(FakeFetcher):
        def get(self, url, **kw):
            self.calls.append(url)
            text = SF_RSS if url.endswith("/sitemal.xml") else "<urlset><url><loc>https://x/about</loc></url></urlset>"
            return type("R", (), {"text": text, "url": url})()
    f = F()
    res = check_company(f, Company("FC", "https://mycareers.frieslandcampina.com/", adapter="successfactors"))
    assert not res.error and len(res.jobs) == 2
    assert f.calls[-1].endswith("/sitemal.xml")


def test_sitemap_adapter_reports_errors_instead_of_zero_jobs():
    class F(FakeFetcher):
        def get(self, url, **kw):
            raise RuntimeError("403 Forbidden")
    res = check_company(F(), Company("Hal", "https://careers.halliburton.com/sitemap.xml"))
    assert res.error


def test_posted_age_formats():
    from datetime import datetime, timezone
    from jobwatch.state import posted_age_days
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert posted_age_days("2026-10-12", now) == 1.5
    assert posted_age_days("Posted Today", now) == 0
    assert posted_age_days("Posted 30+ Days Ago", now) > 3
    assert round(posted_age_days("1791720000", now)) == round((now.timestamp() - 1791720000) / 86400)
    assert posted_age_days("Mon, 12 Oct 2026 12:00:00 GMT", now) == 1
    assert posted_age_days("", now) is None and posted_age_days("soon", now) is None


def test_rebaseline_still_reports_recent_postings(tmp_path):
    from datetime import date
    st = State(tmp_path / "s.json")
    today = date.today().isoformat()
    jobs = [Job("McD", "Associate Process Engineer", "u1", posted=today, id="1"),
            Job("McD", "Old job", "u2", posted="2026-01-01", id="2"),
            Job("McD", "No date", "u3", id="3")]
    assert [j.id for j in st.new_jobs("McD", jobs, source="feed-a")] == ["1"]
    assert st.new_jobs("McD", jobs, source="feed-a") == []          # seen now
    assert [j.id for j in st.new_jobs("McD", jobs, source="feed-b")] == ["1"]  # feed switch


# ------------------------------------------------------ repeat recent alerts
def test_alerts_repeat_jobs_shown_in_last_24h(tmp_path):
    from datetime import datetime, timedelta, timezone
    st = State(tmp_path / "s.json")
    a = Job("McD", "Associate Process Engineer", "u1", "The Hague, Netherlands", id="1")
    st.remember_shown([a])
    assert st.shown_within(24) and st.shown_within(24)[0].repeat
    assert st.shown_within(24, exclude={("McD", "1")}) == []
    assert st.shown_within(0) == []
    # older than 24h -> no longer repeated
    st.data["shown"][0]["found"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    assert st.shown_within(24) == []
    st.save()
    assert State(tmp_path / "s.json").data["shown"]  # kept on disk (48h) until pruned


def test_email_lists_repeats_after_new_jobs():
    new = [Job("Tetra Pak", "Service Engineer Trainee", "https://t/1", "Utrecht")]
    rep = [Job("McD", "Associate Process Engineer", "https://m/1", "The Hague", repeat=True, found="Tue 13:05")]
    subject, text, html_body = notify.build_email(new, repeats=rep)
    assert "1 new posting" in subject and "+1 from the last 24h" in subject
    assert text.index("Service Engineer Trainee") < text.index("Already shown in the last 24 hours")
    assert "first shown Tue 13:05" in text and "Already shown" in html_body


# ------------------------------------------------- location: NL / BE / India only
from jobwatch.filters import location_verdict, resolve_location  # noqa: E402


@pytest.mark.parametrize(
    "title,location,url,expected",
    [
        # location field, incl. other languages
        ("Production Engineer", "Tilburg, Paesi Bassi", "u", "keep"),
        ("Data Analysis Scientist", "Uppsala, Svezia", "u", "drop"),
        ("Field Service Engineer", "Hemel Hempstead, Regno Unito", "u", "drop"),
        ("Chemist I", "Gdynia, Poland", "u", "drop"),
        ("Engineer II, Hardware", "Hyderabad, India", "u", "drop"),  # India no longer wanted (2026-11-03)
        ("Process Engineer", "Rotterdam; Houston, TX", "u", "keep"),       # a wanted place wins
        ("Process Engineer", "Liège", "u", "keep"),
        # only the title says where
        ("Scientist - Cell Culture in San Diego, California, Stati Uniti d'America", "", "u", "drop"),
        ("St Louis Digital and Automation Project Engineer MO", "", "u", "drop"),
        ("Shanghai Mechanical Engineer", "", "u", "drop"),
        # only the link says where (Workday)
        ("Chemical Engineer", "", "https://x.wd3.myworkdayjobs.com/C/job/Veldhoven-Netherlands/CE_1", "keep"),
        ("Chemical Engineer", "", "https://x.wd3.myworkdayjobs.com/C/job/Houston-Texas/CE_1", "drop"),
        # locale codes in a link are not a location
        ("Manufacturing Engineer", "", "https://careers.slb.com/jobdescription.aspx?id=EF21350-en_US", "unknown"),
        ("Manufacturing Engineer", "", "u", "unknown"),
    ],
)
def test_location_verdict(settings, title, location, url, expected):
    assert location_verdict(Job("X", title, url, location=location), settings) == expected


def test_unknown_location_resolved_from_job_page(settings):
    assert settings.keep_unmatched_locations is False
    us = Job("SLB", "Manufacturing Engineer", "https://careers.slb.com/job?id=1",
             description="Join our team in Houston. Location: United States. " * 20)
    assert not resolve_location(us, settings)
    nl = Job("SLB", "Manufacturing Engineer", "https://careers.slb.com/job?id=2",
             description="Based at our site in the Netherlands. " * 20)
    assert resolve_location(nl, settings) and nl.location
    dutch = Job("Tata", "Procestechnoloog", "https://careers.example.com/job/3",
                description=("Als procestechnoloog ben jij verantwoordelijk voor het verbeteren van onze installaties. "
                             "Wij bieden een goed salaris en je werkt met een team van specialisten bij ons. "
                             "Je hebt een afgeronde opleiding en werkervaring is een pluspunt voor deze functie. ") * 3)
    assert resolve_location(dutch, settings)
    nothing = Job("SLB", "Manufacturing Engineer", "https://careers.slb.com/job?id=4", description="Great job. " * 50)
    assert not resolve_location(nothing, settings)               # strict: unknown -> dropped
    assert resolve_location(Job("VDL", "Engineer", "https://www.werkenbijvdl.nl/vacature/1"), settings)  # .nl site


def test_run_drops_foreign_and_keeps_dutch(tmp_path, monkeypatch):
    """End to end: a sitemap company with one Dutch and one US posting, location only on the job page."""
    import jobwatch.cli as cli
    pages = {
        "https://acme.com/job/1": '<html><h1>Process Engineer</h1><p>' + "Location: Geleen, Netherlands. " * 20 + '</p></html>',
        "https://acme.com/job/2": '<html><h1>Process Engineer</h1><p>' + "Location: Baton Rouge, United States. " * 20 + '</p></html>',
    }
    from jobwatch.models import CheckResult
    jobs = [Job("Acme", "Process Engineer", u) for u in pages]

    class F:
        allow_render = False
        def __init__(self, *a, **k): pass
        def get(self, url):
            class R: text = pages[url]
            return R()
        def close(self): pass

    calls = {"n": 0}
    def fake_check(f, src, n):
        calls["n"] += 1
        return CheckResult(Company("Acme", "https://acme.com"), jobs=jobs if calls["n"] > 1 else jobs[:0], method="sitemap")
    monkeypatch.setattr(cli, "Fetcher", F)
    monkeypatch.setattr(cli, "check_company", fake_check)
    monkeypatch.setattr(cli, "resolve", lambda f, c, cache, force=False: type("S", (), {"adapter": "sitemap", "url": c.url})())
    csv = tmp_path / "c.csv"
    csv.write_text("name,priority,group,url,enabled\nAcme,High,x,https://acme.com,yes\n", encoding="utf-8")
    args = ["run", "--config", str(ROOT / "config.yaml"), "--companies", str(csv),
            "--state", str(tmp_path / "s.json"), "--notify", "none"]
    assert main(args) == 0                     # baseline
    out = []
    monkeypatch.setattr(cli.notify, "build_email", lambda new, errors, repeats=None: out.append(new) or ("s", "t", "h"))
    assert main(args) == 0
    assert [j.url for j in out[0]] == ["https://acme.com/job/1"]


def test_dedupe_same_job_listed_twice():
    from jobwatch.cli import _dedupe
    jobs = [Job("Tata Steel IJmuiden", "Technologist", "https://www.tatasteeljobs.nl/vacature/768/t", "Nederland"),
            Job("Tata Steel Technology", "Technologist", "https://www.tatasteeljobs.nl/vacature/768/t", "Nederland"),
            Job("Lamb Weston", "QA Engineer", "https://lambweston.eu/nl/careers/vacancies/Kruiningen/QA/1"),
            Job("Lamb Weston", "QA Engineer", "https://lambweston.eu/de/careers/vacancies/Kruiningen/QA/1"),
            Job("Lamb Weston", "QA Engineer", "https://lambweston.eu/careers/vacancies/Bergen-op-Zoom/QA/2", "Bergen op Zoom")]
    assert [j.url.rsplit("/", 2)[-2:] for j in _dedupe(jobs)] == [["768", "t"], ["QA", "1"], ["QA", "2"]]


def test_werken_bij_and_careers_company_links():
    # Colsen: /werken-bij-colsen/<slug> (NL) and /en/careers-colsen/<slug> (EN)
    from jobwatch.adapters import parse_html_jobs
    html = '<html><body>' \
           '<a href="/werken-bij-colsen/process-engineer">Process Engineer</a>' \
           '<a href="/en/careers-colsen/project-engineer">Project Engineer</a>' \
           '<a href="/werken-bij-colsen">Werken bij</a>' \
           '<a href="/over-ons/team">Ons team en cultuur</a></body></html>'
    urls = [j.url for j in parse_html_jobs(html, "https://www.colsen.nl/werken-bij-colsen", "Colsen")]
    assert urls == ["https://www.colsen.nl/werken-bij-colsen/process-engineer",
                    "https://www.colsen.nl/en/careers-colsen/project-engineer"]


# ------------------------------------------------- generic titles: read the description
def test_generic_titles_kept_only_if_description_asks_for_chemical_background(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "name,priority,group,url,enabled,adapter,render,link_type,note\n"
        "Example,High,Chemicals,https://example.com/vacatures,yes,auto,never,Careers page,\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "include_keywords: [engineer, junior, process engineer]\n"
        "exclude_keywords: [senior]\n"
        "favourite_keywords: [process engineer]\n"
        "only_favourites: true\n"
        "check_description: true\n"
        "description_keywords: [chemical engineering, chemische technologie]\n"
        "check_experience: false\n"
        "location_keywords: [netherlands, rotterdam]\n"
        "keep_unmatched_locations: false\n",
        encoding="utf-8",
    )
    pages = {
        "https://example.com/vacatures": HTML,
        "https://example.com/vacatures/engineer-1": "<p>Your profile: MSc in Chemical Engineering or similar. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/junior-engineer-2": "<p>Je hebt een wo-opleiding Chemische Technologie. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/engineer-3": "<p>We are a chemical company. You have a BSc in Electrical Engineering. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/process-engineer-4": "<p>Rotterdam, Netherlands.</p>",
    }

    def fake_get(self, url, **kw):
        if url not in pages:
            raise ConnectionError("unreachable")
        return type("R", (), {"text": pages[url], "url": url})()

    monkeypatch.setattr("jobwatch.fetch.Fetcher.get", fake_get)
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    args = ["--config", str(cfg), "--companies", str(companies), "--state", str(tmp_path / "state.json"),
            "--no-render"]
    assert main(["run", *args]) == 0                      # baseline
    pages["https://example.com/vacatures"] = HTML.replace(
        "</ul>",
        '<li><a href="/vacatures/engineer-1">Engineer</a></li>'
        '<li><a href="/vacatures/junior-engineer-2">Junior Engineer</a></li>'
        '<li><a href="/vacatures/engineer-3">Electrical Engineer</a></li>'
        '<li><a href="/vacatures/process-engineer-4">Process Engineer</a></li></ul>')
    assert main(["run", *args]) == 0
    _, text, _ = emails[0]
    assert "⭐ Process Engineer" in text                                  # field word in title
    assert "Engineer (" in text and "description: chemical engineering" in text  # generic title, ChemE asked
    assert "Junior Engineer" in text and "description: chemische technologie" in text
    assert "Electrical Engineer" not in text                             # 'chemical company' isn't enough


# ------------------------------------------------------------- Dutch language check
@pytest.mark.parametrize("text,expected", [
    ("Fluent in Dutch and English is required.", "required"),
    ("You speak Dutch fluently and have good English.", "required"),
    ("Dutch at C1 level is mandatory.", "required"),
    ("Je spreekt vloeiend Nederlands en Engels.", "required"),
    ("Goede beheersing van de Nederlandse en Engelse taal in woord en geschrift.", "required"),
    ("Fluent Dutch required; German is a plus.", "required"),
    ("Fluent English is required. Dutch is a plus.", "lenient"),
    ("Fluent Dutch is a plus but not required.", "lenient"),
    ("Basic knowledge of Dutch (A2) is sufficient.", "lenient"),
    ("Intermediate Dutch (B1/B2) is fine.", "lenient"),
    ("You are willing to learn Dutch.", "lenient"),
    ("Kennis van de Nederlandse taal is een pré.", "lenient"),
    ("Join a leading Dutch company in the port of Rotterdam.", ""),
    ("", ""),
])
def test_dutch_requirement(text, expected):
    from jobwatch.filters import dutch_requirement
    assert dutch_requirement(text) == expected


def test_run_skips_jobs_asking_fluent_dutch(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    companies.write_text(
        "name,priority,group,url,enabled,adapter,render,link_type,note\n"
        "Example,High,Chemicals,https://example.com/vacatures,yes,auto,never,Careers page,\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "include_keywords: [process engineer]\nfavourite_keywords: [process engineer]\n"
        "skip_if_fluent_dutch_required: true\ncheck_experience: false\n"
        "location_keywords: [netherlands]\nkeep_unmatched_locations: false\n",
        encoding="utf-8",
    )
    pages = {
        "https://example.com/vacatures": HTML,
        "https://example.com/vacatures/pe-1": "<p>Je spreekt vloeiend Nederlands. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/pe-2": "<p>Fluent English required. Basic Dutch is a plus. Rotterdam, Netherlands.</p>",
        "https://example.com/vacatures/pe-3": "<p>English speaking team. Rotterdam, Netherlands.</p>",
    }

    def fake_get(self, url, **kw):
        if url not in pages:
            raise ConnectionError("unreachable")
        return type("R", (), {"text": pages[url], "url": url})()

    monkeypatch.setattr("jobwatch.fetch.Fetcher.get", fake_get)
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    args = ["--config", str(cfg), "--companies", str(companies), "--state", str(tmp_path / "s.json"), "--no-render"]
    assert main(["run", *args]) == 0
    pages["https://example.com/vacatures"] = HTML.replace(
        "</ul>",
        '<li><a href="/vacatures/pe-1">Process Engineer Dutch</a></li>'
        '<li><a href="/vacatures/pe-2">Process Engineer Basic</a></li>'
        '<li><a href="/vacatures/pe-3">Process Engineer English</a></li></ul>')
    assert main(["run", *args]) == 0
    _, text, _ = emails[0]
    assert "Process Engineer Dutch" not in text
    assert "Process Engineer Basic" in text and "basic Dutch OK" in text
    assert "Process Engineer English" in text


# ------------------------------------------- live page next to a stale sitemap
SITEMAP_JOBS = "<urlset>" + "".join(
    f"<url><loc>https://acme.nl/vacature/{n}/{t}</loc></url>"
    for n, t in [(770, "operator-rd"), (771, "prototypebouwer"), (772, "teamleider-productie"),
                 (773, "hogere-veiligheidskundige")]) + "</urlset>"
LIVE_PAGE = ('<ul><li><a href="/vacature/786/young-professional-process-innovation-technologist">'
             'Young Professional Process Innovation Technologist</a></li>'
             '<li><a href="/vacature/773/hogere-veiligheidskundige">Hogere Veiligheidskundige</a></li>'
             '<li><a href="/vacatures/vakgebieden/research-development">Research &amp; Development jobs</a></li>'
             '<li><a href="/nieuws/2026/open-dag-ijmuiden">Open dag IJmuiden vacature-event</a></li></ul>')


def test_job_path_prefix():
    from jobwatch.adapters import _job_path_prefix
    assert _job_path_prefix("https://www.acme.nl/vacature/786/young-pro") == ("acme.nl", "/vacature")
    assert _job_path_prefix("https://acme.nl/nl/vacatures/process-engineer") == ("acme.nl", "/nl/vacatures")


def test_live_page_extras_finds_jobs_missing_from_sitemap():
    from jobwatch.adapters import fetch_sitemap, live_page_extras
    f = SiteFetcher({"https://acme.nl/sitemap.xml": SITEMAP_JOBS, "https://acme.nl/vacatures": LIVE_PAGE})
    sm = fetch_sitemap(f, Company("Acme", "https://acme.nl/sitemap.xml", adapter="sitemap"))
    extras = live_page_extras(f, Company("Acme", "https://acme.nl/vacatures"), sm)
    assert [j.url for j in extras] == ["https://acme.nl/vacature/786/young-professional-process-innovation-technologist"]
    assert extras[0].title == "Young Professional Process Innovation Technologist"
    # an .xml careers URL, or an unreachable page, adds nothing (and never raises out of the run)
    assert live_page_extras(f, Company("Acme", "https://acme.nl/sitemap.xml"), sm) == []


def test_run_alerts_job_on_live_page_before_sitemap_has_it(tmp_path, monkeypatch):
    import dataclasses
    import jobwatch.cli as cli
    companies = tmp_path / "companies.csv"
    companies.write_text("name,priority,group,url,enabled,adapter,render,link_type,note\n"
                         "Acme,High,Metals,https://acme.nl/vacatures,yes,auto,never,Careers page,\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(Path("config.yaml").read_text(encoding="utf-8").replace(
        "check_description: true", "check_description: false"), encoding="utf-8")
    pages = {"https://acme.nl/sitemap.xml": SITEMAP_JOBS,
             "https://acme.nl/vacatures": LIVE_PAGE.replace(
                 '<li><a href="/vacature/786/young-professional-process-innovation-technologist">'
                 'Young Professional Process Innovation Technologist</a></li>', ""),
             "https://acme.nl/vacature/786/young-professional-process-innovation-technologist":
                 "<p>Locatie: IJmuiden, Nederland. Opleiding: chemical engineering.</p>"}

    def fake_get(self, url, **kw):
        if url not in pages:
            raise ConnectionError("unreachable")
        return type("R", (), {"text": pages[url], "url": url})()

    monkeypatch.setattr("jobwatch.fetch.Fetcher.get", fake_get)
    monkeypatch.setattr(cli, "resolve", lambda f, c, cache, force=False:
                        dataclasses.replace(c, adapter="sitemap", url="https://acme.nl/sitemap.xml"))
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    args = ["--companies", str(companies), "--state", str(tmp_path / "state.json"),
            "--config", str(cfg), "--no-render"]
    assert main(["run", *args]) == 0                     # baseline
    assert emails == []
    pages["https://acme.nl/vacatures"] = LIVE_PAGE       # posted this morning; sitemap still old
    assert main(["run", *args]) == 0
    assert len(emails) == 1 and "Young Professional Process Innovation Technologist" in emails[0][1]


# ------------------------------------------------ discovery: don't get stuck on a wrong page
def test_vacancy_link_skips_other_language_versions():
    from jobwatch.discover import vacancy_link
    html = ('<a href="/fr/carrieres">Carrières</a><a href="/de/karriere">Karriere</a>'
            '<a href="/en/careers/vacancies">Vacancies</a>')
    assert vacancy_link(html, "https://acme.com/en/careers") == "https://acme.com/en/careers/vacancies"


def test_discover_stays_on_page_when_followed_link_has_no_jobs_either():
    from jobwatch.discover import discover
    pages = {"https://acme.com/careers": '<p>We are hiring!</p><a href="/careers/our-culture">Working at Acme: careers</a>',
             "https://acme.com/careers/our-culture": "<p>Great people, nice coffee.</p>"}
    f = SiteFetcher(pages)
    assert discover(f, Company("Acme", "https://acme.com/careers")) == ("html", "https://acme.com/careers")


def test_discover_keeps_empty_careers_page_found_from_homepage():
    # a homepage linking to /careers that has no openings today: watch /careers, not the homepage
    from jobwatch.discover import discover
    pages = {"https://acme.com/": '<a href="/products">Products</a><a href="/careers">Careers</a>',
             "https://acme.com/careers": "<p>No openings right now - send an open application.</p>"}
    assert discover(SiteFetcher(pages), Company("Acme", "https://acme.com/")) == ("html", "https://acme.com/careers")


def test_resolve_rediscovers_only_companies_without_jobs():
    from jobwatch.discover import resolve
    old = {"from": "https://acme.com/careers", "adapter": "html", "url": "https://acme.com/careers/x",
           "at": "2099-01-01T00:00:00+00:00", "v": 2}
    working = {"seen": {"https://acme.com/job/1": "t"}, "discovered": dict(old)}
    c = Company("Acme", "https://acme.com/careers")
    assert resolve(SiteFetcher({}), c, working).url == "https://acme.com/careers/x"   # untouched
    empty = {"seen": {}, "discovered": dict(old)}
    f = SiteFetcher({"https://acme.com/careers": '<a href="/vacature/12/process-engineer">Process engineer</a>'})
    assert resolve(f, c, empty).url == "https://acme.com/careers"                       # looked again


def test_fix_encoding_reads_utf8_sitemap_without_charset():
    import requests
    from jobwatch.fetch import _fix_encoding
    from jobwatch.discover import _sitemap_urls
    r = requests.Response()
    r._content = ("﻿<?xml version='1.0' encoding='utf-8'?><urlset><url><loc>https://x.com/job/a/1</loc></url>"
                  "</urlset>").encode("utf-8")
    r.headers["Content-Type"] = "text/xml"
    r.encoding = "ISO-8859-1"          # what requests guesses for text/* without a charset
    assert _sitemap_urls(r.text)[1] == []
    _fix_encoding(r)
    assert _sitemap_urls(r.text)[1] == ["https://x.com/job/a/1"]
    r2 = requests.Response()
    r2._content = "Procesoperator Zeeländ".encode("utf-8")
    r2.headers["Content-Type"] = "text/html"
    _fix_encoding(r2)
    assert r2.text == "Procesoperator Zeeländ"
    r3 = requests.Response()
    r3._content = "café".encode("latin-1")
    r3.headers["Content-Type"] = "text/html"
    r3.encoding = "ISO-8859-1"
    _fix_encoding(r3)
    assert r3.text == "café"           # real Latin-1 pages are left alone


def test_companies_that_never_had_jobs_are_rebaselined_once(tmp_path, monkeypatch):
    companies = tmp_path / "companies.csv"
    companies.write_text("name,priority,group,url,enabled,adapter,render,link_type,note\n"
                         "Acme,High,Chemicals,https://acme.nl/vacatures,yes,html,never,Careers page,\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"companies": {"Acme": {"seen": {}, "baselined": True}}, "runs": 5}), encoding="utf-8")
    page = '<a href="/vacatures/junior-chemical-engineer-9">Junior Chemical Engineer</a>'
    monkeypatch.setattr("jobwatch.fetch.Fetcher.get",
                        lambda self, url, **kw: type("R", (), {"text": page, "url": url})())
    emails = []
    monkeypatch.setattr(notify, "send_email", lambda *a: emails.append(a) or True)
    assert main(["run", "--companies", str(companies), "--state", str(state), "--no-render"]) == 0
    assert emails == []                               # old job found by the fix: recorded quietly
    data = json.loads(state.read_text())
    assert data["migrations"]["rebaseline_empty_2026_11"] and data["companies"]["Acme"]["seen"]


def test_join_us_job_links_are_recognised():
    from jobwatch.adapters import parse_html_jobs
    html = ('<a href="/join-us/energy-portfolio-specialist/">Energy Portfolio Specialist</a>'
            '<a href="/join-us/?page=2">Next page</a><a href="/join-us/apply-spontaneous/">Spontaneous application</a>')
    urls = [j.url for j in parse_html_jobs(html, "https://www.huvepharma.com/join-us/", "Huvepharma")]
    assert "https://www.huvepharma.com/join-us/energy-portfolio-specialist/" in urls
    assert not any("page=2" in u for u in urls)


def test_job_links_with_trailing_slash_are_recognised():
    from jobwatch.adapters import parse_html_jobs
    html = '<a href="/careers/senior-ehs-specialist/">Senior EHS Specialist</a>'
    assert [j.url for j in parse_html_jobs(html, "https://www.iqatalyst.com/careers/", "IQatalyst")] == \
        ["https://www.iqatalyst.com/careers/senior-ehs-specialist/"]


def test_ashby_board_is_read_from_posting_api():
    from jobwatch.adapters import check_company, detect_adapter
    from jobwatch.discover import find_ats_links
    assert detect_adapter("https://jobs.ashbyhq.com/skynrg") == "ashby"
    assert find_ats_links('<a href="https://jobs.ashbyhq.com/skynrg/?ashby_jid=9e6f">HSE</a>') == \
        ("ashby", "https://jobs.ashbyhq.com/skynrg")
    api = {"https://api.ashbyhq.com/posting-api/job-board/skynrg?includeCompensation=false": {"jobs": [
        {"id": "9e6f", "title": "HSE Specialist | DSL-01", "location": "Delfzijl", "isListed": True,
         "jobUrl": "https://jobs.ashbyhq.com/skynrg/9e6f", "publishedAt": "2026-11-01T10:00:00Z",
         "address": {"postalAddress": {"addressCountry": "Netherlands"}}},
        {"id": "x", "title": "Hidden", "isListed": False}]}}
    res = check_company(SiteFetcher({}, api), Company("SkyNRG", "https://jobs.ashbyhq.com/skynrg"))
    assert res.method == "ashby" and [j.title for j in res.jobs] == ["HSE Specialist | DSL-01"]
    assert res.jobs[0].location == "Delfzijl; Netherlands"


def test_homerun_board_jobs_are_read():
    from jobwatch.adapters import parse_html_jobs
    html = ('<meta name="homerun:company_id" content="com_x">'
            '<a href="/operator/en">Operator</a><a href="/operator">Operator</a>'
            '<a href="https://jobs.myne.eco/commissioning-engineer/en">Commissioning Engineer</a>'
            '<a href="/?lang=en">English</a><a href="https://myne.eco/">Our website</a>')
    jobs = parse_html_jobs(html, "https://jobs.myne.eco/", "Myne")
    assert sorted(j.url for j in jobs) == ["https://jobs.myne.eco/commissioning-engineer", "https://jobs.myne.eco/operator"]


# ------------------------------------------------- transient API errors (Nov 2026)
def _resp(status, body):
    import requests as rq
    r = rq.Response()
    r.status_code = status
    r._content = body.encode()
    return r


@pytest.mark.parametrize("first", [(500, "oops"), (200, "<html>maintenance</html>")])
def test_json_api_retries_once_after_server_error_or_html(monkeypatch, first):
    from jobwatch import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "TRANSIENT_RETRY_SECONDS", 0)
    f = fetch_mod.Fetcher(Settings(request_delay_seconds=0))
    answers = [_resp(*first), _resp(200, '{"total": 1}')]
    monkeypatch.setattr(f.session, "post", lambda *a, **k: answers.pop(0))
    assert f.post_json("https://x.wd3.myworkdayjobs.com/wday/cxs/x/s/jobs", {}) == {"total": 1}
    answers[:] = [_resp(*first), _resp(200, '{"ok": true}')]
    monkeypatch.setattr(f.session, "get", lambda *a, **k: answers.pop(0))
    assert f.get_json("https://api.example.com/jobs") == {"ok": True}


def test_json_api_gives_up_after_one_retry(monkeypatch):
    import requests as rq
    from jobwatch import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "TRANSIENT_RETRY_SECONDS", 0)
    f = fetch_mod.Fetcher(Settings(request_delay_seconds=0))
    calls = []
    monkeypatch.setattr(f.session, "post", lambda *a, **k: calls.append(1) or _resp(500, "down"))
    with pytest.raises(rq.HTTPError):
        f.post_json("https://x.wd3.myworkdayjobs.com/wday/cxs/x/s/jobs", {})
    assert len(calls) == 2


def test_plain_get_retries_once_after_server_error(monkeypatch):
    from jobwatch import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "TRANSIENT_RETRY_SECONDS", 0)
    f = fetch_mod.Fetcher(Settings(request_delay_seconds=0))
    monkeypatch.setattr(f, "_check_robots", lambda url: None)
    answers = [_resp(503, "busy"), _resp(200, "<rss></rss>")]
    monkeypatch.setattr(f.session, "get", lambda *a, **k: answers.pop(0))
    assert f.get("https://jobs.example.com/sitemap.xml").text == "<rss></rss>"


def test_successfactors_reports_the_sitemap_error_not_the_sitemal_404():
    class F(FakeFetcher):
        def get(self, url, **kw):
            self.calls.append(url)
            raise RuntimeError(("503 for " if url.endswith("/sitemap.xml") else "404 for ") + url)
    res = check_company(F(), Company("SBM", "https://careers.sbmoffshore.com/", adapter="successfactors"))
    assert res.error and "503" in res.error and "sitemap.xml" in res.error


def test_workday_keeps_earlier_pages_when_a_later_page_fails():
    class F(FakeFetcher):
        def post_json(self, url, payload):
            if payload["offset"] > 0:
                raise RuntimeError("422 Client Error: Unprocessable Entity")
            return {"total": 40, "jobPostings": [
                {"title": f"Engineer {i}", "externalPath": f"/job/Nijmegen/E_{i}", "locationsText": "Nijmegen"}
                for i in range(20)]}
    res = check_company(F(), Company("NXP", "https://nxp.wd3.myworkdayjobs.com/careers", adapter="workday"), 300)
    assert not res.error and len(res.jobs) == 20


def test_json_api_4xx_error_includes_what_the_server_said(monkeypatch):
    import requests as rq
    from jobwatch import fetch as fetch_mod
    f = fetch_mod.Fetcher(Settings(request_delay_seconds=0))
    r = _resp(422, '{"errorCode": "S22", "message": "Invalid offset"}')
    r.url = "https://nxp.wd3.myworkdayjobs.com/wday/cxs/nxp/careers/jobs"
    monkeypatch.setattr(f.session, "post", lambda *a, **k: r)
    with pytest.raises(rq.HTTPError) as exc:
        f.post_json(r.url, {})
    assert "422" in str(exc.value) and "Invalid offset" in str(exc.value)
    assert exc.value.response.status_code == 422
