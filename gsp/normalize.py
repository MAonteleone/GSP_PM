"""Turn raw channel payloads into InboundMessage.

Handles:
  * normal emails to the business inbox
  * Google Voice texts, voicemails and missed calls that Google Voice forwards to Gmail
  * Twilio inbound SMS webhooks
"""

from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Mapping, Optional

from .models import Channel, InboundMessage

GV_TEXT_DOMAIN = "txt.voice.google.com"
GV_SENDER = "voice-noreply@google.com"

_PHONE_RE = re.compile(r"\(?\+?1?\)?[\s.-]*\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})")

# Lines after which Google Voice puts its footer.
_GV_FOOTER_MARKERS = (
    "YOUR ACCOUNT",
    "To respond to this text message, reply to this email",
    "Google LLC",
    "This email was sent to you because",
    "Play message",
    "HELP CENTER",
)

# Senders and headers that are never leads; skipped before any AI call.
_SKIP_SENDER_PATTERNS = re.compile(
    r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?@|newsletter|bounce)", re.I
)


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Return a US number in E.164 form (+14045551234), or None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"


def find_phone(text: str) -> Optional[str]:
    m = _PHONE_RE.search(text or "")
    return normalize_phone("".join(m.groups())) if m else None


def _strip_gv_footer(body: str) -> str:
    lines = []
    for line in body.splitlines():
        if any(marker.lower() in line.lower() for marker in _GV_FOOTER_MARKERS):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_quoted_reply(body: str) -> str:
    """Drop the quoted history under a reply so Claude sees only the new text."""
    out = []
    for line in body.splitlines():
        if re.match(r"^On .+ wrote:\s*$", line.strip()) or line.strip().startswith(">"):
            break
        out.append(line)
    return "\n".join(out).strip()


def from_gmail(
    *,
    message_id: str,
    thread_id: Optional[str],
    headers: Mapping[str, str],
    body: str,
    business_email: str = "",
) -> Optional[InboundMessage]:
    """Build an InboundMessage from a Gmail message. Returns None when it should be skipped."""
    h = {k.lower(): v for k, v in headers.items()}
    from_name, from_addr = parseaddr(h.get("from", ""))
    from_addr = from_addr.lower()
    subject = h.get("subject", "") or ""
    msg_id_header = h.get("message-id")

    # ---- Google Voice forwarded text
    if from_addr.endswith("@" + GV_TEXT_DOMAIN):
        local = from_addr.split("@")[0]
        phone = normalize_phone(local.split(".")[0]) or find_phone(subject)
        name_match = re.search(r"New text message from (.+)$", subject)
        name = None
        if name_match and not find_phone(name_match.group(1)):
            name = name_match.group(1).strip()
        return InboundMessage(
            channel=Channel.sms,
            source="gvoice",
            external_id=f"gmail:{message_id}",
            from_name=name or (from_name if from_name and not find_phone(from_name) else None),
            from_phone=phone,
            subject=subject,
            body=_strip_gv_footer(_strip_quoted_reply(body)),
            gv_reply_address=from_addr,
            gmail_thread_id=thread_id,
            gmail_message_id_header=msg_id_header,
        )

    # ---- Google Voice voicemail / missed call
    if from_addr == GV_SENDER:
        if subject.lower().startswith("new voicemail"):
            channel = Channel.voicemail
        elif "missed call" in subject.lower():
            channel = Channel.missed_call
        else:
            return None  # other Google Voice notices
        name_match = re.search(r"from (.+?)(?: at \d|$)", subject)
        caller = name_match.group(1).strip() if name_match else None
        phone = find_phone(subject) or find_phone(body)
        transcript = _strip_gv_footer(body)
        if channel == Channel.missed_call:
            transcript = "(Missed call. No voicemail was left.)"
        return InboundMessage(
            channel=channel,
            source="gvoice",
            external_id=f"gmail:{message_id}",
            from_name=caller if caller and not find_phone(caller) else None,
            from_phone=phone,
            subject=subject,
            body=transcript,
            gmail_thread_id=thread_id,
            gmail_message_id_header=msg_id_header,
        )

    # ---- skip obvious non-leads before spending an AI call
    if business_email and from_addr == business_email.lower():
        return None
    if _SKIP_SENDER_PATTERNS.search(from_addr):
        return None
    if h.get("auto-submitted", "no").lower() != "no" or "list-unsubscribe" in h or h.get("precedence", "").lower() in {
        "bulk",
        "list",
        "junk",
    }:
        return None

    clean = _strip_quoted_reply(body)
    return InboundMessage(
        channel=Channel.email,
        source="gmail",
        external_id=f"gmail:{message_id}",
        from_name=from_name or None,
        from_email=from_addr or None,
        from_phone=find_phone(clean),
        subject=subject,
        body=clean,
        gmail_thread_id=thread_id,
        gmail_message_id_header=msg_id_header,
    )


def from_twilio(form: Mapping[str, str]) -> InboundMessage:
    return InboundMessage(
        channel=Channel.sms,
        source="twilio",
        external_id=f"twilio:{form.get('MessageSid', '')}",
        from_phone=normalize_phone(form.get("From")),
        body=form.get("Body", "") or "",
    )
