"""Transactional email via Resend API."""
import os
import logging
import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@canto-anki.duckdns.org")
APP_URL = os.getenv("APP_URL", "https://canto-anki.duckdns.org")


async def _send(to: str, subject: str, html: str) -> tuple[bool, str]:
    """Send an email via Resend. Returns (success, detail_message)."""
    if not RESEND_API_KEY:
        msg = "RESEND_API_KEY not configured"
        logger.warning("%s — skipping email to %s", msg, to)
        return False, msg
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html},
            )
            if resp.status_code >= 400:
                msg = f"Resend {resp.status_code}: {resp.text}"
                logger.error(msg)
                return False, msg
            logger.info("Email sent to %s (status %s)", to, resp.status_code)
            return True, "ok"
    except Exception as exc:
        msg = f"Network error: {exc}"
        logger.exception("Failed to send email to %s", to)
        return False, msg


def _base(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
  <h2 style="margin-bottom:8px">{title}</h2>
  {body}
  <hr style="margin-top:40px;border:none;border-top:1px solid #e5e7eb">
  <p style="font-size:12px;color:#888">
    You're receiving this because an account was created with this email address.
    If this wasn't you, you can ignore this email.
  </p>
</body></html>"""


async def send_verification(to: str, token: str, app_name: str = "Canto Anki") -> bool:
    url = f"{APP_URL}/verify-email?token={token}"
    html = _base(
        f"Verify your {app_name} account",
        f"""<p>Thanks for signing up! Click the button below to verify your email address.</p>
<p style="margin:24px 0">
  <a href="{url}" style="background:#1a73e8;color:#fff;padding:12px 24px;border-radius:8px;
     text-decoration:none;font-weight:600">Verify email</a>
</p>
<p style="font-size:13px;color:#666">Or copy this link: <a href="{url}">{url}</a></p>
<p style="font-size:13px;color:#666">This link expires in 24 hours.</p>""",
    )
    ok, _ = await _send(to, f"Verify your {app_name} account", html)
    return ok


async def send_password_reset(to: str, token: str, app_name: str = "Canto Anki") -> bool:
    url = f"{APP_URL}/reset-password?token={token}"
    html = _base(
        f"Reset your {app_name} password",
        f"""<p>We received a request to reset your password. Click the button below to choose a new one.</p>
<p style="margin:24px 0">
  <a href="{url}" style="background:#1a73e8;color:#fff;padding:12px 24px;border-radius:8px;
     text-decoration:none;font-weight:600">Reset password</a>
</p>
<p style="font-size:13px;color:#666">Or copy this link: <a href="{url}">{url}</a></p>
<p style="font-size:13px;color:#666">This link expires in 1 hour. If you didn't request this, ignore this email.</p>""",
    )
    ok, _ = await _send(to, f"Reset your {app_name} password", html)
    return ok
