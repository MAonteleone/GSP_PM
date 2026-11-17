from gsp.models import Channel
from gsp.normalize import from_gmail, from_twilio, normalize_phone


def test_normalize_phone():
    assert normalize_phone("(404) 555-1234") == "+14045551234"
    assert normalize_phone("1-404-555-1234") == "+14045551234"
    assert normalize_phone("+1 404.555.1234") == "+14045551234"
    assert normalize_phone("555-1234") is None


def test_google_voice_text_forward():
    msg = from_gmail(
        message_id="m1",
        thread_id="t1",
        headers={
            "From": '"(404) 555-1234" <14045551234.17705550000.abcDEF@txt.voice.google.com>',
            "Subject": "New text message from (404) 555-1234",
            "Message-ID": "<abc@mail.gmail.com>",
        },
        body="Hi, do you install batteries? Our bill is like $280 in summer.\n\n"
        "To respond to this text message, reply to this email or visit Google Voice.\nYOUR ACCOUNT HELP CENTER",
    )
    assert msg.channel == Channel.sms
    assert msg.source == "gvoice"
    assert msg.from_phone == "+14045551234"
    assert msg.gv_reply_address.endswith("@txt.voice.google.com")
    assert "YOUR ACCOUNT" not in msg.body
    assert msg.body.startswith("Hi, do you install batteries?")


def test_google_voice_voicemail_and_missed_call():
    vm = from_gmail(
        message_id="m2",
        thread_id=None,
        headers={"From": "Google Voice <voice-noreply@google.com>",
                 "Subject": "New voicemail from (678) 555-0199 at 3:14 PM"},
        body="Hey this is Dana in Marietta, calling about solar for our house, call me back.\n\nPlay message",
    )
    assert vm.channel == Channel.voicemail
    assert vm.from_phone == "+16785550199"
    assert "Play message" not in vm.body

    missed = from_gmail(
        message_id="m3",
        thread_id=None,
        headers={"From": "voice-noreply@google.com", "Subject": "New missed call from (770) 555-0101"},
        body="",
    )
    assert missed.channel == Channel.missed_call
    assert missed.from_phone == "+17705550101"


def test_plain_email_strips_quoted_history_and_finds_phone():
    msg = from_gmail(
        message_id="m4",
        thread_id="t4",
        headers={"From": "Pat Lee <pat@example.com>", "Subject": "Solar quote?"},
        body="Looking at solar for our place in Athens. Call me at 706-555-0123.\n\n"
        "On Tue, Nov 10, 2026 someone wrote:\n> old stuff",
    )
    assert msg.channel == Channel.email
    assert msg.from_email == "pat@example.com"
    assert msg.from_phone == "+17065550123"
    assert "old stuff" not in msg.body


def test_skips_newsletters_and_own_mail():
    assert from_gmail(message_id="x", thread_id=None,
                      headers={"From": "news@vendor.com", "List-Unsubscribe": "<mailto:u@v>"}, body="hi") is None
    assert from_gmail(message_id="y", thread_id=None,
                      headers={"From": "no-reply@service.com"}, body="hi") is None
    assert from_gmail(message_id="z", thread_id=None,
                      headers={"From": "info@gsp.com"}, body="hi", business_email="info@gsp.com") is None


def test_twilio():
    msg = from_twilio({"MessageSid": "SM1", "From": "+14045551234", "Body": "solar?"})
    assert msg.external_id == "twilio:SM1"
    assert msg.from_phone == "+14045551234"
