import logging
import smtplib
from email.message import EmailMessage

from .config import Settings


logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, text: str, settings: Settings) -> None:
    if not settings.smtp_host:
        if settings.environment == "development":
            logger.warning("Development email to %s | %s\n%s", to, subject, text)
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
