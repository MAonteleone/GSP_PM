"""Gmail: read new messages, mark them processed, and send email.

Uses the official Gmail API with an OAuth token stored at GMAIL_TOKEN_FILE.
Run `python -m gsp.gmail auth` once on a computer with a browser to create it.
"""

from __future__ import annotations

import base64
import html
import re
import sys
from email.message import EmailMessage
from typing import Any, Iterator, Optional

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read, label, send
PROCESSED_LABEL = "gsp-processed"


def build_service(credentials_file: str, token_file: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def run_auth_flow(credentials_file: str, token_file: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_file, "w") as f:
        f.write(creds.to_json())
    print(f"Saved Gmail token to {token_file}")


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to text/html converted to text."""
    plain, rich = [], []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    return _html_to_text("\n".join(rich))


class GmailClient:
    def __init__(self, service):
        self.service = service
        self._label_id: Optional[str] = None

    def processed_label_id(self) -> str:
        if self._label_id:
            return self._label_id
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label["name"] == PROCESSED_LABEL:
                self._label_id = label["id"]
                return self._label_id
        created = (
            self.service.users()
            .labels()
            .create(userId="me", body={"name": PROCESSED_LABEL, "labelListVisibility": "labelShow"})
            .execute()
        )
        self._label_id = created["id"]
        return self._label_id

    def new_messages(self, query: str) -> Iterator[dict[str, Any]]:
        """Yield {id, thread_id, headers, body} for messages matching the query, oldest first."""
        resp = self.service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        for ref in reversed(resp.get("messages", [])):
            full = self.service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
            yield {
                "id": full["id"],
                "thread_id": full.get("threadId"),
                "headers": headers,
                "body": extract_body(full["payload"]),
            }

    def mark_processed(self, message_id: str) -> None:
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [self.processed_label_id()]}
        ).execute()

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        sender: str = "",
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> str:
        msg = EmailMessage()
        msg["To"] = to
        if sender:
            msg["From"] = sender
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        sent = self.service.users().messages().send(userId="me", body=payload).execute()
        return sent["id"]


if __name__ == "__main__":  # python -m gsp.gmail auth
    from .config import Settings

    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        s = Settings()
        run_auth_flow(s.gmail_credentials_file, s.gmail_token_file)
    else:
        print("usage: python -m gsp.gmail auth")
