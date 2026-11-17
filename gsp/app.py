"""Web app: webhooks, the approval page, and background jobs.

Run:  uvicorn gsp.app:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import normalize
from .ai import ClaudeLeadAI, FakeLeadAI
from .config import Settings
from .models import Booking, Channel, DraftStatus, InboundMessage, LeadStatus, new_id
from .pipeline import Pipeline
from .senders import GmailMailer, GoogleVoiceEmailSms, NoSms, OutboxMailer, TwilioSms
from .store import Store

log = logging.getLogger("gsp")
AUTO_SEND_STREAK = 25  # unedited approvals in a row before suggesting AUTO_SEND=true


# =========================================================================== wiring


def build_pipeline(settings: Settings):
    store = Store(settings.db_path)
    gmail = None
    if settings.gmail_enabled:
        from .gmail import GmailClient, build_service

        gmail = GmailClient(build_service(settings.gmail_credentials_file, settings.gmail_token_file))
        mailer = GmailMailer(gmail, settings.business_email)
    else:
        mailer = OutboxMailer()

    if settings.sms_provider == "twilio":
        sms = TwilioSms(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number)
    elif settings.sms_provider == "gvoice_email" and gmail is not None:
        sms = GoogleVoiceEmailSms(mailer)
    else:
        sms = NoSms()

    if settings.ai_mode == "fake":
        ai = FakeLeadAI()
    else:
        ai = ClaudeLeadAI(settings.business_name, settings.response_parameters(), settings.claude_model)
    return Pipeline(settings, store, ai, mailer, sms), gmail


def _gmail_loop(pipeline: Pipeline, gmail, settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            for m in gmail.new_messages(settings.gmail_query):
                try:
                    msg = normalize.from_gmail(
                        message_id=m["id"], thread_id=m["thread_id"], headers=m["headers"], body=m["body"],
                        business_email=settings.business_email,
                    )
                    if msg is not None:
                        outcome = pipeline.process_inbound(msg)
                        log.info("gmail %s -> %s (%s)", m["id"], outcome.action, outcome.lead_id)
                finally:
                    gmail.mark_processed(m["id"])
        except Exception:
            log.exception("gmail poll failed")
        stop.wait(settings.gmail_poll_seconds)


def _reminder_loop(pipeline: Pipeline, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            created = pipeline.run_reminders()
            if created:
                log.info("created %d reminder(s)", len(created))
        except Exception:
            log.exception("reminder job failed")
        stop.wait(15 * 60)


# =========================================================================== security helpers


def twilio_signature_ok(auth_token: str, url: str, params: dict, signature: str) -> bool:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def calendly_signature_ok(signing_key: str, header: str, raw_body: bytes, tolerance_s: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t, v1 = parts["t"], parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - int(t)) > tolerance_s:
        return False
    expected = hmac.new(signing_key.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


# =========================================================================== app


def create_app(settings: Optional[Settings] = None, pipeline: Optional[Pipeline] = None, gmail=None,
               background: bool = True) -> FastAPI:
    settings = settings or Settings()
    if pipeline is None:
        pipeline, gmail = build_pipeline(settings)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threads = []
        if background:
            if gmail is not None:
                threads.append(threading.Thread(target=_gmail_loop, args=(pipeline, gmail, settings, stop), daemon=True))
            threads.append(threading.Thread(target=_reminder_loop, args=(pipeline, stop), daemon=True))
            for t in threads:
                t.start()
        yield
        stop.set()

    app = FastAPI(title=f"{settings.business_name} - Stage 1 Intake", lifespan=lifespan)
    app.state.pipeline = pipeline
    security = HTTPBasic()

    def admin(creds: HTTPBasicCredentials = Depends(security)) -> str:
        if not settings.admin_password:
            raise HTTPException(503, "Set ADMIN_PASSWORD to use the admin page.")
        ok = secrets.compare_digest(creds.username, settings.admin_user) and secrets.compare_digest(
            creds.password, settings.admin_password
        )
        if not ok:
            raise HTTPException(401, "Wrong login", headers={"WWW-Authenticate": "Basic"})
        return creds.username

    # ------------------------------------------------------------------ webhooks

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/webhooks/twilio/sms")
    async def twilio_sms(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        url = settings.public_base_url.rstrip("/") + "/webhooks/twilio/sms"
        if not twilio_signature_ok(settings.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "bad signature")
        body = (form.get("Body") or "").strip().upper()
        if body not in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "HELP"}:
            pipeline.process_inbound(normalize.from_twilio(form))
        # Twilio handles STOP/HELP replies itself; we answer with empty TwiML.
        return Response("<Response></Response>", media_type="text/xml")

    @app.post("/webhooks/calendly")
    async def calendly(request: Request):
        raw = await request.body()
        if not settings.calendly_signing_key or not calendly_signature_ok(
            settings.calendly_signing_key, request.headers.get("Calendly-Webhook-Signature", ""), raw
        ):
            raise HTTPException(403, "bad signature")
        data = await request.json()
        event, p = data.get("event"), data.get("payload", {}) or {}
        lead_id = ((p.get("tracking") or {}).get("utm_content")) or None
        phone = normalize.normalize_phone(p.get("text_reminder_number"))
        if not phone:
            for qa in p.get("questions_and_answers") or []:
                phone = normalize.find_phone(qa.get("answer", ""))
                if phone:
                    break
        if event == "invitee.created":
            se = p.get("scheduled_event") or {}
            lead = pipeline.record_booking(
                lead_id=lead_id, email=p.get("email"), phone=phone, name=p.get("name"),
                booking=Booking(start_time=se.get("start_time"), end_time=se.get("end_time"),
                                event_uri=se.get("uri"), cancel_url=p.get("cancel_url")),
            )
            return {"lead_id": lead.id}
        if event == "invitee.canceled":
            lead = pipeline.record_cancellation(lead_id=lead_id, email=p.get("email"), phone=phone)
            return {"lead_id": lead.id if lead else None}
        return {"ignored": event}

    # ------------------------------------------------------------------ admin

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(_: str = Depends(admin)):
        return _render_admin(settings, pipeline)

    @app.post("/admin/drafts/{draft_id}/send")
    def admin_send(draft_id: str, sms_text: str = Form(""), email_subject: str = Form(""),
                   email_body: str = Form(""), _: str = Depends(admin)):
        pipeline.update_draft(draft_id, sms_text=sms_text or None, email_subject=email_subject or None,
                              email_body=email_body or None)
        pipeline.send_draft(draft_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/drafts/{draft_id}/reject")
    def admin_reject(draft_id: str, reason: str = Form(""), _: str = Depends(admin)):
        pipeline.reject_draft(draft_id, reason)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/leads/{lead_id}/close")
    def admin_close(lead_id: str, _: str = Depends(admin)):
        lead = pipeline.store.get_lead(lead_id) or _404()
        lead.status = LeadStatus.closed
        lead.log("note", text="closed from admin")
        pipeline.store.save_lead(lead)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/leads/{lead_id}/booked")
    def admin_booked(lead_id: str, start_time: str = Form(...), _: str = Depends(admin)):
        """For bookings that don't come through Calendly (phone, Google Calendar page)."""
        lead = pipeline.store.get_lead(lead_id) or _404()
        pipeline.record_booking(lead_id=lead.id, email=None, phone=None, name=None,
                                booking=Booking(start_time=start_time, source="manual"))
        return RedirectResponse("/admin", status_code=303)

    @app.get("/admin/leads/{lead_id}", response_class=HTMLResponse)
    def admin_lead(lead_id: str, _: str = Depends(admin)):
        lead = pipeline.store.get_lead(lead_id) or _404()
        return _page(f"Lead {lead.id}", f"<p><a href='/admin'>&larr; back</a></p><pre>{_e(lead.model_dump_json(indent=2))}</pre>")

    @app.post("/admin/test-message")
    def admin_test(channel: str = Form("email"), name: str = Form(""), phone: str = Form(""),
                   email: str = Form(""), body: str = Form(...), _: str = Depends(admin)):
        """Paste in a sample inquiry to see what the pipeline does with it."""
        msg = InboundMessage(
            channel=Channel(channel), source="manual", external_id=new_id("manual"),
            from_name=name or None, from_phone=normalize.normalize_phone(phone), from_email=email or None, body=body,
        )
        pipeline.process_inbound(msg)
        return RedirectResponse("/admin", status_code=303)

    @app.get("/api/leads")
    def api_leads(status: Optional[str] = None, _: str = Depends(admin)):
        statuses = [LeadStatus(status)] if status else None
        return JSONResponse([l.model_dump(mode="json") for l in pipeline.store.list_leads(statuses)])

    return app


def _404():
    raise HTTPException(404, "not found")


# =========================================================================== HTML


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:980px;margin:0 auto;padding:16px;color:#1d2433;background:#f6f7f9}}
 h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:2rem}}
 .card{{background:#fff;border:1px solid #dde1e7;border-radius:8px;padding:14px;margin:10px 0}}
 .muted{{color:#5d6675;font-size:.9rem}} .warn{{color:#9a4b00}} .ok{{color:#1a7f37}}
 textarea,input,select{{width:100%;box-sizing:border-box;font:inherit;padding:6px;margin:4px 0 8px;border:1px solid #c9ced6;border-radius:6px}}
 button{{font:inherit;padding:7px 14px;border-radius:6px;border:1px solid #1d2433;background:#1d2433;color:#fff;cursor:pointer}}
 button.secondary{{background:#fff;color:#1d2433}} .row{{display:flex;gap:8px;flex-wrap:wrap}}
 table{{width:100%;border-collapse:collapse;font-size:.9rem}} td,th{{text-align:left;padding:6px;border-bottom:1px solid #e5e8ec;vertical-align:top}}
 .pill{{display:inline-block;padding:1px 8px;border-radius:99px;background:#e8ebf0;font-size:.8rem}}
 pre{{white-space:pre-wrap;background:#fff;padding:12px;border:1px solid #dde1e7;border-radius:8px}}
</style></head><body>{body}</body></html>"""


def _render_admin(settings: Settings, pipeline: Pipeline) -> str:
    store = pipeline.store
    out = [f"<h1>{_e(settings.business_name)} &mdash; New leads (Stage 1)</h1>"]

    streak = store.approval_streak()
    mode = "<span class='ok'>AUTO-SEND is ON</span>" if settings.auto_send else "Approval mode: every reply waits for you."
    hint = ""
    if not settings.auto_send and streak >= AUTO_SEND_STREAK:
        hint = f" <b>{streak} replies in a row went out unedited &mdash; consider setting AUTO_SEND=true.</b>"
    out.append(f"<p class='muted'>{mode} Unedited approvals in a row: {streak}.{hint}</p>")

    # ---- drafts waiting
    drafts = store.list_drafts(DraftStatus.pending)
    out.append(f"<h2>Replies waiting for approval ({len(drafts)})</h2>")
    for d in drafts:
        lead = store.get_lead(d.lead_id)
        if lead is None:
            continue
        holds = "".join(f"<div class='warn'>&#9888; {_e(h)}</div>" for h in d.hold_reasons)
        failed = ""
        if d.send_results:
            failed = f"<div class='warn'>Last send attempt: {_e(d.send_results)}</div>"
        out.append(f"""<div class="card">
 <b>{_e(lead.contact.name or lead.contact.phone or lead.contact.email)}</b>
 <span class="pill">{_e(d.kind.replace('_',' '))}</span> <span class="pill">{_e(lead.first_channel.value if lead.first_channel else '')}</span>
 <div class="muted">{_e(lead.contact.phone)} &middot; {_e(lead.contact.email)} &middot; {_e(lead.city)}</div>
 <p>{_e(lead.summary)}</p>{holds}{failed}
 <form method="post" action="/admin/drafts/{_e(d.id)}/send">
  <label>Text message</label><textarea name="sms_text" rows="3">{_e(d.sms_text)}</textarea>
  <label>Email subject</label><input name="email_subject" value="{_e(d.email_subject)}">
  <label>Email</label><textarea name="email_body" rows="7">{_e(d.email_body)}</textarea>
  <div class="row"><button>Send</button></div>
 </form>
 <form method="post" action="/admin/drafts/{_e(d.id)}/reject" class="row" style="margin-top:6px">
  <input name="reason" placeholder="Why (optional) - I'll handle this one myself" style="flex:1">
  <button class="secondary">Don't send</button>
 </form>
 <div class="muted"><a href="/admin/leads/{_e(lead.id)}">full record</a></div>
</div>""")

    # ---- needs a person
    review = store.list_leads([LeadStatus.needs_review])
    out.append(f"<h2>Needs a person ({len(review)})</h2>")
    for lead in review:
        out.append(f"""<div class="card">
 <b>{_e(lead.contact.name or lead.contact.phone or lead.contact.email or lead.id)}</b>
 <div class="muted">{_e(lead.contact.phone)} &middot; {_e(lead.contact.email)}</div>
 <div class="warn">{_e(lead.review_reason)}</div><p>{_e(lead.summary)}</p>
 <div class="row">
  <form method="post" action="/admin/leads/{_e(lead.id)}/booked" class="row">
   <input name="start_time" placeholder="Call booked for (e.g. 2026-11-20 10:00)" required style="width:260px">
   <button class="secondary">Mark call booked</button></form>
  <form method="post" action="/admin/leads/{_e(lead.id)}/close"><button class="secondary">Close</button></form>
 </div>
 <div class="muted"><a href="/admin/leads/{_e(lead.id)}">full record</a></div>
</div>""")

    # ---- pipeline table
    leads = store.list_leads(limit=100)
    rows = "".join(
        f"<tr><td><a href='/admin/leads/{_e(l.id)}'>{_e(l.contact.name or l.contact.phone or l.contact.email or l.id)}</a></td>"
        f"<td><span class='pill'>{_e(l.status.value.replace('_',' '))}</span></td>"
        f"<td>{_e(l.first_channel.value if l.first_channel else 'booking')}</td>"
        f"<td>{_e(l.intent)}</td><td>{_e((l.booking.start_time if l.booking else '') or '')}</td>"
        f"<td>{_e(l.created_at.strftime('%b %d %H:%M'))}</td></tr>"
        for l in leads
    )
    out.append(f"<h2>All leads</h2><div class='card'><table><tr><th>Lead</th><th>Status</th><th>Came in by</th>"
               f"<th>Wants</th><th>Call</th><th>Received</th></tr>{rows}</table></div>")

    # ---- test box
    out.append("""<h2>Try a sample message</h2><div class="card"><p class="muted">Paste a real past inquiry to see how
 the AI reads it and what reply it drafts. Nothing is sent until you press Send above.</p>
 <form method="post" action="/admin/test-message">
  <select name="channel"><option value="email">email</option><option value="sms">text</option>
   <option value="voicemail">voicemail</option></select>
  <div class="row"><input name="name" placeholder="Name" style="flex:1"><input name="phone" placeholder="Phone" style="flex:1">
   <input name="email" placeholder="Email" style="flex:1"></div>
  <textarea name="body" rows="4" placeholder="Their message" required></textarea><button>Run it</button>
 </form></div>""")
    return _page("GSP leads", "".join(out))


# Start with:  uvicorn gsp.app:create_app --factory --host 0.0.0.0 --port 8000
