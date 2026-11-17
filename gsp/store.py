"""SQLite storage for leads, drafts, and processed-message ids.

Each lead is stored as one JSON document plus a few indexed columns for lookup.
Swap this class for Postgres/Supabase later without touching the pipeline.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from .models import Draft, DraftStatus, Lead, LeadStatus, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    phone TEXT,
    email TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS processed_messages (
    external_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ messages

    def claim_message(self, external_id: str) -> bool:
        """Mark a message as processed. Returns False if it was already handled."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO processed_messages VALUES (?, ?)", (external_id, utcnow().isoformat())
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ------------------------------------------------------------------ leads

    def save_lead(self, lead: Lead) -> None:
        lead.updated_at = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO leads (id, phone, email, status, created_at, updated_at, doc)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET phone=excluded.phone, email=excluded.email,
                     status=excluded.status, updated_at=excluded.updated_at, doc=excluded.doc""",
                (
                    lead.id,
                    lead.contact.phone,
                    (lead.contact.email or "").lower() or None,
                    lead.status.value,
                    lead.created_at.isoformat(),
                    lead.updated_at.isoformat(),
                    lead.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        row = self._conn.execute("SELECT doc FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return Lead.model_validate_json(row["doc"]) if row else None

    def find_lead(self, phone: Optional[str] = None, email: Optional[str] = None) -> Optional[Lead]:
        """Most recent open lead matching phone or email."""
        clauses, args = [], []
        if phone:
            clauses.append("phone = ?")
            args.append(phone)
        if email:
            clauses.append("email = ?")
            args.append(email.lower())
        if not clauses:
            return None
        row = self._conn.execute(
            f"SELECT doc FROM leads WHERE ({' OR '.join(clauses)}) AND status NOT IN ('not_a_lead') "
            "ORDER BY created_at DESC LIMIT 1",
            args,
        ).fetchone()
        return Lead.model_validate_json(row["doc"]) if row else None

    def list_leads(self, statuses: Optional[Iterable[LeadStatus]] = None, limit: int = 200) -> list[Lead]:
        if statuses:
            vals = [s.value for s in statuses]
            rows = self._conn.execute(
                f"SELECT doc FROM leads WHERE status IN ({','.join('?' * len(vals))}) "
                "ORDER BY created_at DESC LIMIT ?",
                (*vals, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT doc FROM leads ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Lead.model_validate_json(r["doc"]) for r in rows]

    # ------------------------------------------------------------------ drafts

    def save_draft(self, draft: Draft) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO drafts (id, lead_id, status, created_at, doc) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, doc=excluded.doc""",
                (draft.id, draft.lead_id, draft.status.value, draft.created_at.isoformat(), draft.model_dump_json()),
            )
            self._conn.commit()

    def get_draft(self, draft_id: str) -> Optional[Draft]:
        row = self._conn.execute("SELECT doc FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return Draft.model_validate_json(row["doc"]) if row else None

    def list_drafts(self, status: DraftStatus = DraftStatus.pending) -> list[Draft]:
        rows = self._conn.execute(
            "SELECT doc FROM drafts WHERE status = ? ORDER BY created_at", (status.value,)
        ).fetchall()
        return [Draft.model_validate_json(r["doc"]) for r in rows]

    def approval_streak(self) -> int:
        """How many of the most recent sent first replies went out with no edits."""
        rows = self._conn.execute(
            "SELECT doc FROM drafts WHERE status IN ('sent','rejected') ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        streak = 0
        for r in rows:
            d = Draft.model_validate_json(r["doc"])
            if d.kind != "first_reply":
                continue
            if d.status == DraftStatus.sent and not d.edited:
                streak += 1
            else:
                break
        return streak
