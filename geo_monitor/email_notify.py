"""SMTP email notifications for geo disruption alerts."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Optional

log = logging.getLogger("geo_monitor.email")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def email_configured() -> bool:
    return bool(_env("ALERT_EMAIL_TO") and _env("SMTP_HOST") and _env("SMTP_FROM"))


def send_email(subject: str, body: str) -> None:
    """Send one plain-text email to ALERT_EMAIL_TO over TLS. Raises on failure."""
    to_addr = _env("ALERT_EMAIL_TO")
    from_addr = _env("SMTP_FROM")
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")

    if not (to_addr and from_addr and host):
        raise RuntimeError(
            "Email not configured — set ALERT_EMAIL_TO, SMTP_FROM, SMTP_HOST"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            # Never authenticate or deliver over a downgraded plaintext
            # connection. Any STARTTLS failure is a hard delivery failure.
            smtp.starttls(context=context)
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    log.info("Email sent to %s — %s", to_addr, subject[:80])


def send_alert_email(alert: dict[str, Any]) -> None:
    """Send one disruption alert email. Raises on failure."""
    severity = str(alert.get("severity") or "?").upper()
    title = alert.get("title") or "(no title)"
    tickers = alert.get("affected_tickers") or []
    tickers_s = ", ".join(tickers) if tickers else "—"

    subject = f"[STOCKIFY {severity}] {title[:120]}"
    body = f"""STOCKIFY geo disruption alert

Severity: {severity} (score {alert.get('disruption_score', '?')}/100)
Scope: {alert.get('scope', '—')}
Affected tickers: {tickers_s}

Headline: {title}
Source: {alert.get('source', '—')}
Published: {alert.get('published_at', '—')}
URL: {alert.get('url', '—')}

Why disruptive:
{alert.get('why_disruptive') or '—'}

Investor action:
{alert.get('investor_action') or '—'}

Confidence: {alert.get('confidence', '—')}
Alert id: {alert.get('id', '—')}

— STOCKIFY geo monitor
"""

    send_email(subject, body)


def try_send_alert_email(alert: dict[str, Any]) -> Optional[str]:
    """Send if configured; return error string or None on success/skip."""
    if not email_configured():
        return "email not configured (skipped)"
    try:
        send_alert_email(alert)
        return None
    except Exception as e:
        log.exception("Failed to send alert email")
        return str(e)
