from __future__ import annotations

import threading
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from auth_store import get_ginesys_auth, save_ginesys_login, save_ginesys_token, set_ginesys_connection_error


_locks_guard = threading.Lock()
_user_locks: dict[int, threading.Lock] = {}


def _user_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("message") or body.get("error") or fallback)
    return fallback


def _plain_message(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " ", html.unescape(str(value)), flags=re.I)).strip()


class GinesysLoginConflict(ValueError):
    def __init__(self, message: str, ginesys_user_id: int):
        super().__init__(_plain_message(message))
        self.ginesys_user_id = int(ginesys_user_id)


def _parse_login_conflict(body: Any) -> tuple[str, int] | None:
    description = _message(body, "")
    parts = str(description).split("|")
    if len(parts) >= 3 and parts[0].strip() == "012":
        try:
            return parts[1].strip(), int(parts[2].strip())
        except (TypeError, ValueError):
            return None
    return None


def request_ginesys_token(base_url: str, username: str, password: str, *, timeout: int = 60,
                          session: requests.Session | None = None, logout_existing_session: bool = False) -> dict:
    """Authenticate exactly as the deployed Ginesys Web login client does."""
    http = session or requests.Session()
    token_url = f"{base_url.rstrip('/')}/WebAPI/token"

    def login_request():
        return http.post(
            token_url,
            data={"grant_type": "password", "username": username, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )

    try:
        response = login_request()
    except requests.RequestException as exc:
        raise ValueError(f"Could not connect to Ginesys authentication: {exc}") from exc
    try:
        body = response.json()
    except Exception:
        body = {"message": response.text[:1000]}

    conflict = _parse_login_conflict(body)
    if conflict:
        message, ginesys_user_id = conflict
        if not logout_existing_session:
            raise GinesysLoginConflict(message, ginesys_user_id)
        try:
            kill_response = http.get(
                f"{base_url.rstrip('/')}/WebAPI/api/Session/KillUser",
                params={"userId": ginesys_user_id},
                headers={"Authorization": f"Bearer {body.get('access_token', '') if isinstance(body, dict) else ''}",
                         "Accept": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ValueError(f"Could not connect to Ginesys session management: {exc}") from exc
        if kill_response.status_code not in (200, 201, 204):
            try:
                kill_body = kill_response.json()
            except Exception:
                kill_body = {"message": kill_response.text[:1000]}
            raise ValueError(_plain_message(_message(kill_body, f"Could not log out the existing Ginesys session (HTTP {kill_response.status_code}).")))
        try:
            response = login_request()
        except requests.RequestException as exc:
            raise ValueError(f"Could not reconnect to Ginesys authentication: {exc}") from exc
        try:
            body = response.json()
        except Exception:
            body = {"message": response.text[:1000]}

    token = body.get("access_token") if isinstance(body, dict) else None
    if response.status_code not in (200, 201) or not token:
        raise ValueError(_plain_message(_message(body, f"Ginesys authentication failed (HTTP {response.status_code}).")))
    try:
        lifetime = max(60, int(body.get("expires_in") or 1200))
    except (TypeError, ValueError):
        lifetime = 1200
    authenticated_at = _utcnow()
    return {
        "token": str(token),
        "authenticated_at": authenticated_at.isoformat(timespec="seconds"),
        "expires_at": (authenticated_at + timedelta(seconds=lifetime)).isoformat(timespec="seconds"),
        "body": body,
    }


def login_portal_user(base_url: str, user: dict, username: str, password: str, *, timeout: int = 60,
                      session: requests.Session | None = None, logout_existing_session: bool = False) -> dict:
    result = request_ginesys_token(
        base_url, username, password, timeout=timeout, session=session,
        logout_existing_session=logout_existing_session,
    )
    return save_ginesys_login(
        user["id"], username, password, result["token"], result["expires_at"], result["authenticated_at"],
        result["body"].get("userId") if isinstance(result.get("body"), dict) else None,
    )


class GinesysTokenManager:
    """Obtains and persists a short-lived Ginesys bearer token for one portal user."""

    def __init__(self, base_url: str, user_id: int, timeout: int = 60, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.user_id = int(user_id)
        self.timeout = timeout
        self.session = session or requests.Session()

    def token(self, *, force: bool = False) -> str:
        with _user_lock(self.user_id):
            auth = get_ginesys_auth(self.user_id)
            expiry = _parse_expiry(auth.get("token_expires_at"))
            if not force and auth.get("token") and expiry and expiry > _utcnow() + timedelta(seconds=60):
                return auth["token"]
            return self._authenticate(auth)

    def _authenticate(self, auth: dict) -> str:
        try:
            result = request_ginesys_token(
                self.base_url, auth["username"], auth["password"], timeout=self.timeout, session=self.session
            )
            save_ginesys_token(
                self.user_id, result["token"], result["expires_at"], result["authenticated_at"],
            )
            return result["token"]
        except requests.RequestException as exc:
            message = f"Could not connect to Ginesys authentication: {exc}"
            set_ginesys_connection_error(self.user_id, message)
            raise ValueError(message) from exc
        except Exception as exc:
            set_ginesys_connection_error(self.user_id, str(exc))
            raise
