"""Optional email via any SMTP server (Gmail App Password works, free). Sends the customer
a confirmation with an .ics invite (opens in Google/Apple/Outlook calendar) and alerts the owner."""
import logging
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import settings

log = logging.getLogger("notify")


def smtp_ready() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS)


def _send(to, subject, body, ics):
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.SMTP_USER, to, subject
        msg.set_content(body)
        if ics:
            msg.add_attachment(ics.encode(), maintype="text", subtype="calendar",
                               filename="appointment.ics", params={"method": "REQUEST"})
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(settings.SMTP_USER, settings.SMTP_PASS)
            s.send_message(msg)
    except Exception:
        log.exception("Email to %s failed", to)


def send_async(to: str, subject: str, body: str, ics: str | None = None) -> None:
    if to and smtp_ready():
        threading.Thread(target=_send, args=(to, subject, body, ics), daemon=True).start()


def make_ics(cfg: dict, name: str, b: dict) -> str:
    start = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(cfg["timezone"]))
    start_u = start.astimezone(timezone.utc)
    end_u = start_u + timedelta(minutes=cfg["appointment_minutes"])
    f = "%Y%m%dT%H%M%SZ"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Furniture Agent//EN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{b['id']}@furniture-agent", f"DTSTAMP:{datetime.now(timezone.utc).strftime(f)}",
        f"DTSTART:{start_u.strftime(f)}", f"DTEND:{end_u.strftime(f)}",
        f"SUMMARY:Showroom visit - {name}", f"DESCRIPTION:Interest: {b['product']}",
        "END:VEVENT", "END:VCALENDAR", ""])


def booking_emails(cfg: dict, business: str, b: dict) -> None:
    ics = make_ics(cfg, business, b)
    when = f"{b['date']} at {b['time']}"
    if b.get("email"):
        send_async(b["email"], f"Your visit to {business} is confirmed",
                   f"Hi {b['name']},\n\nYour showroom visit is booked for {when}.\nInterest: {b['product']}\n\nSee you then!\n{business}", ics)
    send_async(cfg.get("owner_email", ""), f"New booking: {b['name']} - {when}",
               f"Name: {b['name']}\nEmail: {b.get('email') or '-'}\nPhone: {b.get('phone') or '-'}\nWhen: {when}\nInterest: {b['product']}", ics)


def handoff_email(cfg: dict, business: str, reason: str, contact: str) -> None:
    send_async(cfg.get("owner_email", ""), f"[{business}] Customer needs a human",
               f"Reason: {reason}\nContact: {contact or 'not provided'}")
