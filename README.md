# Georgia Solar Panels — Stage 1: Inception (lead intake)

A customer emails, texts or leaves a voicemail. Within a minute this app:

1. pulls the message in (Gmail, Google Voice forwards, or Twilio),
2. finds or creates a **lead record** for that person,
3. has Claude summarize it and list which discovery details are still missing,
4. has Claude draft a text and an email that follow `config/response_parameters.md`,
5. checks the draft (no prices, no AI-written links, length, booking link present),
6. sends it, or holds it on `/admin` for one-tap approval,
7. sends one reminder if they haven't booked after 24 hours,
8. when they book (Calendly webhook, or "Mark call booked"), sets the lead to
   `call_scheduled`, moves it to Stage 2, and emails the team a call-prep note with the
   checklist of what to ask.

Upset customers, existing-job issues, spam, AI refusals and API errors never get an auto-reply:
they go to **Needs a person** (spam is just filed away).

## Quick start (demo, no accounts needed)

```bash
pip install -r requirements.txt
cp .env.example .env            # then set ADMIN_PASSWORD and AI_MODE=fake
uvicorn gsp.app:create_app --factory --port 8000
```

Open http://localhost:8000/admin and use **Try a sample message**. Set `AI_MODE=claude`
and `ANTHROPIC_API_KEY` to use real Claude. Run tests with `pytest`.

## Going live — checklist

| Step | What to do |
|---|---|
| 1. Response rules | Edit `config/response_parameters.md`. Paste 20–30 real past inquiries into *Try a sample message* and tune until you rarely edit a draft. |
| 2. Claude | Set `ANTHROPIC_API_KEY`. Model defaults to `claude-opus-5-5`. |
| 3. Gmail | In Google Cloud, enable the Gmail API and create an OAuth "Desktop app" client; save it as `secrets/credentials.json`. Run `python -m gsp.gmail auth` once on a computer with a browser, then set `GMAIL_ENABLED=true`, `BUSINESS_EMAIL`, `NOTIFY_EMAILS`. |
| 4. Google Voice | In Google Voice settings, turn on *Forward messages to email*, plus voicemail and missed-call email notifications, to the same Gmail inbox. |
| 5. Texting | **Quick start:** `SMS_PROVIDER=gvoice_email` replies to people who texted your Google Voice number (it uses Google Voice's reply-by-email feature; there's no official API, so it can stop working without notice). People who only called get flagged "call them back". **Recommended:** `SMS_PROVIDER=twilio`. Point the number's incoming-message webhook to `PUBLIC_BASE_URL/webhooks/twilio/sms` and finish A2P 10DLC registration (1–3 weeks). |
| 6. Booking | `BOOKING_LINK` = your Calendly event link. Create a Calendly webhook subscription (`invitee.created`, `invitee.canceled`) to `PUBLIC_BASE_URL/webhooks/calendly` and put its signing key in `CALENDLY_SIGNING_KEY`. Using a Google Calendar booking page instead? Use **Mark call booked** on `/admin`. |
| 7. Host it | Any small server with HTTPS (Render, Railway, Fly.io, a VPS). Set `PUBLIC_BASE_URL` to its address. Keep `data/` on a persistent disk. |
| 8. Auto-send | Leave `AUTO_SEND=false` at first. `/admin` counts replies sent without edits; after 25 in a row, switch it to `true`. Anything the guardrails flag is still held for approval. |

## Layout

```
gsp/models.py      Lead record (shared by all 5 stages), messages, drafts
gsp/normalize.py   Gmail / Google Voice / Twilio -> one message shape
gsp/ai.py          Claude: extract lead + draft reply (structured outputs)
gsp/pipeline.py    Stage 1 logic, guardrails, booking hand-off, reminders
gsp/senders.py     Gmail, Twilio, Google Voice text-by-email
gsp/gmail.py       Gmail API read/label/send + one-time auth
gsp/store.py       SQLite storage
gsp/app.py         Web app: webhooks, /admin, background jobs
config/response_parameters.md   The rules for replies, in plain English
```

## Endpoints

- `GET /admin`: approval queue, needs-a-person list, all leads, sample-message tester (login required)
- `GET /api/leads?status=call_scheduled`: lead records as JSON (login required; Stage 2 reads this)
- `POST /webhooks/twilio/sms`: Twilio inbound texts (signature checked)
- `POST /webhooks/calendly`: bookings and cancellations (signature checked)

## Plugging in Stage 2

Stage 2 picks up leads with `status = call_scheduled` (`stage = 2`). Each one already has a
`summary`, the known `details`, and `missing_details`, which is the call agenda. The `roof` and
`racking_system` fields are there for Stage 2 to fill in and Stage 5 to use.
