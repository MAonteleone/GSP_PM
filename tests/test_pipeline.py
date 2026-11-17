from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from gsp.ai import AIRefusal, FakeLeadAI
from gsp.models import Booking, Channel, DraftStatus, InboundMessage, LeadStatus, ReplyDraft, utcnow


def sms_msg(body="Interested in solar for my house, bill is about $250", ext="twilio:1", phone="+14045551234"):
    return InboundMessage(channel=Channel.sms, source="twilio", external_id=ext, from_phone=phone, body=body)


def email_msg(body="Hi, thinking about panels plus a battery.", ext="gmail:1"):
    return InboundMessage(channel=Channel.email, source="gmail", external_id=ext, from_name="Dana Smith",
                          from_email="dana@example.com", body=body, gmail_thread_id="t1")


def test_new_lead_waits_for_approval_then_sends(pipeline, mailer, sms):
    out = pipeline.process_inbound(email_msg())
    assert out.action == "awaiting_approval"
    lead = pipeline.store.get_lead(out.lead_id)
    assert lead.status == LeadStatus.awaiting_approval
    assert lead.summary and "full_name" not in lead.missing_details
    # team was told
    assert any("approval" in m["subject"] for m in mailer.outbox)

    draft = pipeline.store.get_draft(out.draft_id)
    assert "{BOOKING_LINK}" not in draft.email_body
    link = next(w for w in draft.email_body.split() if w.startswith("https://calendly.com"))
    q = parse_qs(urlsplit(link).query)
    assert q["utm_content"] == [lead.id] and q["email"] == ["dana@example.com"]

    pipeline.send_draft(draft.id)
    lead = pipeline.store.get_lead(lead.id)
    assert lead.status == LeadStatus.replied and lead.replied_at
    assert any(m["to"] == "dana@example.com" for m in mailer.outbox)
    assert pipeline.store.get_draft(draft.id).status == DraftStatus.sent


def test_auto_send_texts_with_opt_out(pipeline, settings, sms):
    settings.auto_send = True
    out = pipeline.process_inbound(sms_msg())
    assert out.action == "sent"
    phone, text = sms.sent[0]
    assert phone == "+14045551234"
    assert "Reply STOP to opt out." in text
    assert pipeline.store.get_lead(out.lead_id).details.average_monthly_bill == "$250"


def test_duplicate_message_is_ignored(pipeline):
    assert pipeline.process_inbound(sms_msg()).action == "awaiting_approval"
    assert pipeline.process_inbound(sms_msg()).action == "duplicate"


def test_follow_up_does_not_send_second_auto_reply(pipeline, settings, sms):
    settings.auto_send = True
    first = pipeline.process_inbound(sms_msg())
    second = pipeline.process_inbound(sms_msg(body="Also do you do batteries?", ext="twilio:2"))
    assert second.action == "follow_up" and second.lead_id == first.lead_id
    assert len(sms.sent) == 1


def test_spam_is_not_a_lead(pipeline, mailer):
    out = pipeline.process_inbound(email_msg(body="We offer SEO and backlinks for solar companies"))
    assert out.action == "not_a_lead"
    assert not any(m["subject"].startswith("[GSP]") for m in mailer.outbox)


def test_upset_customer_goes_to_a_person(pipeline, settings, sms):
    settings.auto_send = True
    out = pipeline.process_inbound(sms_msg(body="The system you installed is still not working, this is unacceptable"))
    assert out.action == "needs_review"
    assert sms.sent == []


def test_refusal_goes_to_a_person(pipeline):
    class Refuses(FakeLeadAI):
        def extract(self, msg, existing):
            raise AIRefusal("declined")

    pipeline.ai = Refuses()
    assert pipeline.process_inbound(sms_msg()).action == "needs_review"


def test_api_failure_never_drops_the_lead(pipeline):
    class Broken(FakeLeadAI):
        def extract(self, msg, existing):
            raise ConnectionError("network down")

    pipeline.ai = Broken()
    out = pipeline.process_inbound(sms_msg())
    lead = pipeline.store.get_lead(out.lead_id)
    assert lead.status == LeadStatus.needs_review and lead.contact.phone == "+14045551234"


def test_guardrails_hold_pricing_and_ai_written_urls(pipeline, settings):
    settings.auto_send = True

    class Pricey(FakeLeadAI):
        def draft_reply(self, lead, msg):
            return ReplyDraft(sms_text="Most systems run $18,000. Book: {BOOKING_LINK}",
                              email_subject="Hi", email_body="See https://evil.example.com and book {BOOKING_LINK}")

    pipeline.ai = Pricey()
    out = pipeline.process_inbound(sms_msg())
    assert out.action == "awaiting_approval"  # held even though AUTO_SEND is on
    holds = " ".join(out.notes)
    assert "price" in holds and "URL" in holds


def test_missing_placeholder_gets_link_appended(pipeline):
    class NoLink(FakeLeadAI):
        def draft_reply(self, lead, msg):
            return ReplyDraft(sms_text="Thanks! Let's set up a call.", email_subject="Hi", email_body="Thanks!")

    pipeline.ai = NoLink()
    out = pipeline.process_inbound(sms_msg())
    d = pipeline.store.get_draft(out.draft_id)
    assert "calendly.com" in d.sms_text and "calendly.com" in d.email_body


def test_voicemail_without_text_route_needs_callback(pipeline, settings):
    settings.sms_provider = "gvoice_email"  # can only text people who texted in
    vm = InboundMessage(channel=Channel.voicemail, source="gvoice", external_id="gmail:vm",
                        from_phone="+16785550199", body="Call me back about solar")
    out = pipeline.process_inbound(vm)
    assert out.action == "needs_review" and "Call them back" in out.notes[0]


def test_booking_hands_off_to_stage_2(pipeline, mailer):
    out = pipeline.process_inbound(email_msg())
    pipeline.send_draft(out.draft_id)
    lead = pipeline.record_booking(lead_id=out.lead_id, email=None, phone=None, name=None,
                                   booking=Booking(start_time="2026-11-20T15:00:00Z"))
    assert lead.status == LeadStatus.call_scheduled and lead.stage == 2
    note = next(m for m in mailer.outbox if m["subject"].startswith("[GSP] Call booked"))
    assert "Ask about on the call" in note["body"] and "utility company" in note["body"]


def test_booking_matches_by_email_and_creates_unknown(pipeline):
    out = pipeline.process_inbound(email_msg())
    lead = pipeline.record_booking(lead_id=None, email="DANA@example.com", phone=None, name="Dana",
                                   booking=Booking(start_time="x"))
    assert lead.id == out.lead_id
    walk_in = pipeline.record_booking(lead_id=None, email="new@example.com", phone=None, name="New",
                                      booking=Booking(start_time="y"))
    assert walk_in.id != out.lead_id and walk_in.status == LeadStatus.call_scheduled


def test_reminder_after_24h_only_once(pipeline, settings, sms):
    settings.auto_send = True
    out = pipeline.process_inbound(sms_msg())
    assert pipeline.run_reminders() == []  # too soon
    later = utcnow() + timedelta(hours=25)
    assert len(pipeline.run_reminders(now=later)) == 1
    assert pipeline.store.get_lead(out.lead_id).reminders_sent == 1
    assert pipeline.run_reminders(now=later) == []
    assert len(sms.sent) == 2


def test_approval_streak(pipeline):
    for i in range(3):
        out = pipeline.process_inbound(sms_msg(ext=f"twilio:s{i}", phone=f"+1404555000{i}"))
        pipeline.send_draft(out.draft_id)
    assert pipeline.store.approval_streak() == 3
    out = pipeline.process_inbound(sms_msg(ext="twilio:s9", phone="+14045550009"))
    pipeline.update_draft(out.draft_id, sms_text="edited", email_subject=None, email_body=None)
    pipeline.send_draft(out.draft_id)
    assert pipeline.store.approval_streak() == 0
