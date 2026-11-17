"""Outbound channels: email, SMS, and internal notifications.

Every sender returns a short result string that is saved on the draft, so you can see
exactly what went out and through which route.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from .models import Lead

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str, *, thread_id: Optional[str] = None,
             in_reply_to: Optional[str] = None) -> str: ...


class SmsSender(Protocol):
    def send_sms(self, lead: Lead, text: str) -> str: ...


# --------------------------------------------------------------------------- email


class GmailMailer:
    def __init__(self, gmail_client, sender: str = ""):
        self.gmail = gmail_client
        self.sender = sender

    def send(self, to, subject, body, *, thread_id=None, in_reply_to=None) -> str:
        msg_id = self.gmail.send(to, subject, body, sender=self.sender, thread_id=thread_id, in_reply_to=in_reply_to)
        return f"gmail:{msg_id}"


class OutboxMailer:
    """Keeps sent mail in memory and logs it. Used in tests and before Gmail is connected."""

    def __init__(self):
        self.outbox: list[dict] = []

    def send(self, to, subject, body, *, thread_id=None, in_reply_to=None) -> str:
        self.outbox.append({"to": to, "subject": subject, "body": body, "thread_id": thread_id})
        log.info("EMAIL (outbox) to=%s subject=%s", to, subject)
        return f"outbox:{len(self.outbox)}"


# --------------------------------------------------------------------------- SMS


class TwilioSms:
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.sid, self.token, self.from_number = account_sid, auth_token, from_number

    def send_sms(self, lead: Lead, text: str) -> str:
        if not lead.contact.phone:
            return "skipped: no phone number"
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode({"To": lead.contact.phone, "From": self.from_number, "Body": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return f"twilio:{json.loads(resp.read())['sid']}"
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Twilio error {e.code}: {e.read().decode(errors='replace')[:300]}") from e


class GoogleVoiceEmailSms:
    """Text back through Google Voice by replying to the forwarded-text email.

    Google Voice forwards each text to Gmail from an address like
    14045551234.xxxx@txt.voice.google.com; a reply to that address is delivered as a
    text from your Google Voice number. This is a Google Voice feature, not an API,
    so it only works for people who texted you first and may change without notice.
    """

    def __init__(self, mailer: Mailer):
        self.mailer = mailer

    def send_sms(self, lead: Lead, text: str) -> str:
        if not lead.gv_reply_address:
            return "skipped: no Google Voice text thread (lead did not text in)"
        return "gvoice:" + self.mailer.send(lead.gv_reply_address, "", text)


class NoSms:
    def send_sms(self, lead: Lead, text: str) -> str:
        return "skipped: SMS not configured"


class RecordingSms:
    """Test double."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, lead: Lead, text: str) -> str:
        if not lead.contact.phone:
            return "skipped: no phone number"
        self.sent.append((lead.contact.phone, text))
        return f"recorded:{len(self.sent)}"
