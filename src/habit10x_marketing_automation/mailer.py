"""Send email: Resend HTTP API if RESEND_API_KEY is set, otherwise SMTP (e.g. Gmail app password)."""

import smtplib
from email.message import EmailMessage

import httpx

from .config import MissingSettingError, Settings

RESEND_API_URL = "https://api.resend.com"


class MailError(RuntimeError):
    pass


def send_email(
    settings: Settings,
    subject: str,
    html: str,
    text: str,
    to: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Send one email to `to` (default: ALERT_RECIPIENTS). Returns a message id (empty for SMTP)."""
    settings.require("alert_from_email")
    recipients = to or settings.recipients
    if not recipients:
        raise MissingSettingError("Missing in .env: ALERT_RECIPIENTS")
    if settings.resend_api_key and settings.resend_api_key.get_secret_value():
        return _send_resend(settings, recipients, subject, html, text, transport)
    if settings.smtp_host:
        return _send_smtp(settings, recipients, subject, html, text)
    raise MissingSettingError("Missing in .env: RESEND_API_KEY (or SMTP_HOST, SMTP_USER, SMTP_PASSWORD)")


def _send_resend(settings, recipients, subject, html, text, transport) -> str:
    with httpx.Client(
        base_url=RESEND_API_URL,
        headers={"Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}"},
        timeout=30,
        transport=transport or httpx.HTTPTransport(retries=2),
    ) as client:
        response = client.post(
            "/emails",
            json={"from": settings.alert_from_email, "to": recipients, "subject": subject, "html": html, "text": text},
        )
    if response.status_code >= 400:
        raise MailError(f"{response.status_code} from Resend: {response.text[:400]}")
    return response.json().get("id", "")


def _send_smtp(settings, recipients, subject, html, text) -> str:
    settings.require("smtp_user", "smtp_password")
    message = EmailMessage()
    message["From"] = settings.alert_from_email
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls()
        with server:
            server.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            "SMTP login rejected. For Gmail use a 16-character App Password (needs 2-Step Verification), "
            f"not your normal password. Server said: {exc.smtp_code} {exc.smtp_error!r}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc
    return ""
