"""SMS (Twilio) and email (Gmail SMTP) alerts. Failures are logged, never raised."""
import smtplib
from email.message import EmailMessage

import requests

from . import config, store


def sms(text: str) -> bool:
    if not (config.TWILIO_SID and config.TWILIO_TOKEN and config.TWILIO_FROM and config.ALERT_SMS_TO):
        return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/Messages.json",
            data={"From": config.TWILIO_FROM, "To": config.ALERT_SMS_TO, "Body": text[:1500]},
            auth=(config.TWILIO_SID, config.TWILIO_TOKEN), timeout=20)
        if r.status_code >= 300:
            store.log("error", f"SMS failed {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        store.log("error", f"SMS failed: {e}")
        return False


def email(subject: str, body: str, html: str | None = None) -> bool:
    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and config.ALERT_EMAIL_TO):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[TradeAgent] {subject}"
        msg["From"] = config.GMAIL_USER
        msg["To"] = config.ALERT_EMAIL_TO
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        store.log("error", f"email failed: {e}")
        return False


def both(subject: str, short: str, long: str) -> None:
    sms(short)
    email(subject, long)
