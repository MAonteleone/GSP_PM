"""Settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load .env when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    business_name: str = field(default_factory=lambda: os.getenv("BUSINESS_NAME", "Georgia Solar Panels"))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(ROOT / "data" / "leads.db")))

    # Response rules (the "define response parameters" item from the notebook)
    response_parameters_path: str = field(
        default_factory=lambda: os.getenv(
            "RESPONSE_PARAMETERS_PATH", str(ROOT / "config" / "response_parameters.md")
        )
    )
    booking_link: str = field(default_factory=lambda: os.getenv("BOOKING_LINK", ""))

    # Sending. AUTO_SEND=false means every reply waits in /admin for one-tap approval.
    auto_send: bool = field(default_factory=lambda: _bool("AUTO_SEND", False))
    # sms provider: "twilio" | "gvoice_email" | "none"
    sms_provider: str = field(default_factory=lambda: os.getenv("SMS_PROVIDER", "gvoice_email"))
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", ""))
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", ""))
    twilio_from_number: str = field(default_factory=lambda: os.getenv("TWILIO_FROM_NUMBER", ""))

    # Gmail
    gmail_enabled: bool = field(default_factory=lambda: _bool("GMAIL_ENABLED", False))
    gmail_credentials_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_CREDENTIALS_FILE", str(ROOT / "secrets" / "credentials.json"))
    )
    gmail_token_file: str = field(
        default_factory=lambda: os.getenv("GMAIL_TOKEN_FILE", str(ROOT / "secrets" / "token.json"))
    )
    gmail_poll_seconds: int = field(default_factory=lambda: int(os.getenv("GMAIL_POLL_SECONDS", "60")))
    gmail_query: str = field(
        default_factory=lambda: os.getenv("GMAIL_QUERY", "in:inbox newer_than:2d -label:gsp-processed")
    )
    business_email: str = field(default_factory=lambda: os.getenv("BUSINESS_EMAIL", ""))

    # People to tell about new leads and bookings (comma-separated emails)
    notify_emails: list[str] = field(
        default_factory=lambda: [e.strip() for e in os.getenv("NOTIFY_EMAILS", "").split(",") if e.strip()]
    )

    # Booking webhook (Calendly signing key)
    calendly_signing_key: str = field(default_factory=lambda: os.getenv("CALENDLY_SIGNING_KEY", ""))

    # Reminder if they haven't booked
    reminder_after_hours: int = field(default_factory=lambda: int(os.getenv("REMINDER_AFTER_HOURS", "24")))

    # /admin login
    admin_user: str = field(default_factory=lambda: os.getenv("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))

    # Claude
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    ai_mode: str = field(default_factory=lambda: os.getenv("AI_MODE", "claude"))  # "claude" | "fake"

    def response_parameters(self) -> str:
        return Path(self.response_parameters_path).read_text(encoding="utf-8")
