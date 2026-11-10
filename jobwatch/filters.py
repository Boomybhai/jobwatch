"""Decide which postings are relevant."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .config import Settings
from .models import Job

# "Houston, TX" / "Midland, MI, USA" style locations
US_STATE_RE = re.compile(r",\s*([A-Z]{2})\b")
# Dutch province codes some careers sites use ("Bergen op Zoom, NB") — never treat as US states.
NL_CODES = {"NL", "ZH", "NH", "NB", "GE", "GLD", "UT", "OV", "LI", "FR", "GR", "DR", "FL", "ZE"}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "GA", "HI", "ID", "IL", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NJ", "NM", "NY", "NC", "ND",
    "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "VT", "VA", "WA", "WV", "WI", "WY",
}
# "St Louis Digital ... Engineer MO" - a US state code as the last word of a title
TITLE_STATE_RE = re.compile(r"(?:\s|,|-)\(?([A-Z]{2})\)?\s*$")
# locale codes in URLs ("en_US", "en-GB") say which language, not where the job is
LOCALE_RE = re.compile(r"(?<![A-Za-z])[a-z]{2}[-_][A-Z]{2}(?![A-Za-z])")

# Country names checked in the job DESCRIPTION when nothing else tells us where a job is.
# Only unambiguous names here (no "us", "ham", "ede": those would match ordinary words).
WANTED_COUNTRIES = [
    "netherlands", "nederland", "the netherlands", "pays-bas", "niederlande", "paesi bassi",
    "belgium", "belgië", "belgie", "belgique", "belgien",
    "india",
]
FOREIGN_COUNTRIES = [
    "united states", "usa", "u.s.a.", "united kingdom", "germany", "deutschland", "duitsland", "france",
    "frankrijk", "spain", "españa", "italy", "italia", "switzerland", "schweiz", "austria", "sweden",
    "denmark", "norway", "finland", "poland", "czech republic", "czechia", "hungary", "romania",
    "ireland", "portugal", "greece", "canada", "mexico", "méxico", "brazil", "brasil", "brésil", "brasilien", "argentina", "chile", "colombia",
    "china", "japan", "singapore", "malaysia", "indonesia", "thailand", "vietnam", "philippines",
    "australia", "new zealand", "saudi arabia", "united arab emirates", "qatar", "south africa",
    "egypt", "israel", "turkey", "türkiye", "south korea", "taiwan", "lithuania", "latvia", "estonia",
    "slovakia", "slovenia", "croatia", "bulgaria", "serbia", "luxembourg",
]
# Country codes in location fields ("CH - Visp", "Cardiff, gb", "Dammam, SAU", "GBR-Norfolk").
WANTED_CODES = {"nl", "nld", "be", "bel", "in", "ind"}
FOREIGN_CODES = {
    "us", "usa", "gb", "gbr", "uk", "de", "deu", "fra", "es", "esp", "it", "ita", "ch", "che", "at", "aut",
    "se", "swe", "dk", "dnk", "no", "nor", "fi", "fin", "pl", "pol", "cz", "cze", "hu", "hun", "ro", "rou",
    "ie", "irl", "pt", "prt", "grc", "bg", "bgr", "hr", "hrv", "si", "svn", "sk", "svk", "lt", "ltu", "lv",
    "lva", "ee", "est", "lu", "lux", "ca", "can", "mx", "mex", "br", "bra", "ar", "arg", "cl", "chl", "co",
    "col", "pe", "per", "cn", "chn", "jp", "jpn", "kr", "kor", "tw", "twn", "sg", "sgp", "my", "mys", "id",
    "idn", "th", "tha", "vn", "vnm", "ph", "phl", "au", "aus", "nz", "nzl", "sa", "sau", "ae", "are", "qa",
    "qat", "za", "zaf", "eg", "egy", "il", "isr", "tr", "tur", "ru", "rus", "ua", "ukr", "pk", "pak", "bd",
    "bgd", "lk", "lka", "hk", "hkg", "iq", "irq", "ng", "nga", "ma", "mar",
}
# Dutch province codes double as country codes (FR = Friesland/France) - never decide on those.
_AMBIGUOUS_CODES = {c.lower() for c in NL_CODES}

# Common Dutch words: a description full of these is a job in the Netherlands or Flanders.
DUTCH_WORDS = [
    "je", "jij", "jouw", "wij", "we", "ons", "onze", "het", "een", "van", "voor", "met", "bij",
    "naar", "zijn", "wordt", "heb", "werkervaring", "vacature", "solliciteer", "functie",
    "werkzaamheden", "opleiding", "salaris",
]
_DUTCH_RE = re.compile(r"(?<![a-zà-ÿ])(" + "|".join(DUTCH_WORDS) + r")(?![a-zà-ÿ])")


def _contains_any(text: str, words: list[str]) -> bool:
    for w in words:
        # whole-word-ish match so "intern" doesn't hit "international"
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text):
            return True
    return False


def _fold(text: str) -> str:
    """Lower-case, and also add an accent-free copy ("Liège" -> "liege")."""
    low = text.lower()
    plain = "".join(c for c in unicodedata.normalize("NFKD", low) if not unicodedata.combining(c))
    return low if plain == low else f"{low} | {plain}"


def _url_words(url: str) -> str:
    """The path/query of a job link as words: /job/Veldhoven-Netherlands/X -> 'job veldhoven netherlands x'."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    text = LOCALE_RE.sub(" ", unquote(parts.path + " " + parts.query))
    return re.sub(r"[-_/+=&?.%]+", " ", text).lower()


def _domain_country(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.endswith((".nl", ".be", ".in"))


def title_ok(job: Job, settings: Settings) -> bool:
    title = job.title.lower()
    if settings.exclude_keywords and _contains_any(title, settings.exclude_keywords):
        return False
    if settings.include_keywords and not _contains_any(title, settings.include_keywords):
        return False
    return True


def _place_verdict(text: str, settings: Settings, min_len: int = 1) -> str:
    if not text:
        return "unknown"
    t = _fold(text)
    wanted = [w for w in settings.location_keywords if len(w) >= min_len]
    foreign = [w for w in settings.foreign_location_keywords if len(w) >= min_len]
    if wanted and _contains_any(t, wanted):
        return "keep"
    if foreign and _contains_any(t, foreign):
        return "drop"
    return "unknown"


def _code_verdict(location: str) -> str:
    """'keep' / 'drop' from country codes written as their own part of the location."""
    parts = {p.strip().lower() for p in re.split(r"[,;()/|]|\s[-–]\s|-(?=[A-Za-z])|^([A-Za-z]{2,3})\s", location) if p}
    codes = {p for p in parts if re.fullmatch(r"[a-z]{2,3}", p)} - _AMBIGUOUS_CODES
    if codes & WANTED_CODES:
        return "keep"
    if codes & FOREIGN_CODES:
        return "drop"
    return "unknown"


def location_verdict(job: Job, settings: Settings, use_page: bool = False) -> str:
    """'keep' (in a wanted country), 'drop' (elsewhere) or 'unknown'.

    Looks at, in order: the location the site gives, the title, the link, and -
    with use_page=True, after the job page was read - the description text.
    A wanted place anywhere wins over a foreign one ("Rotterdam; Houston" is kept).
    """
    if not (settings.location_keywords or settings.foreign_location_keywords):
        return "keep"  # location filter switched off
    # 1. the location field
    v = _place_verdict(job.location, settings)
    if v != "unknown":
        return v
    if job.location:
        v = _code_verdict(job.location)
        if v != "unknown":
            return v
        codes = set(US_STATE_RE.findall(job.location))
        if codes and not codes & NL_CODES:
            return "drop"
    # 2. the title ("... Engineer in San Diego, California"), 3. the link (Workday: /job/Veldhoven-Netherlands/)
    title_v = _place_verdict(job.title, settings)
    url_v = _place_verdict(_url_words(job.url), settings, min_len=3)  # "/nl/" in a link is a language
    if "keep" in (title_v, url_v):
        return "keep"
    if "drop" in (title_v, url_v):
        return "drop"
    m = TITLE_STATE_RE.search(job.title)
    if m and m.group(1) in US_STATES:
        return "drop"
    if not use_page:
        return "unknown"
    # 4. the job description
    text = _fold(job.description or "")
    if text:
        wanted, foreign = _contains_any(text, WANTED_COUNTRIES), _contains_any(text, FOREIGN_COUNTRIES)
        if job.location:
            # the site named a place we don't know ("Seremban"): a company blurb like
            # "headquartered in the Netherlands" must not outweigh the job's own country
            if foreign:
                return "drop"
            if wanted:
                return "keep"
        elif wanted:
            return "keep"
        elif foreign:
            return "drop"
        if len(set(_DUTCH_RE.findall(text[:6000]))) >= 8:
            return "keep"  # written in Dutch -> Netherlands / Flanders
    # 5. a .nl / .be / .in careers site
    if _domain_country(job.url):
        return "keep"
    return "unknown"


def resolve_location(job: Job, settings: Settings) -> bool:
    """Final yes/no after the job page was read. Fills job.location when we worked it out."""
    v = location_verdict(job, settings, use_page=True)
    if v == "unknown":
        return bool(settings.keep_unmatched_locations)
    if v == "keep" and not job.location:
        job.location = _country_label(job)
    return v == "keep"


def _country_label(job: Job) -> str:
    """Short location to show in alerts for a job placed via its page: 'Netherlands', 'Belgium', 'India'."""
    text = _fold(" ".join([job.title, _url_words(job.url), job.description[:6000]]))
    for label, words in (("Netherlands", ["netherlands", "nederland", "holland"]),
                         ("Belgium", ["belgium", "belgië", "belgie", "belgique"]), ("India", ["india"])):
        if _contains_any(text, words):
            return label
    host = urlsplit(job.url).hostname or ""
    return {"nl": "Netherlands", "be": "Belgium", "in": "India"}.get(host.rsplit(".", 1)[-1], "Netherlands")


def is_relevant(job: Job, settings: Settings) -> bool:
    """Title passes and the location isn't known to be elsewhere.

    An unknown location still passes here; run() then reads the job page and
    calls resolve_location() to decide.
    """
    if not title_ok(job, settings):
        return False
    return location_verdict(job, settings) != "drop"


# ------------------------------------------------------------- Dutch language
_DUTCH = re.compile(r"(?<![a-z])(dutch|nederlands|nederlandse|neerlandais|niederlandisch)(?![a-z])")
# softer wording near the mention -> basic / intermediate / learning is fine
_LENIENT = re.compile(
    r"(?<![a-z])(a plus|plus|advantage|asset|bonus|nice to have|nice-to-have|preferred|preferably|desirable|"
    r"beneficial|not required|not necessary|not mandatory|not a must|no requirement|optional|basic|elementary|"
    r"beginner|intermediate|conversational|a1|a2|b1|b2|willing to learn|willingness to learn|eager to learn|"
    r"open to learning|learn dutch|learning dutch|pre|pluspunt|voordeel|geen vereiste|niet vereist|"
    r"niet noodzakelijk|geen must|geen eis|basiskennis|basisniveau|enige kennis|bereid|wil leren|te leren)(?![a-z])")
# clear "you must be fluent" wording near the mention
_STRICT = re.compile(
    r"(?<![a-z])(fluent|fluently|fluency|native|mother tongue|excellent|perfect|proficient|proficiency|"
    r"full professional|strong command|excellent command|c1|c2|must|required|requirement|mandatory|essential|"
    r"necessary|vloeiend|uitstekend|uitstekende|perfecte|moedertaal|moedertaalniveau|vereist|verplicht|"
    r"noodzakelijk|een must|in woord en geschrift|speak|spreek|spreekt|beheerst)(?![a-z])")


def dutch_requirement(text: str) -> str:
    """'lenient'  - the job accepts basic/intermediate Dutch or only prefers it
    'required' - the job clearly demands fluent / native Dutch
    ''         - no clear statement about Dutch"""
    if not text:
        return ""
    t = _fold(text.lower())
    lenient = strict = False
    for m in _DUTCH.finditer(t):
        before = t[max(0, m.start() - 45): m.start()]
        before = re.split(r"[.;!?]", before)[-1]          # stay inside the sentence
        after_short = re.split(r"[.;!?]", t[m.end(): m.end() + 35])[0]
        after_long = re.split(r"[.;!?]", t[m.end(): m.end() + 60])[0]   # "... en Engelse taal in woord en geschrift"
        if _LENIENT.search(before + " " + after_short):
            lenient = True
        elif _STRICT.search(before + " " + after_long):
            strict = True
    return "lenient" if lenient else ("required" if strict else "")


def description_fit(text: str, settings: Settings) -> str:
    """The first description keyword found in a job text ('' if none) - i.e. the
    reason a posting with a generic title ("Engineer", "Junior ...") is still for us."""
    if not text or not settings.description_keywords:
        return ""
    t = _fold(text.lower())
    for w in settings.description_keywords:
        if _contains_any(t, [_fold(w)]):
            return w
    return ""


def is_favourite(job: Job, settings: Settings) -> bool:
    return bool(settings.favourite_keywords) and _contains_any(job.title.lower(), settings.favourite_keywords)


def experience_ok(job: Job, settings: Settings) -> bool:
    if job.years is None:
        return settings.keep_if_experience_unknown
    return job.years <= settings.max_years_experience
