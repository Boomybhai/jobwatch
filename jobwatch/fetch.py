"""HTTP fetching: polite delays, robots.txt, optional JavaScript rendering."""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests

from .config import Settings

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


# Some sites (Akamai/Cloudflare bot rules) refuse non-browser identities with 403/406.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_STATUSES = (403, 406)


class Fetcher:
    def __init__(self, settings: Settings, allow_render: bool = True):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
            }
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request = 0.0
        self.allow_render = allow_render
        self._pw = None
        self._browser = None

    # ---------------------------------------------------------------- helpers
    def _wait(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.settings.request_delay_seconds:
            time.sleep(self.settings.request_delay_seconds - gap)
        self._last_request = time.monotonic()

    def _check_robots(self, url: str) -> None:
        if not self.settings.respect_robots_txt:
            return
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(base + "/robots.txt", timeout=10)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None  # no robots.txt -> allowed
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        if rp is not None and not rp.can_fetch(self.settings.user_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    # ------------------------------------------------------------------- API
    def get(self, url: str, **kw) -> requests.Response:
        self._check_robots(url)
        self._wait()
        resp = self.session.get(url, timeout=self.settings.timeout_seconds, **kw)
        if resp.status_code in RETRY_STATUSES:
            headers = {**kw.pop("headers", {}), "User-Agent": BROWSER_UA}
            self._wait()
            resp = self.session.get(url, timeout=self.settings.timeout_seconds, headers=headers, **kw)
        resp.raise_for_status()
        _fix_encoding(resp)
        return resp

    def post_json(self, url: str, payload: dict) -> dict:
        # JSON APIs (Workday etc.) are what the careers page itself calls.
        self._wait()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        resp = self.session.post(url, json=payload, timeout=self.settings.timeout_seconds, headers=headers)
        if resp.status_code in RETRY_STATUSES:
            self._wait()
            resp = self.session.post(url, json=payload, timeout=self.settings.timeout_seconds,
                                     headers={**headers, "User-Agent": BROWSER_UA})
        resp.raise_for_status()
        return resp.json()

    def get_json(self, url: str, **kw) -> dict:
        self._wait()
        resp = self.session.get(
            url, timeout=self.settings.timeout_seconds, headers={"Accept": "application/json"}, **kw
        )
        if resp.status_code in RETRY_STATUSES:
            self._wait()
            resp = self.session.get(url, timeout=self.settings.timeout_seconds,
                                    headers={"Accept": "application/json", "User-Agent": BROWSER_UA}, **kw)
        resp.raise_for_status()
        return resp.json()

    def render(self, url: str) -> str:
        """Return the HTML after JavaScript has run (needs Playwright)."""
        if not self.allow_render:
            raise RuntimeError("rendering disabled")
        self._check_robots(url)
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # optional dependency

            self._pw = sync_playwright().start()
            self._browser = self._launch_browser()
        self._wait()
        page = self._browser.new_page(user_agent=self.settings.user_agent)
        try:
            # "networkidle" never happens on sites with chat widgets/trackers -> timeouts.
            page.goto(url, timeout=self.settings.timeout_seconds * 1000 * 2, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # good enough - the job list is usually there by now
            page.wait_for_timeout(1500)
            self.last_render_url = page.url  # after redirects, for resolving relative links
            return page.content()
        finally:
            page.close()

    def _launch_browser(self):
        """Use Playwright's own Chromium if installed, else Edge/Chrome already on the PC."""
        errors = []
        for channel in (None, "msedge", "chrome"):
            try:
                if channel:
                    return self._pw.chromium.launch(channel=channel)
                return self._pw.chromium.launch()
            except Exception as exc:  # browser not installed -> try the next one
                errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
        self.allow_render = False
        raise RuntimeError("no browser for JavaScript pages (" + "; ".join(errors) + ")")

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None


def _fix_encoding(resp: requests.Response) -> None:
    """Decode pages that don't name their charset as UTF-8 when they are UTF-8.

    Without a charset in Content-Type, requests falls back to ISO-8859-1 for text/*.
    That turns a UTF-8 byte-order mark into 'ï»¿' (so XML sitemaps parse as empty -
    Radancy/TalentBrew job sitemaps all start with one) and mangles é, ë, ü in titles.
    """
    ctype = resp.headers.get("Content-Type", "").lower()
    if "charset=" in ctype:
        return
    body = resp.content[:200000]
    if body.startswith(b"\xef\xbb\xbf"):
        resp.encoding = "utf-8-sig"
        return
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        cut_off = len(body) == 200000 and exc.start >= len(body) - 4  # a character split at the 200 kB cut
        if not cut_off:
            return  # really not UTF-8 (e.g. Latin-1): leave it as it is
    resp.encoding = "utf-8"
