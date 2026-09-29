from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "portal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
  document_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  payload_hash TEXT,
  dc_intg_id TEXT,
  invoice_intg_id TEXT,
  dc_code INTEGER,
  dc_number TEXT,
  transfer_code INTEGER,
  transfer_number TEXT,
  created_by_user_id INTEGER,
  created_by_portal_username TEXT,
  created_by_ginesys_username TEXT,
  created_by_ginesys_user_code INTEGER,
  dc_created_by_user_id INTEGER,
  dc_created_by_portal_username TEXT,
  dc_created_by_ginesys_username TEXT,
  dc_created_by_ginesys_user_code INTEGER,
  transfer_created_by_user_id INTEGER,
  transfer_created_by_portal_username TEXT,
  transfer_created_by_ginesys_username TEXT,
  transfer_created_by_ginesys_user_code INTEGER,
  error TEXT,
  updated_at TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute(SCHEMA)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(postings)").fetchall()}
        for name, definition in (
            ("created_by_user_id", "INTEGER"),
            ("created_by_portal_username", "TEXT"),
            ("created_by_ginesys_username", "TEXT"),
            ("created_by_ginesys_user_code", "INTEGER"),
            ("dc_created_by_user_id", "INTEGER"),
            ("dc_created_by_portal_username", "TEXT"),
            ("dc_created_by_ginesys_username", "TEXT"),
            ("dc_created_by_ginesys_user_code", "INTEGER"),
            ("transfer_created_by_user_id", "INTEGER"),
            ("transfer_created_by_portal_username", "TEXT"),
            ("transfer_created_by_ginesys_username", "TEXT"),
            ("transfer_created_by_ginesys_user_code", "INTEGER"),
        ):
            if name not in columns:
                con.execute(f"ALTER TABLE postings ADD COLUMN {name} {definition}")
        con.commit()
        yield con
        con.commit()
    finally:
        con.close()


def get_posting(document_key: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM postings WHERE document_key=?", (document_key,)).fetchone()
        return dict(row) if row else None


def save_posting(document_key: str, **fields):
    current = get_posting(document_key) or {}
    data = {
        "document_key": document_key,
        "status": fields.get("status", current.get("status", "PENDING")),
        "payload_hash": fields.get("payload_hash", current.get("payload_hash")),
        "dc_intg_id": fields.get("dc_intg_id", current.get("dc_intg_id")),
        "invoice_intg_id": fields.get("invoice_intg_id", current.get("invoice_intg_id")),
        "dc_code": fields.get("dc_code", current.get("dc_code")),
        "dc_number": fields.get("dc_number", current.get("dc_number")),
        "transfer_code": fields.get("transfer_code", current.get("transfer_code")),
        "transfer_number": fields.get("transfer_number", current.get("transfer_number")),
        "created_by_user_id": fields.get("created_by_user_id", current.get("created_by_user_id")),
        "created_by_portal_username": fields.get("created_by_portal_username", current.get("created_by_portal_username")),
        "created_by_ginesys_username": fields.get("created_by_ginesys_username", current.get("created_by_ginesys_username")),
        "created_by_ginesys_user_code": fields.get("created_by_ginesys_user_code", current.get("created_by_ginesys_user_code")),
        "dc_created_by_user_id": fields.get("dc_created_by_user_id", current.get("dc_created_by_user_id")),
        "dc_created_by_portal_username": fields.get("dc_created_by_portal_username", current.get("dc_created_by_portal_username")),
        "dc_created_by_ginesys_username": fields.get("dc_created_by_ginesys_username", current.get("dc_created_by_ginesys_username")),
        "dc_created_by_ginesys_user_code": fields.get("dc_created_by_ginesys_user_code", current.get("dc_created_by_ginesys_user_code")),
        "transfer_created_by_user_id": fields.get("transfer_created_by_user_id", current.get("transfer_created_by_user_id")),
        "transfer_created_by_portal_username": fields.get("transfer_created_by_portal_username", current.get("transfer_created_by_portal_username")),
        "transfer_created_by_ginesys_username": fields.get("transfer_created_by_ginesys_username", current.get("transfer_created_by_ginesys_username")),
        "transfer_created_by_ginesys_user_code": fields.get("transfer_created_by_ginesys_user_code", current.get("transfer_created_by_ginesys_user_code")),
        "error": fields.get("error", current.get("error")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _conn() as con:
        con.execute(
            """
            INSERT INTO postings(document_key,status,payload_hash,dc_intg_id,invoice_intg_id,dc_code,dc_number,transfer_code,transfer_number,
              created_by_user_id,created_by_portal_username,created_by_ginesys_username,created_by_ginesys_user_code,
              dc_created_by_user_id,dc_created_by_portal_username,dc_created_by_ginesys_username,dc_created_by_ginesys_user_code,
              transfer_created_by_user_id,transfer_created_by_portal_username,transfer_created_by_ginesys_username,transfer_created_by_ginesys_user_code,
              error,updated_at)
            VALUES(:document_key,:status,:payload_hash,:dc_intg_id,:invoice_intg_id,:dc_code,:dc_number,:transfer_code,:transfer_number,
              :created_by_user_id,:created_by_portal_username,:created_by_ginesys_username,:created_by_ginesys_user_code,
              :dc_created_by_user_id,:dc_created_by_portal_username,:dc_created_by_ginesys_username,:dc_created_by_ginesys_user_code,
              :transfer_created_by_user_id,:transfer_created_by_portal_username,:transfer_created_by_ginesys_username,:transfer_created_by_ginesys_user_code,
              :error,:updated_at)
            ON CONFLICT(document_key) DO UPDATE SET
              status=excluded.status,
              payload_hash=excluded.payload_hash,
              dc_intg_id=excluded.dc_intg_id,
              invoice_intg_id=excluded.invoice_intg_id,
              dc_code=excluded.dc_code,
              dc_number=excluded.dc_number,
              transfer_code=excluded.transfer_code,
              transfer_number=excluded.transfer_number,
              created_by_user_id=COALESCE(postings.created_by_user_id, excluded.created_by_user_id),
              created_by_portal_username=COALESCE(postings.created_by_portal_username, excluded.created_by_portal_username),
              created_by_ginesys_username=COALESCE(postings.created_by_ginesys_username, excluded.created_by_ginesys_username),
              created_by_ginesys_user_code=COALESCE(postings.created_by_ginesys_user_code, excluded.created_by_ginesys_user_code),
              dc_created_by_user_id=COALESCE(postings.dc_created_by_user_id, excluded.dc_created_by_user_id),
              dc_created_by_portal_username=COALESCE(postings.dc_created_by_portal_username, excluded.dc_created_by_portal_username),
              dc_created_by_ginesys_username=COALESCE(postings.dc_created_by_ginesys_username, excluded.dc_created_by_ginesys_username),
              dc_created_by_ginesys_user_code=COALESCE(postings.dc_created_by_ginesys_user_code, excluded.dc_created_by_ginesys_user_code),
              transfer_created_by_user_id=COALESCE(postings.transfer_created_by_user_id, excluded.transfer_created_by_user_id),
              transfer_created_by_portal_username=COALESCE(postings.transfer_created_by_portal_username, excluded.transfer_created_by_portal_username),
              transfer_created_by_ginesys_username=COALESCE(postings.transfer_created_by_ginesys_username, excluded.transfer_created_by_ginesys_username),
              transfer_created_by_ginesys_user_code=COALESCE(postings.transfer_created_by_ginesys_user_code, excluded.transfer_created_by_ginesys_user_code),
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            data,
        )
        con.commit()
