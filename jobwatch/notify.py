"""Send notifications: email (SMTP) and desktop pop-ups."""
from __future__ import annotations

import os as _os


def app_name() -> str:
    """'jobwatch', or e.g. 'jobwatch Belgium' when config.yaml sets a label."""
    label = _os.environ.get("JOBWATCH_LABEL", "").strip()
    return f"jobwatch {label}" if label else "jobwatch"


import html
import logging
import os
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage

from .models import Job

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ format
def experience_label(j: Job) -> str:
    if j.years is None:
        return ""
    return "starter / 0 yrs" if j.years == 0 else f"asks {j.years}+ yrs"


def display_title(j: Job) -> str:
    return ("⭐ " if j.favourite else "") + j.title


def meta(j: Job) -> str:
    fit = f"description: {j.fit}" if j.fit else ""
    return " · ".join(x for x in [j.location, experience_label(j), j.dutch, fit, j.posted] if x)


def group_by_company(jobs: list[Job]) -> dict[str, list[Job]]:
    out: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        out[j.company].append(j)
    # companies with a favourite posting first, then alphabetical; favourites first within a company
    for js in out.values():
        js.sort(key=lambda j: not j.favourite)
    return dict(sorted(out.items(), key=lambda kv: (not any(j.favourite for j in kv[1]), kv[0].lower())))


def build_email(jobs: list[Job], errors: list[tuple[str, str]] | None = None,
                repeats: list[Job] | None = None) -> tuple[str, str, str]:
    """Return (subject, plain_text, html_body). `repeats` = jobs already shown earlier, listed below the new ones."""
    grouped = group_by_company(jobs)
    n, c = len(jobs), len(grouped)
    subject = f"{app_name()}: {n} new posting{'s' if n != 1 else ''} at {c} compan{'ies' if c != 1 else 'y'}"
    fav = sum(j.favourite for j in jobs)
    if fav:
        subject = f"⭐ {subject} ({fav} favourite{'s' if fav != 1 else ''})"
    if not jobs and errors:
        subject = f"{app_name()}: {len(errors)} careers page(s) need attention"

    lines, parts = [], []
    for company, items in grouped.items():
        lines.append(company)
        parts.append(f"<h3 style='margin:18px 0 6px;font-family:Arial'>{html.escape(company)}</h3><ul>")
        for j in items:
            m = meta(j)
            lines.append(f"  - {display_title(j)}{' (' + m + ')' if m else ''}\n    {j.url}")
            parts.append(
                f"<li style='font-family:Arial;margin:4px 0'><a href='{html.escape(j.url)}'>"
                f"{html.escape(display_title(j))}</a>"
                + (f" <span style='color:#666'>— {html.escape(m)}</span>" if m else "")
                + "</li>"
            )
        parts.append("</ul>")
        lines.append("")

    if repeats:
        lines.append(f"Already shown in the last 24 hours ({len(repeats)}):")
        parts.append("<h3 style='margin:24px 0 6px;font-family:Arial;color:#555'>"
                     f"Already shown in the last 24 hours ({len(repeats)})</h3><ul>")
        for j in repeats:
            m = " · ".join(x for x in [j.company, meta(j), f"first shown {j.found}" if j.found else ""] if x)
            lines.append(f"  - {display_title(j)} ({m})\n    {j.url}")
            parts.append(f"<li style='font-family:Arial;margin:4px 0;color:#555'><a href='{html.escape(j.url)}'>"
                         f"{html.escape(display_title(j))}</a> <span style='color:#888'>— {html.escape(m)}</span></li>")
        parts.append("</ul>")
        lines.append("")
        subject += f" (+{len(repeats)} from the last 24h)"

    if errors:
        lines.append("Careers pages that keep failing (check the link in companies.csv):")
        parts.append("<h4 style='font-family:Arial;color:#a33'>Careers pages that keep failing</h4><ul>")
        for name, err in errors:
            lines.append(f"  - {name}: {err}")
            parts.append(f"<li style='font-family:Arial;color:#666'>{html.escape(name)}: {html.escape(err)}</li>")
        parts.append("</ul>")

    return subject, "\n".join(lines), "".join(parts)


# ------------------------------------------------------------------- email
def send_email(subject: str, text: str, html_body: str) -> bool:
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "465")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to = os.getenv("EMAIL_TO") or user
    if not (user and password and to):
        log.warning("Email not sent: set SMTP_USER, SMTP_PASSWORD (and optionally EMAIL_TO).")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
    log.info("Email sent to %s", to)
    return True


# ----------------------------------------------------------------- desktop
def desktop_notify(jobs: list[Job], repeats: list[Job] | None = None) -> None:
    grouped = group_by_company(jobs)
    try:
        from plyer import notification
    except ImportError:
        log.warning("Desktop pop-ups need 'plyer' (pip install plyer).")
        return
    # One pop-up per job (max 10), then a summary.
    ordered = [j for js in grouped.values() for j in js]
    for j in ordered[:10]:
        _popup(notification, f"{'⭐ ' if j.favourite else ''}{j.company}",
               j.title + (f" ({meta(j)})" if meta(j) else ""))
    if len(ordered) > 10:
        rest = ordered[10:]
        _popup(notification, app_name(), f"+{len(rest)} more new postings at "
                                          f"{len({j.company for j in rest})} companies - full list in new-jobs.html")
    if repeats:
        _popup(notification, f"{app_name()}: still open from the last 24h",
               "; ".join(f"{j.company}: {j.title}" for j in repeats[:6])
               + (f" (+{len(repeats) - 6} more)" if len(repeats) > 6 else ""))


def _popup(notification, title: str, message: str) -> None:
    try:
        notification.notify(title=title[:63], message=message[:255], app_name=app_name(), timeout=15)
    except Exception as exc:  # no notification daemon (e.g. on a server)
        log.warning("Desktop notification failed: %s", exc)


# ------------------------------------------------------------- phone (ntfy)
MAX_PHONE_PUSHES = 200   # ntfy.sh allows ~250 messages a day for free
PUSH_GAP = 1.0           # seconds between pushes, to stay under ntfy's rate limit

def phone_notify(jobs: list[Job], title_prefix: str = "", repeats: list[Job] | None = None) -> bool:
    """Push to your phone with the free ntfy app (https://ntfy.sh). No account needed.

    Set NTFY_TOPIC (in email.env or as an environment variable) to a private,
    hard-to-guess channel name and subscribe to the same name in the ntfy app.
    """
    import requests

    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    server = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    if not topic:
        log.warning("Phone notification not sent: set NTFY_TOPIC in email.env.")
        return False
    # One push per job, for every relevant posting, so each one opens its own link.
    import time
    ordered = sorted(jobs, key=lambda j: (not j.favourite, j.company.lower()))
    sent = 0
    for j in ordered[:MAX_PHONE_PUSHES]:
        body = display_title(j) + (f" ({meta(j)})" if meta(j) else "")
        payload = {
            "topic": topic,
            "title": f"{title_prefix}{'⭐ ' if j.favourite else ''}{j.company}"[:120],
            "message": f"{body}\n{j.url}",
            "click": j.url,
            "tags": ["briefcase"],
            "priority": 4 if j.favourite else 3,
        }
        for attempt in range(4):
            try:
                r = requests.post(server, json=payload, timeout=20)
                if getattr(r, "status_code", 200) == 429:  # ntfy rate limit -> slow down
                    time.sleep(15 * (attempt + 1))
                    continue
                r.raise_for_status()
                sent += 1
                break
            except Exception as exc:
                log.warning("Phone notification failed for %s: %s", j.title, exc)
                time.sleep(5)
        if PUSH_GAP:
            time.sleep(PUSH_GAP)
    if len(ordered) > MAX_PHONE_PUSHES:
        rest = len(ordered) - MAX_PHONE_PUSHES
        try:
            requests.post(server, json={"topic": topic, "title": f"{title_prefix}{app_name()}",
                                        "message": f"+{rest} more new postings - full list in new-jobs.html "
                                                   "in the jobwatch folder"}, timeout=20)
        except Exception as exc:
            log.warning("Phone notification failed: %s", exc)
    if repeats:
        # one push (not one per job) so the same postings don't flood your phone every 3 hours
        lines = [f"{'⭐ ' if j.favourite else ''}{j.company}: {j.title} (first shown {j.found})" for j in repeats[:25]]
        if len(repeats) > 25:
            lines.append(f"+{len(repeats) - 25} more - see the email or new-jobs.html")
        try:
            requests.post(server, json={"topic": topic, "title": f"{title_prefix}Still open from the last 24h ({len(repeats)})",
                                        "message": "\n".join(lines), "tags": ["repeat"], "priority": 2}, timeout=20)
        except Exception as exc:
            log.warning("Phone notification failed: %s", exc)
    log.info("Sent %d phone notification(s) to ntfy topic '%s'", sent, topic)
    return sent > 0


# -------------------------------------------------------- list on the laptop
def save_list(jobs: list[Job], folder, keep_days: int = 30) -> None:
    """Add this run's postings to new-jobs.html (newest first) in the jobwatch folder."""
    import json as _json
    from datetime import datetime, timedelta
    from pathlib import Path

    folder = Path(folder)
    hist_path = folder / "new-jobs.json"
    try:
        history = _json.loads(hist_path.read_text(encoding="utf-8"))
    except Exception:
        history = []
    now = datetime.now()
    history = [{"found": now.strftime("%Y-%m-%d %H:%M"), "company": j.company, "title": display_title(j),
                "url": j.url, "meta": meta(j)} for j in jobs] + history
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    history = [h for h in history if h["found"] >= cutoff]
    hist_path.write_text(_json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")

    rows, last = [], None
    for h in history:
        if h["found"] != last:
            rows.append(f"<h2>Found {html.escape(h['found'])}</h2>")
            last = h["found"]
        rows.append(f"<p><b>{html.escape(h['company'])}</b> — <a href='{html.escape(h['url'])}'>"
                    f"{html.escape(h['title'])}</a> <span>{html.escape(h['meta'])}</span></p>")
    page = (f"<!doctype html><meta charset='utf-8'><title>{html.escape(app_name())} - new jobs</title>"
            "<style>body{font-family:Arial;max-width:900px;margin:24px auto;padding:0 16px}"
            "h2{font-size:15px;color:#555;margin-top:24px;border-bottom:1px solid #ddd}"
            "p{margin:6px 0}span{color:#777;font-size:13px}</style>"
            f"<h1>{html.escape(app_name())}: new postings (last {keep_days} days)</h1>" + "".join(rows))
    (folder / "new-jobs.html").write_text(page, encoding="utf-8")
