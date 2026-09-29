from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "portal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  user_id INTEGER,
  ginesys_username TEXT,
  display_name TEXT,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  details TEXT,
  ip_address TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at ON audit_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_user_id ON audit_events(user_id);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def log_event(*, action: str, status: str, user: dict | None = None, details: str = "",
              username: str | None = None, ip_address: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO audit_events(
                 occurred_at,user_id,ginesys_username,display_name,action,status,details,ip_address
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                user.get("id") if user else None,
                (user.get("ginesys_username") if user else None) or username,
                user.get("display_name") if user else None,
                str(action)[:80], str(status)[:30], str(details or "")[:2000], str(ip_address or "")[:80],
            ),
        )


def list_events(limit: int = 500) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]
