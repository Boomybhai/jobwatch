"""Plain data classes shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Company:
    name: str
    url: str
    priority: str = ""
    group: str = ""
    link_type: str = ""
    enabled: bool = True
    adapter: str = "auto"      # auto | workday | greenhouse | lever | recruitee | smartrecruiters | workable | personio | oracle | sitemap | html
    render: str = "auto"       # auto | never | always  (Playwright for JS-heavy pages)
    note: str = ""


@dataclass
class Job:
    company: str
    title: str
    url: str
    location: str = ""
    posted: str = ""
    id: str = ""
    years: int | None = None      # min. years of experience asked (None = not stated)
    favourite: bool = False       # title matches a favourite keyword
    description: str = ""         # job text, when already read from the job page
    repeat: bool = False          # already shown in an earlier alert (within repeat_hours)
    found: str = ""               # when jobwatch first showed it ("2026-10-13 15:05")
    fit: str = ""                 # description phrase that made a generic title count ("chemical engineering")
    dutch: str = ""               # "" | "basic Dutch OK" (description accepts basic/intermediate Dutch)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.url or self.title


@dataclass
class CheckResult:
    company: Company
    jobs: list[Job] = field(default_factory=list)
    error: str = ""
    method: str = ""
