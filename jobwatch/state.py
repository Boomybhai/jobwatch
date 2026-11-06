"""Remember which jobs we've already seen, per company."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import Job

MAX_SEEN = 20000  # per company; sitemaps can list thousands of jobs worldwide
RECENT_DAYS = 3   # on a (re)baseline, still report jobs posted this recently

_REL_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def posted_age_days(posted: str, now: datetime | None = None) -> float | None:
    """How many days ago a job was posted, from the many formats job systems use.

    "2026-10-12", "2026-10-12T18:31:29+00:00", "Posted Today", "Posted 3 Days Ago",
    "Mon, 12 Oct 2026 10:00:00 GMT", "1791471702" (unix seconds). None = unknown.
    """
    if not posted:
        return None
    now = now or datetime.now(timezone.utc)
    txt = str(posted).strip()
    m = _REL_RE.search(txt)
    if m:
        if m.group(1).lower() == "today":
            return 0.0
        if m.group(1).lower() == "yesterday":
            return 1.0
        return 99.0 if "+" in txt else float(m.group(2))
    if txt.isdigit() and len(txt) >= 9:
        ts = int(txt) / (1000 if len(txt) >= 12 else 1)
        return (now - datetime.fromtimestamp(ts, timezone.utc)).total_seconds() / 86400
    dt = None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(txt)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"companies": {}, "runs": 0}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("companies", {})
            self.data.setdefault("runs", 0)

    def is_baselined(self, company: str) -> bool:
        return company in self.data["companies"]

    def new_jobs(self, company: str, jobs: list[Job], source: str | None = None) -> list[Job]:
        """Return jobs not seen before and record all current ones.

        The first time a company is checked we only record (baseline) so you
        don't get an email with every existing vacancy - except jobs the site
        says were posted in the last RECENT_DAYS days, which are still reported.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = self.data["companies"].setdefault(company, {"seen": {}, "first_checked": now})
        if source and entry.get("source") and entry["source"] != source:
            # careers URL changed (e.g. switched to the company's job API):
            # re-baseline so the switch doesn't look like hundreds of new jobs
            entry["seen"], entry["baselined"] = {}, False
        if source:
            entry["source"] = source
        seen: dict = entry["seen"]
        baseline = entry.get("baselined", False) is False
        fresh = []
        for job in jobs:
            if job.id not in seen:
                seen[job.id] = now
                if not baseline:
                    fresh.append(job)
                else:
                    age = posted_age_days(job.posted)
                    if age is not None and -1 <= age <= RECENT_DAYS:
                        fresh.append(job)  # brand-new posting found while (re)baselining
        entry["baselined"] = True
        entry["last_ok"] = now
        entry.pop("last_error", None)
        entry["consecutive_errors"] = 0
        # keep the file from growing forever
        if len(seen) > MAX_SEEN:
            for key in sorted(seen, key=seen.get)[: len(seen) - MAX_SEEN]:
                del seen[key]
        return fresh

    # ------------------------------------------------ jobs already shown to you
    def remember_shown(self, jobs: list[Job], keep_hours: int = 48) -> None:
        """Keep the relevant jobs we alerted about, so later alerts can repeat them."""
        now = datetime.now(timezone.utc)
        shown = self.data.setdefault("shown", [])
        have = {(x["company"], x["id"]) for x in shown}
        for j in jobs:
            if (j.company, j.id) not in have:
                shown.append({"company": j.company, "id": j.id, "title": j.title, "url": j.url,
                              "location": j.location, "posted": j.posted, "years": j.years,
                              "favourite": j.favourite, "fit": j.fit, "dutch": j.dutch, "found": now.isoformat(timespec="seconds")})
        cutoff = now.timestamp() - keep_hours * 3600
        self.data["shown"] = [x for x in shown if datetime.fromisoformat(x["found"]).timestamp() >= cutoff]

    def shown_within(self, hours: int, exclude: set | None = None) -> list[Job]:
        """Jobs alerted about in the last `hours` hours (newest first), as repeat=True Jobs."""
        if hours <= 0:
            return []
        now = datetime.now(timezone.utc)
        exclude = exclude or set()
        out = []
        for x in sorted(self.data.get("shown", []), key=lambda x: x["found"], reverse=True):
            found = datetime.fromisoformat(x["found"])
            if (now - found).total_seconds() > hours * 3600 or (x["company"], x["id"]) in exclude:
                continue
            out.append(Job(company=x["company"], title=x["title"], url=x["url"], location=x.get("location", ""),
                           posted=x.get("posted", ""), id=x["id"], years=x.get("years"),
                           favourite=bool(x.get("favourite")), fit=x.get("fit", ""), dutch=x.get("dutch", ""), repeat=True,
                           found=found.astimezone().strftime("%a %H:%M")))
        return out

    def cache(self, company: str) -> dict:
        """Per-company scratch space (e.g. the discovered job feed)."""
        return self.data["companies"].setdefault(company, {"seen": {}})

    def record_error(self, company: str, error: str) -> int:
        entry = self.data["companies"].setdefault(company, {"seen": {}})
        entry["last_error"] = error[:300]
        entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
        return entry["consecutive_errors"]

    def bump_run(self) -> int:
        self.data["runs"] += 1
        return self.data["runs"]

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
