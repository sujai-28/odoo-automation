from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "portal.db"
SESSION_SECRET_PATH = ROOT / "portal-data" / "session_secret.key"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
load_dotenv(ROOT / ".env")


def _ensure_column(con: sqlite3.Connection, existing: set[str], name: str, definition: str) -> None:
    if name not in existing:
        con.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              ginesys_user_code INTEGER NOT NULL,
              role TEXT NOT NULL DEFAULT 'operator',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        _ensure_column(con, columns, "display_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, columns, "ginesys_user_code", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(con, columns, "role", "TEXT NOT NULL DEFAULT 'operator'")
        _ensure_column(con, columns, "active", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(con, columns, "created_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, columns, "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, columns, "ginesys_username", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, columns, "ginesys_password_encrypted", "TEXT")
        _ensure_column(con, columns, "ginesys_token_encrypted", "TEXT")
        _ensure_column(con, columns, "ginesys_token_expires_at", "TEXT")
        _ensure_column(con, columns, "ginesys_connection_status", "TEXT NOT NULL DEFAULT 'not_configured'")
        _ensure_column(con, columns, "ginesys_last_authenticated_at", "TEXT")
        _ensure_column(con, columns, "ginesys_last_error", "TEXT")
        con.commit()
        yield con
        con.commit()
    finally:
        con.close()


def _clean_user(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["active"] = bool(data.get("active"))
    data["ginesys_user_code"] = int(data["ginesys_user_code"])
    return data


def public_user(row: sqlite3.Row | dict | None) -> dict | None:
    user = _clean_user(row)
    if not user:
        return None
    user.pop("password_hash", None)
    user["ginesys_credentials_configured"] = bool(
        user.get("ginesys_username") and user.get("ginesys_password_encrypted")
    )
    for field in ("ginesys_password_encrypted", "ginesys_token_encrypted", "ginesys_token_expires_at"):
        user.pop(field, None)
    return user


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cipher() -> Fernet:
    key = os.getenv("GINESYS_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        raise ValueError("GINESYS_CREDENTIAL_ENCRYPTION_KEY is not configured in .env.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("GINESYS_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def _encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored Ginesys credentials cannot be decrypted with the configured encryption key.") from exc


def get_ginesys_auth(user_id: int | str) -> dict:
    user = get_user(user_id)
    if not user:
        raise ValueError("User not found.")
    username = str(user.get("ginesys_username") or "").strip()
    encrypted = user.get("ginesys_password_encrypted")
    if not username or not encrypted:
        raise ValueError("Ginesys credentials are not configured for this portal user.")
    return {
        "user_id": user["id"], "username": username, "password": _decrypt(encrypted),
        "token": _decrypt(user.get("ginesys_token_encrypted")),
        "token_expires_at": user.get("ginesys_token_expires_at"),
    }


def save_ginesys_token(user_id: int | str, token: str, expires_at: str, authenticated_at: str | None = None) -> None:
    now = authenticated_at or _now()
    with _conn() as con:
        con.execute(
            """UPDATE users SET ginesys_token_encrypted=?, ginesys_token_expires_at=?,
               ginesys_connection_status='connected', ginesys_last_authenticated_at=?,
               ginesys_last_error=NULL, updated_at=? WHERE id=?""",
            (_encrypt(token), expires_at, now, now, int(user_id)),
        )


def set_ginesys_connection_error(user_id: int | str, message: str) -> None:
    now = _now()
    with _conn() as con:
        con.execute(
            """UPDATE users SET ginesys_token_encrypted=NULL, ginesys_token_expires_at=NULL,
               ginesys_connection_status='failed', ginesys_last_error=?, updated_at=? WHERE id=?""",
            (str(message)[:1000], now, int(user_id)),
        )


def user_count() -> int:
    with _conn() as con:
        return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user(user_id: int | str | None) -> dict | None:
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    with _conn() as con:
        return _clean_user(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def list_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return [public_user(row) for row in rows]


def authenticate(username: str, password: str) -> dict | None:
    username = str(username or "").strip().casefold()
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE lower(username)=? AND active=1", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password or ""):
        return None
    return _clean_user(row)


def get_user_by_ginesys_username(username: str) -> dict | None:
    username = str(username or "").strip().casefold()
    if not username:
        return None
    with _conn() as con:
        row = con.execute(
            """SELECT * FROM users
               WHERE active=1 AND (lower(ginesys_username)=? OR (ginesys_username='' AND lower(username)=?))
               ORDER BY CASE WHEN lower(ginesys_username)=? THEN 0 ELSE 1 END LIMIT 1""",
            (username, username, username),
        ).fetchone()
    return _clean_user(row)


def save_ginesys_login(user_id: int | str, username: str, password: str, token: str,
                        expires_at: str, authenticated_at: str, ginesys_user_code: int | str | None = None) -> dict:
    """Persist credentials and the token only after Ginesys accepted the login."""
    with _conn() as con:
        try:
            authenticated_user_code = int(ginesys_user_code or 0)
        except (TypeError, ValueError):
            authenticated_user_code = 0
        con.execute(
            """UPDATE users SET ginesys_username=?, ginesys_password_encrypted=?,
               ginesys_token_encrypted=?, ginesys_token_expires_at=?,
               ginesys_user_code=CASE WHEN ?>0 THEN ? ELSE ginesys_user_code END,
               ginesys_connection_status='connected', ginesys_last_authenticated_at=?,
               ginesys_last_error=NULL, updated_at=? WHERE id=?""",
            (username, _encrypt(password), _encrypt(token), expires_at,
             authenticated_user_code, authenticated_user_code,
             authenticated_at, authenticated_at, int(user_id)),
        )
        row = con.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    return _clean_user(row)


def create_user(*, username: str | None = None, display_name: str, password: str | None = None, ginesys_user_code: int | str | None = None,
                ginesys_username: str, ginesys_password: str | None = None, role: str = "operator") -> dict:
    username = str(username or ginesys_username or "").strip()
    display_name = str(display_name or "").strip()
    role = str(role or "operator").strip().casefold()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3-64 characters and contain only letters, numbers, dot, dash, or underscore.")
    if not display_name:
        raise ValueError("Display name is required.")
    if password is None:
        password = secrets.token_urlsafe(32)
    elif len(password) < 8:
        raise ValueError("Password must contain at least 8 characters.")
    try:
        ginesys_user_code = int(ginesys_user_code or 0)
    except (TypeError, ValueError):
        ginesys_user_code = 0
    ginesys_username = str(ginesys_username or "").strip()
    if not ginesys_username:
        raise ValueError("Ginesys username is required.")
    if role not in {"admin", "operator"}:
        raise ValueError("Role must be admin or operator.")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _conn() as con:
            cur = con.execute(
                """
                INSERT INTO users(username, display_name, password_hash, ginesys_user_code,
                  ginesys_username, ginesys_password_encrypted, ginesys_connection_status,
                  role, active, created_at, updated_at)
                VALUES(?,?,?,?,?,?,'not_tested',?,1,?,?)
                """,
                (username, display_name, generate_password_hash(password), ginesys_user_code,
                 ginesys_username, _encrypt(ginesys_password) if ginesys_password else None, role, now, now),
            )
            row = con.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"Username '{username}' already exists.") from exc
    return _clean_user(row)


def update_user(
    user_id: int | str,
    *,
    display_name: str,
    ginesys_user_code: int | str | None,
    ginesys_username: str,
    ginesys_password: str | None,
    role: str,
    password: str | None = None,
) -> dict:
    try:
        user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid user id.") from exc
    display_name = str(display_name or "").strip()
    role = str(role or "operator").strip().casefold()
    if not display_name:
        raise ValueError("Display name is required.")
    ginesys_username = str(ginesys_username or "").strip()
    if not ginesys_username:
        raise ValueError("Ginesys username is required.")
    if role not in {"admin", "operator"}:
        raise ValueError("Role must be admin or operator.")
    if password and len(password) < 8:
        raise ValueError("Password must contain at least 8 characters.")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found.")
        if row["role"] == "admin" and role != "admin":
            remaining = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (user_id,)).fetchone()[0]
            if remaining == 0:
                raise ValueError("At least one active admin user must remain.")
        credentials_changed = ginesys_username != str(row["ginesys_username"] or "") or bool(ginesys_password)
        if credentials_changed:
            con.execute(
                """UPDATE users SET display_name=?, ginesys_username=?, role=?,
                   ginesys_token_encrypted=NULL, ginesys_token_expires_at=NULL,
                   ginesys_connection_status='not_tested', ginesys_last_error=NULL, updated_at=? WHERE id=?""",
                (display_name, ginesys_username, role, now, user_id),
            )
        else:
            con.execute(
                "UPDATE users SET display_name=?, role=?, updated_at=? WHERE id=?",
                (display_name, role, now, user_id),
            )
        if ginesys_password:
            con.execute(
                "UPDATE users SET ginesys_password_encrypted=? WHERE id=?",
                (_encrypt(ginesys_password), user_id),
            )
        if password:
            con.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (generate_password_hash(password), now, user_id))
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _clean_user(row)


def set_active(user_id: int | str, active: bool) -> dict:
    try:
        user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid user id.") from exc
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found.")
        if not active and row["role"] == "admin":
            remaining = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (user_id,)).fetchone()[0]
            if remaining == 0:
                raise ValueError("At least one active admin user must remain.")
        now = datetime.now().isoformat(timespec="seconds")
        con.execute("UPDATE users SET active=?, updated_at=? WHERE id=?", (1 if active else 0, now, user_id))
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _clean_user(row)


def get_session_secret() -> str:
    configured = os.getenv("PORTAL_SECRET_KEY", "").strip()
    if configured:
        return configured
    SESSION_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_SECRET_PATH.exists():
        saved = SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    generated = secrets.token_urlsafe(48)
    SESSION_SECRET_PATH.write_text(generated, encoding="utf-8")
    return generated
