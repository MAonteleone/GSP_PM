"""Stage 1 - Inception.

inbound message -> find/create lead -> Claude extraction -> Claude reply draft
  -> guardrail checks -> send (or hold for one-tap approval) -> wait for booking
  -> booking webhook -> "call_scheduled" + call-prep note for Stage 2.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .ai import AIRefusal, LeadAI
from .config import Settings
from .models import (
    Booking,
    Channel,
    Draft,
    DraftStatus,
    InboundMessage,
    Lead,
    LeadExtraction,
    LeadStatus,
    utcnow,
)
from .senders import Mailer, SmsSender
from .store import Store

log = logging.getLogger(__name__)

PLACEHOLDER = "{BOOKING_LINK}"
MAX_SMS = 320
_MONEY = re.compile(r"\$\s?\d|\d+\s?(?:dollars|cents)\b|\b\d{1,2}\s?%", re.I)
_URL = re.compile(r"https?://\S+", re.I)

REMINDER_SMS = (
    "Hi {first}, just checking in from {business}. If you'd still like to talk solar, "
    "you can grab a call time here: {link}"
)
REMINDER_EMAIL_SUBJECT = "Still interested in solar? Pick a call time"
REMINDER_EMAIL_BODY = (
    "Hi {first},\n\nJust following up on your note to {business}. If you'd still like to talk "
    "about solar for your home, you can pick a time for a short call here:\n\n{link}\n\n"
    "If now isn't a good time, no problem at all - just reply and let us know.\n\n{business}"
)

# Lead states where a new inbound message should not trigger another auto-reply.
_FOLLOW_UP_STATES = {
    LeadStatus.awaiting_approval,
    LeadStatus.replied,
    LeadStatus.needs_review,
    LeadStatus.call_scheduled,
    LeadStatus.closed,
}


@dataclass
class Outcome:
    """What happened to one inbound message (handy for tests and logs)."""

    action: str  # duplicate | not_a_lead | needs_review | awaiting_approval | sent | follow_up
    lead_id: Optional[str] = None
    draft_id: Optional[str] = None
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, ai: LeadAI, mailer: Mailer, sms: SmsSender):
        self.s = settings
        self.store = store
        self.ai = ai
        self.mailer = mailer
        self.sms = sms

    # ================================================================== inbound

    def process_inbound(self, msg: InboundMessage) -> Outcome:
        if not self.store.claim_message(msg.external_id):
            return Outcome("duplicate")

        existing = self.store.find_lead(phone=msg.from_phone, email=msg.from_email)
        if existing and existing.status in _FOLLOW_UP_STATES:
            return self._handle_follow_up(existing, msg)

        lead = existing or Lead(first_channel=msg.channel)
        lead.log("inbound", channel=msg.channel.value, source=msg.source, subject=msg.subject, body=msg.body)
        self._absorb_contact(lead, msg)

        try:
            extraction = self.ai.extract(msg, existing)
        except AIRefusal as e:
            return self._to_review(lead, f"AI declined to process the message: {e}")
        except Exception as e:  # network/API failure: never drop a lead silently
            log.exception("extraction failed")
            return self._to_review(lead, f"AI extraction failed: {e}")

        self._apply_extraction(lead, extraction)

        if not extraction.is_lead:
            lead.status = LeadStatus.not_a_lead
            lead.review_reason = extraction.not_lead_reason
            self.store.save_lead(lead)
            return Outcome("not_a_lead", lead.id, notes=[extraction.not_lead_reason or ""])

        if extraction.needs_human:
            return self._to_review(lead, extraction.needs_human_reason or "flagged for a person")

        if not self._can_reach(lead):
            return self._to_review(
                lead,
                "No automatic way to reply (no email, and no text route for this phone). Call them back.",
            )

        try:
            reply = self.ai.draft_reply(lead, msg)
        except AIRefusal as e:
            return self._to_review(lead, f"AI declined to draft a reply: {e}")
        except Exception as e:
            log.exception("drafting failed")
            return self._to_review(lead, f"AI reply drafting failed: {e}")

        draft = Draft(lead_id=lead.id, sms_text=reply.sms_text, email_subject=reply.email_subject,
                      email_body=reply.email_body)
        self._finalize_draft(lead, draft)
        lead.log("draft", draft_id=draft.id, hold_reasons=draft.hold_reasons)

        if self.s.auto_send and not draft.hold_reasons:
            self.store.save_lead(lead)
            self.store.save_draft(draft)
            self.send_draft(draft.id)
            return Outcome("sent", lead.id, draft.id)

        lead.status = LeadStatus.awaiting_approval
        self.store.save_lead(lead)
        self.store.save_draft(draft)
        self._notify(
            f"New lead waiting for approval: {lead.contact.name or lead.contact.phone or lead.contact.email}",
            self._lead_brief(lead)
            + (f"\nHeld because: {'; '.join(draft.hold_reasons)}" if draft.hold_reasons else "")
            + f"\n\nApprove or edit: {self.s.public_base_url}/admin",
        )
        return Outcome("awaiting_approval", lead.id, draft.id, notes=draft.hold_reasons)

    def _handle_follow_up(self, lead: Lead, msg: InboundMessage) -> Outcome:
        """Same person wrote again: add it to their record and tell a person - no second auto-reply."""
        lead.log("inbound", channel=msg.channel.value, source=msg.source, subject=msg.subject, body=msg.body)
        self._absorb_contact(lead, msg)
        try:
            self._apply_extraction(lead, self.ai.extract(msg, lead), keep_summary=True)
        except Exception as e:  # follow-ups still get logged and forwarded even if AI fails
            lead.log("error", where="follow_up_extract", error=str(e))
        self.store.save_lead(lead)
        self._notify(
            f"Follow-up message from {lead.contact.name or lead.contact.phone or lead.contact.email}",
            f"Status: {lead.status.value}\n\nThey wrote ({msg.channel.value}):\n{msg.body}\n\n"
            + self._lead_brief(lead),
        )
        return Outcome("follow_up", lead.id)

    # ================================================================== drafts

    def _finalize_draft(self, lead: Lead, draft: Draft) -> None:
        """Insert the booking link and run the guardrail checks. Anything odd -> hold for approval."""
        holds: list[str] = []
        link = self.booking_link_for(lead)
        if not link:
            holds.append("BOOKING_LINK is not set")

        for attr in ("sms_text", "email_body"):
            text = getattr(draft, attr) or ""
            if _URL.search(text.replace(PLACEHOLDER, "")):
                holds.append(f"{attr} contains a URL the AI wrote itself")
            if PLACEHOLDER not in text:
                text = text.rstrip() + ("\n\n" if attr == "email_body" else " ") + PLACEHOLDER
            if _MONEY.search(text):
                holds.append(f"{attr} mentions a price, dollar amount or percentage")
            setattr(draft, attr, text.replace(PLACEHOLDER, link or "[booking link]"))

        if self.s.sms_provider == "twilio" and draft.sms_text and "stop" not in draft.sms_text.lower():
            draft.sms_text = draft.sms_text.rstrip() + " Reply STOP to opt out."
        if draft.sms_text and len(draft.sms_text) > MAX_SMS:
            holds.append(f"text is {len(draft.sms_text)} characters (limit {MAX_SMS})")
        draft.hold_reasons = holds

    def booking_link_for(self, lead: Lead) -> str:
        """Booking link with the lead id attached (Calendly passes utm_content back in its webhook)
        and name/email pre-filled so the customer types less."""
        if not self.s.booking_link:
            return ""
        parts = urlsplit(self.s.booking_link)
        q = dict(parse_qsl(parts.query))
        q["utm_content"] = lead.id
        if lead.contact.name:
            q.setdefault("name", lead.contact.name)
        if lead.contact.email:
            q.setdefault("email", lead.contact.email)
        return urlunsplit(parts._replace(query=urlencode(q)))

    def update_draft(self, draft_id: str, *, sms_text: Optional[str], email_subject: Optional[str],
                     email_body: Optional[str]) -> Draft:
        draft = self._require_draft(draft_id)
        changed = (sms_text, email_subject, email_body) != (draft.sms_text, draft.email_subject, draft.email_body)
        draft.sms_text, draft.email_subject, draft.email_body = sms_text, email_subject, email_body
        draft.edited = draft.edited or changed
        self.store.save_draft(draft)
        return draft

    def send_draft(self, draft_id: str) -> Draft:
        draft = self._require_draft(draft_id)
        lead = self.store.get_lead(draft.lead_id)
        if lead is None:
            raise KeyError(f"lead {draft.lead_id} not found")

        results: dict[str, str] = {}
        if draft.sms_text:
            try:
                results["sms"] = self.sms.send_sms(lead, draft.sms_text)
            except Exception as e:
                results["sms"] = f"error: {e}"
        if draft.email_body and lead.contact.email:
            try:
                results["email"] = self.mailer.send(
                    lead.contact.email,
                    draft.email_subject or f"Thanks for contacting {self.s.business_name}",
                    draft.email_body,
                    thread_id=lead.gmail_thread_id if lead.first_channel == Channel.email else None,
                    in_reply_to=lead.gmail_message_id_header if lead.first_channel == Channel.email else None,
                )
            except Exception as e:
                results["email"] = f"error: {e}"
        else:
            results["email"] = "skipped: no email address"

        delivered = [k for k, v in results.items() if not v.startswith(("skipped", "error"))]
        draft.send_results = results
        draft.status = DraftStatus.sent if delivered else DraftStatus.pending
        self.store.save_draft(draft)

        lead.log("sent" if delivered else "error", draft_id=draft.id, draft_kind=draft.kind, results=results)
        if delivered:
            if draft.kind == "first_reply":
                lead.status = LeadStatus.replied
                lead.replied_at = utcnow()
                lead.review_reason = None
            else:
                lead.reminders_sent += 1
        else:
            lead.status = LeadStatus.needs_review
            lead.review_reason = "Reply could not be delivered: " + "; ".join(f"{k}: {v}" for k, v in results.items())
        self.store.save_lead(lead)
        return draft

    def reject_draft(self, draft_id: str, reason: str = "") -> Draft:
        draft = self._require_draft(draft_id)
        draft.status = DraftStatus.rejected
        self.store.save_draft(draft)
        lead = self.store.get_lead(draft.lead_id)
        if lead:
            lead.status = LeadStatus.needs_review
            lead.review_reason = reason or "Draft rejected - handle personally"
            lead.log("note", text=f"draft {draft.id} rejected", reason=reason)
            self.store.save_lead(lead)
        return draft

    # ================================================================== booking (Stage 1 exit)

    def record_booking(
        self,
        *,
        lead_id: Optional[str],
        email: Optional[str],
        phone: Optional[str],
        name: Optional[str],
        booking: Booking,
    ) -> Lead:
        lead = self.store.get_lead(lead_id) if lead_id else None
        lead = lead or self.store.find_lead(phone=phone, email=email)
        if lead is None:  # booked straight from the website without messaging first
            lead = Lead(first_channel=None)
            lead.contact.name, lead.contact.email, lead.contact.phone = name, email, phone
            lead.summary = "Booked a call directly (no earlier message)."
            lead.log("note", text="lead created from booking")
        else:
            lead.contact.name = lead.contact.name or name
            lead.contact.email = lead.contact.email or email
            lead.contact.phone = lead.contact.phone or phone

        lead.booking = booking
        lead.status = LeadStatus.call_scheduled
        lead.stage = 2  # hand-off: Stage 2 (Addition / discovery call) owns it now
        lead.log("booking", **booking.model_dump())
        self.store.save_lead(lead)

        checklist = "\n".join(f"  [ ] {d.replace('_', ' ')}" for d in lead.missing_details) or "  (nothing missing)"
        self._notify(
            f"Call booked: {lead.contact.name or lead.contact.email or lead.contact.phone} - {booking.start_time}",
            self._lead_brief(lead) + f"\n\nAsk about on the call:\n{checklist}",
        )
        return lead

    def record_cancellation(self, *, lead_id: Optional[str], email: Optional[str], phone: Optional[str]) -> Optional[Lead]:
        lead = (self.store.get_lead(lead_id) if lead_id else None) or self.store.find_lead(phone=phone, email=email)
        if not lead:
            return None
        lead.booking = None
        lead.status = LeadStatus.replied
        lead.stage = 1
        lead.log("booking", canceled=True)
        self.store.save_lead(lead)
        self._notify(f"Call canceled: {lead.contact.name or lead.contact.email}", self._lead_brief(lead))
        return lead

    # ================================================================== reminders

    def run_reminders(self, now: Optional[datetime] = None) -> list[str]:
        """One reminder to leads who got a reply but haven't booked after REMINDER_AFTER_HOURS."""
        now = now or utcnow()
        cutoff = now - timedelta(hours=self.s.reminder_after_hours)
        created = []
        for lead in self.store.list_leads([LeadStatus.replied], limit=500):
            if lead.reminders_sent or not lead.replied_at or lead.replied_at > cutoff:
                continue
            if any(d.lead_id == lead.id and d.kind == "reminder" for d in self.store.list_drafts()):
                continue
            first = (lead.contact.name or "there").split()[0]
            fmt = dict(first=first, business=self.s.business_name, link=self.booking_link_for(lead))
            draft = Draft(
                lead_id=lead.id,
                kind="reminder",
                sms_text=REMINDER_SMS.format(**fmt),
                email_subject=REMINDER_EMAIL_SUBJECT,
                email_body=REMINDER_EMAIL_BODY.format(**fmt),
            )
            self.store.save_draft(draft)
            if self.s.auto_send:
                self.send_draft(draft.id)
            created.append(draft.id)
        return created

    # ================================================================== helpers

    def _absorb_contact(self, lead: Lead, msg: InboundMessage) -> None:
        lead.contact.phone = lead.contact.phone or msg.from_phone
        lead.contact.email = lead.contact.email or msg.from_email
        lead.contact.name = lead.contact.name or msg.from_name
        lead.gv_reply_address = lead.gv_reply_address or msg.gv_reply_address
        if msg.gmail_thread_id and not lead.gmail_thread_id:
            lead.gmail_thread_id = msg.gmail_thread_id
            lead.gmail_message_id_header = msg.gmail_message_id_header

    def _apply_extraction(self, lead: Lead, x: LeadExtraction, keep_summary: bool = False) -> None:
        from .normalize import normalize_phone

        lead.contact.name = lead.contact.name or x.name
        lead.contact.email = lead.contact.email or (x.email.lower() if x.email else None)
        lead.contact.phone = lead.contact.phone or normalize_phone(x.phone)
        lead.property_address = lead.property_address or x.property_address
        lead.city = lead.city or x.city
        lead.intent = lead.intent or x.intent
        lead.urgency = x.urgency
        merged = lead.details.model_dump()
        for k, v in x.details.model_dump().items():
            if v and not merged.get(k):
                merged[k] = v
        lead.details = type(lead.details)(**merged)
        if x.summary and not (keep_summary and lead.summary):
            lead.summary = x.summary
        elif x.summary and keep_summary:
            lead.summary = f"{lead.summary}\nUpdate: {x.summary}"
        lead.missing_details = x.missing_details
        lead.log("extraction", **x.model_dump(exclude={"details"}), details=x.details.model_dump(exclude_none=True))

    def _can_reach(self, lead: Lead) -> bool:
        if lead.contact.email:
            return True
        if not lead.contact.phone:
            return False
        if self.s.sms_provider == "twilio":
            return True
        if self.s.sms_provider == "gvoice_email":
            return bool(lead.gv_reply_address)
        return False

    def _to_review(self, lead: Lead, reason: str) -> Outcome:
        lead.status = LeadStatus.needs_review
        lead.review_reason = reason
        lead.log("note", text="sent to a person", reason=reason)
        self.store.save_lead(lead)
        self._notify(
            f"Lead needs a person: {lead.contact.name or lead.contact.phone or lead.contact.email or lead.id}",
            f"Why: {reason}\n\n" + self._lead_brief(lead) + f"\n\n{self.s.public_base_url}/admin",
        )
        return Outcome("needs_review", lead.id, notes=[reason])

    def _lead_brief(self, lead: Lead) -> str:
        c = lead.contact
        lines = [
            f"Name:    {c.name or '-'}",
            f"Phone:   {c.phone or '-'}",
            f"Email:   {c.email or '-'}",
            f"Area:    {lead.city or lead.property_address or '-'}",
            f"Wants:   {lead.intent or '-'}   Urgency: {lead.urgency or '-'}",
            f"Came in: {lead.first_channel.value if lead.first_channel else 'booking'}",
            "",
            lead.summary or "",
        ]
        known = lead.details.model_dump(exclude_none=True)
        if known:
            lines += ["", "Known so far:"] + [f"  - {k.replace('_', ' ')}: {v}" for k, v in known.items()]
        return "\n".join(lines)

    def _notify(self, subject: str, body: str) -> None:
        for to in self.s.notify_emails:
            try:
                self.mailer.send(to, f"[GSP] {subject}", body)
            except Exception:
                log.exception("notification to %s failed", to)

    def _require_draft(self, draft_id: str) -> Draft:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise KeyError(f"draft {draft_id} not found")
        return draft
