import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from gsp.app import create_app
from gsp.models import LeadStatus

AUTH = ("admin", "pw")


def client_for(settings, pipeline):
    return TestClient(create_app(settings, pipeline, background=False))


def twilio_sig(token, url, params):
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def test_twilio_webhook_requires_valid_signature(settings, pipeline):
    c = client_for(settings, pipeline)
    form = {"MessageSid": "SM9", "From": "+14045551234", "Body": "Do you install solar in Macon?"}
    assert c.post("/webhooks/twilio/sms", data=form, headers={"X-Twilio-Signature": "nope"}).status_code == 403

    sig = twilio_sig("twtoken", "https://gsp.example.com/webhooks/twilio/sms", form)
    r = c.post("/webhooks/twilio/sms", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<Response>" in r.text
    leads = pipeline.store.list_leads()
    assert len(leads) == 1 and leads[0].contact.phone == "+14045551234"


def test_twilio_stop_is_not_a_lead(settings, pipeline):
    c = client_for(settings, pipeline)
    form = {"MessageSid": "SM10", "From": "+14045551234", "Body": "STOP"}
    sig = twilio_sig("twtoken", "https://gsp.example.com/webhooks/twilio/sms", form)
    assert c.post("/webhooks/twilio/sms", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200
    assert pipeline.store.list_leads() == []


def calendly_headers(body: bytes, key="calkey"):
    t = str(int(time.time()))
    v1 = hmac.new(key.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Calendly-Webhook-Signature": f"t={t},v1={v1}", "Content-Type": "application/json"}


def test_calendly_booking_and_cancel(settings, pipeline):
    c = client_for(settings, pipeline)
    c.post("/admin/test-message", auth=AUTH, data={"channel": "email", "name": "Dana", "email": "dana@example.com",
                                                     "body": "Solar for my house please"})
    lead = pipeline.store.list_leads()[0]

    body = json.dumps({"event": "invitee.created", "payload": {
        "email": "dana@example.com", "name": "Dana", "tracking": {"utm_content": lead.id},
        "scheduled_event": {"start_time": "2026-11-20T15:00:00Z", "uri": "https://api.calendly.com/x"}}}).encode()
    assert c.post("/webhooks/calendly", content=body, headers={"Calendly-Webhook-Signature": "t=1,v1=bad",
                                                                 "Content-Type": "application/json"}).status_code == 403
    r = c.post("/webhooks/calendly", content=body, headers=calendly_headers(body))
    assert r.json()["lead_id"] == lead.id
    assert pipeline.store.get_lead(lead.id).status == LeadStatus.call_scheduled

    cancel = json.dumps({"event": "invitee.canceled", "payload": {"email": "dana@example.com",
                                                                  "tracking": {"utm_content": lead.id}}}).encode()
    c.post("/webhooks/calendly", content=cancel, headers=calendly_headers(cancel))
    assert pipeline.store.get_lead(lead.id).status == LeadStatus.replied


def test_admin_requires_login_and_sends_edited_draft(settings, pipeline, mailer):
    c = client_for(settings, pipeline)
    assert c.get("/admin").status_code == 401
    assert c.get("/admin", auth=("admin", "wrong")).status_code == 401

    c.post("/admin/test-message", auth=AUTH, data={"channel": "email", "name": "Pat <script>", "email": "pat@example.com",
                                                     "body": "Thinking about solar"})
    page = c.get("/admin", auth=AUTH)
    assert page.status_code == 200 and "Replies waiting for approval (1)" in page.text
    assert "<script>" not in page.text  # escaped

    draft = pipeline.store.list_drafts()[0]
    r = c.post(f"/admin/drafts/{draft.id}/send", auth=AUTH, follow_redirects=False,
               data={"sms_text": "", "email_subject": "Hello Pat", "email_body": "Edited body"})
    assert r.status_code == 303
    sent = pipeline.store.get_draft(draft.id)
    assert sent.edited and sent.status.value == "sent"
    assert any(m["to"] == "pat@example.com" and m["body"] == "Edited body" for m in mailer.outbox)


def test_admin_disabled_without_password(settings, pipeline):
    settings.admin_password = ""
    assert client_for(settings, pipeline).get("/admin", auth=AUTH).status_code == 503
