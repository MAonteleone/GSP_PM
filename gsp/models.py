"""Data shapes shared by every module of the Georgia Solar Panels pipeline.

The Lead record is the "point" from the notebook: every stage reads it, adds to it,
and moves it forward. Fields that later stages fill in (roof, racking, booking)
exist now so modules 2-5 can plug in without a migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- inbound


class Channel(str, Enum):
    email = "email"
    sms = "sms"
    voicemail = "voicemail"
    missed_call = "missed_call"


class InboundMessage(BaseModel):
    """One message from any channel, normalized to a single shape."""

    channel: Channel
    source: Literal["gmail", "gvoice", "twilio", "manual"]
    external_id: str  # Gmail message id / Twilio MessageSid - used to de-duplicate
    received_at: datetime = Field(default_factory=utcnow)
    from_name: Optional[str] = None
    from_email: Optional[str] = None
    from_phone: Optional[str] = None  # E.164, e.g. +14045551234
    subject: Optional[str] = None
    body: str = ""
    # Google Voice: replying to this address sends a text back from the GV number.
    gv_reply_address: Optional[str] = None
    gmail_thread_id: Optional[str] = None
    gmail_message_id_header: Optional[str] = None  # RFC 822 Message-ID, for threading replies


# --------------------------------------------------------------------------- AI output


Intent = Literal[
    "new_install",
    "battery_storage",
    "service_repair",
    "system_expansion",
    "general_question",
    "other",
]

# The checklist Stage 2 (the discovery call) needs filled in. Stage 1 marks which are
# still missing so the call has an agenda before it starts.
DETAIL_CHECKLIST: list[str] = [
    "full_name",
    "phone",
    "email",
    "property_address",
    "homeowner",
    "utility_company",
    "average_monthly_bill",
    "roof_material",
    "roof_age",
    "roof_pitch_or_shading",
    "battery_interest",
    "timeline",
    "financing_preference",
    "main_motivation",
]


class KnownDetails(BaseModel):
    homeowner: Optional[str] = None
    utility_company: Optional[str] = None
    average_monthly_bill: Optional[str] = None
    roof_material: Optional[str] = None
    roof_age: Optional[str] = None
    roof_pitch_or_shading: Optional[str] = None
    battery_interest: Optional[str] = None
    timeline: Optional[str] = None
    financing_preference: Optional[str] = None
    main_motivation: Optional[str] = None


class LeadExtraction(BaseModel):
    """What Claude pulls out of one inbound message."""

    is_lead: bool = Field(description="True if this is a person asking about solar/battery work.")
    not_lead_reason: Optional[str] = Field(
        default=None, description="If not a lead: spam, vendor pitch, newsletter, wrong number, etc."
    )
    needs_human: bool = Field(
        description="True if a person should answer instead of an auto-reply: upset customer, "
        "existing job or warranty issue, legal/billing dispute, urgent safety issue, or anything unclear."
    )
    needs_human_reason: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    property_address: Optional[str] = None
    city: Optional[str] = None
    intent: Intent
    details: KnownDetails
    urgency: Literal["low", "normal", "high"]
    summary: str = Field(description="2-3 plain sentences: who they are, what they want, anything notable.")
    missing_details: list[str] = Field(
        description="Items from the checklist that are still unknown, using the exact checklist keys."
    )


class ReplyDraft(BaseModel):
    """The auto-response Claude writes. {BOOKING_LINK} is filled in by code."""

    sms_text: str
    email_subject: str
    email_body: str


# --------------------------------------------------------------------------- lead record


class LeadStatus(str, Enum):
    new = "new"
    needs_review = "needs_review"  # a person must look at it (flagged, refusal, existing customer)
    awaiting_approval = "awaiting_approval"  # draft reply is waiting for one-tap approval
    replied = "replied"  # auto-reply sent, waiting for them to book
    call_scheduled = "call_scheduled"  # Stage 1 done - hand-off to Stage 2
    not_a_lead = "not_a_lead"
    closed = "closed"


class Contact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class Roof(BaseModel):
    """Filled in by Stage 2 / site visit; required by Stage 5 racking tools."""

    material: Optional[str] = None
    pitch: Optional[str] = None
    rafter_spacing: Optional[str] = None
    age: Optional[str] = None


class Booking(BaseModel):
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    event_uri: Optional[str] = None
    cancel_url: Optional[str] = None
    source: str = "calendly"


class Event(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: str  # inbound | extraction | draft | sent | booking | reminder | note | error
    data: dict[str, Any] = Field(default_factory=dict)


class Lead(BaseModel):
    id: str = Field(default_factory=lambda: new_id("lead"))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    stage: int = 1
    status: LeadStatus = LeadStatus.new
    first_channel: Optional[Channel] = None
    contact: Contact = Field(default_factory=Contact)
    property_address: Optional[str] = None
    city: Optional[str] = None
    intent: Optional[str] = None
    urgency: Optional[str] = None
    details: KnownDetails = Field(default_factory=KnownDetails)
    roof: Roof = Field(default_factory=Roof)
    racking_system: Optional[str] = None
    summary: Optional[str] = None
    missing_details: list[str] = Field(default_factory=list)
    review_reason: Optional[str] = None
    gv_reply_address: Optional[str] = None
    gmail_thread_id: Optional[str] = None
    gmail_message_id_header: Optional[str] = None
    booking: Optional[Booking] = None
    replied_at: Optional[datetime] = None
    reminders_sent: int = 0
    history: list[Event] = Field(default_factory=list)

    def log(self, kind: str, **data: Any) -> None:
        self.history.append(Event(kind=kind, data=data))
        self.updated_at = utcnow()


class DraftStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    rejected = "rejected"


class Draft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("draft"))
    lead_id: str
    created_at: datetime = Field(default_factory=utcnow)
    kind: Literal["first_reply", "reminder"] = "first_reply"
    sms_text: Optional[str] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    status: DraftStatus = DraftStatus.pending
    hold_reasons: list[str] = Field(default_factory=list)
    edited: bool = False
    send_results: dict[str, Any] = Field(default_factory=dict)
