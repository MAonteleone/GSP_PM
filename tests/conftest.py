import pytest

from gsp.ai import FakeLeadAI
from gsp.config import Settings
from gsp.pipeline import Pipeline
from gsp.senders import OutboxMailer, RecordingSms
from gsp.store import Store


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = ":memory:"
    s.booking_link = "https://calendly.com/gsp/15min"
    s.auto_send = False
    s.sms_provider = "twilio"
    s.notify_emails = ["owner@example.com"]
    s.admin_password = "pw"
    s.calendly_signing_key = "calkey"
    s.twilio_auth_token = "twtoken"
    s.public_base_url = "https://gsp.example.com"
    s.ai_mode = "fake"
    return s


@pytest.fixture
def mailer():
    return OutboxMailer()


@pytest.fixture
def sms():
    return RecordingSms()


@pytest.fixture
def pipeline(settings, mailer, sms):
    return Pipeline(settings, Store(":memory:"), FakeLeadAI(), mailer, sms)
