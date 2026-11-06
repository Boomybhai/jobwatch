"""Diagnose companies that never yield jobs: rediscover their feed from scratch and show what's found.

    python scripts/diagnose.py "Cargill B.V." "IQatalyst B.V."      (or --zero for every 0-job company in state.json)
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobwatch.adapters import check_company, fetch_html, parse_html_jobs  # noqa: E402
from jobwatch.config import load_companies, load_settings  # noqa: E402
from jobwatch.discover import discover  # noqa: E402
from jobwatch.fetch import Fetcher  # noqa: E402


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings(ROOT / "config.yaml")
    companies = {c.name: c for c in load_companies(ROOT / "companies.csv")}
    names = [a for a in argv if not a.startswith("--")]
    if "--zero" in argv:
        state_path = ROOT / ("state.json" if (ROOT / "state.json").exists() else "seed/state.json")
        st = json.loads(state_path.read_text(encoding="utf-8"))["companies"]
        names += [n for n, c in companies.items() if c.enabled and not st.get(n, {}).get("seen")]
    f = Fetcher(settings, allow_render=True)
    try:
        for name in names:
            c = companies.get(name)
            if not c:
                print(f"\n### {name}: not in companies.csv")
                continue
            print(f"\n### {name}\n    csv url : {c.url}  (adapter={c.adapter}, render={c.render})")
            try:
                adapter, url = discover(f, c)
            except Exception as exc:
                print(f"    discover: ERROR {type(exc).__name__}: {str(exc)[:150]}")
                adapter, url = "html", c.url
            print(f"    discover: {adapter} {url}")
            res = check_company(f, dataclasses.replace(c, adapter=adapter, url=url), settings.max_jobs_per_api)
            print(f"    check   : {len(res.jobs)} jobs via {res.method}" + (f"  ERROR {res.error[:150]}" if res.error else ""))
            for j in res.jobs[:4]:
                print(f"        - {j.title[:70]}  {j.url[:110]}")
            if not res.jobs:  # what does the page look like when a browser renders it?
                try:
                    html = f.render(c.url)
                    final = getattr(f, "last_render_url", None) or c.url
                    jobs = parse_html_jobs(html, final, c.name)
                    print(f"    rendered: {len(jobs)} job-like links on {final}")
                    for j in jobs[:4]:
                        print(f"        - {j.title[:70]}  {j.url[:110]}")
                except Exception as exc:
                    print(f"    rendered: ERROR {type(exc).__name__}: {str(exc)[:150]}")
            sys.stdout.flush()
    finally:
        f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
