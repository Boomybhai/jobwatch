"""Load settings (config.yaml) and the company list (companies.csv)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Company

TRUE = {"yes", "y", "true", "1"}


@dataclass
class Settings:
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    favourite_keywords: list[str] = field(default_factory=list)
    only_favourites: bool = False
    check_description: bool = True      # with only_favourites: read generic-title jobs' descriptions
    description_keywords: list[str] = field(default_factory=list)  # ...and keep them if one of these appears
    skip_if_fluent_dutch_required: bool = False  # drop jobs whose description clearly demands fluent Dutch
    check_experience: bool = True
    max_years_experience: int = 5
    keep_if_experience_unknown: bool = True
    location_keywords: list[str] = field(default_factory=list)
    foreign_location_keywords: list[str] = field(default_factory=list)
    keep_unmatched_locations: bool = True  # False = only alert when the job is placed in a wanted country
    request_delay_seconds: float = 2.0
    timeout_seconds: float = 25.0
    user_agent: str = "jobwatch/1.0 (personal job alert script)"
    respect_robots_txt: bool = True
    max_jobs_per_api: int = 200
    min_priority: str = "Low"           # Low | Medium | High
    notify_errors_every_runs: int = 0   # 0 = never include error list in emails
    label: str = ""                     # e.g. "Belgium": shown in notification titles
    repeat_hours: int = 24              # repeat jobs already shown this many hours back (0 = off)


def load_settings(path: str | Path) -> Settings:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    s = Settings()
    for key, value in data.items():
        if hasattr(s, key):
            setattr(s, key, value)
    s.include_keywords = [k.lower() for k in s.include_keywords]
    s.exclude_keywords = [k.lower() for k in s.exclude_keywords]
    s.favourite_keywords = [k.lower() for k in s.favourite_keywords]
    s.description_keywords = [k.lower() for k in (s.description_keywords or [])]
    s.location_keywords = [k.lower() for k in s.location_keywords]
    s.foreign_location_keywords = [k.lower() for k in s.foreign_location_keywords]
    import os
    os.environ["JOBWATCH_LABEL"] = str(s.label or "")
    return s


def load_companies(path: str | Path) -> list[Company]:
    companies: list[Company] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("url") or "").strip()
            companies.append(
                Company(
                    name=row["name"].strip(),
                    url=url,
                    priority=(row.get("priority") or "").strip(),
                    group=(row.get("group") or "").strip(),
                    link_type=(row.get("link_type") or "").strip(),
                    enabled=(row.get("enabled") or "yes").strip().lower() in TRUE and bool(url),
                    adapter=(row.get("adapter") or "auto").strip().lower() or "auto",
                    render=(row.get("render") or "auto").strip().lower() or "auto",
                    note=(row.get("note") or "").strip(),
                )
            )
    return companies


PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def passes_priority(company: Company, min_priority: str) -> bool:
    want = PRIORITY_RANK.get(min_priority.lower(), 0)
    have = PRIORITY_RANK.get(company.priority.lower(), 0)
    return have >= want
