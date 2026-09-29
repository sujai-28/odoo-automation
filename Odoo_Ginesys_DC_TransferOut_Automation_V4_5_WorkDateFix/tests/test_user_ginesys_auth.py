import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_store
from ginesys_auth import GinesysLoginConflict, GinesysTokenManager, login_portal_user, request_ginesys_token


class Response:
    status_code = 200
    text = ""

    def json(self):
        return {"access_token": "per-user-token", "expires_in": 600, "userId": 64230}


class AuthSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


class ConflictResponse:
    def __init__(self, body, status=400):
        self.body, self.status_code, self.text = body, status, ""

    def json(self):
        return self.body


class ConflictSession:
    def __init__(self):
        self.responses = [
            ConflictResponse({"error": "invalid_grant", "error_description": "012|Already logged in<BR>Continue?|64230"}),
            ConflictResponse({"access_token": "new-token", "expires_in": 600}, 200),
        ]
        self.killed_user_id = None

    def post(self, *_args, **_kwargs):
        return self.responses.pop(0)

    def get(self, _url, **kwargs):
        self.killed_user_id = kwargs["params"]["userId"]
        return ConflictResponse({}, 200)


class UserGinesysAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = auth_store.DB_PATH
        auth_store.DB_PATH = Path(self.temp.name) / "portal.db"
        self.key = Fernet.generate_key().decode()
        self.env = patch.dict(os.environ, {"GINESYS_CREDENTIAL_ENCRYPTION_KEY": self.key})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        auth_store.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_password_and_token_are_encrypted_and_token_is_cached_per_user(self):
        user = auth_store.create_user(
            username="operator1", display_name="Operator One", password="portal-pass",
            ginesys_user_code=42, ginesys_username="GIN-OP-1", ginesys_password="ginesys-pass",
        )
        public = auth_store.public_user(user)
        self.assertNotIn("ginesys_password_encrypted", public)
        self.assertNotIn("ginesys_token_encrypted", public)

        session = AuthSession()
        manager = GinesysTokenManager("https://example.test", user["id"], session=session)
        self.assertEqual(manager.token(), "per-user-token")
        self.assertEqual(manager.token(), "per-user-token")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0].endswith("/WebAPI/token"))
        self.assertEqual(session.calls[0][1]["data"]["username"], "GIN-OP-1")

        stored = auth_store.get_user(user["id"])
        self.assertNotIn("ginesys-pass", stored["ginesys_password_encrypted"])
        self.assertNotIn("per-user-token", stored["ginesys_token_encrypted"])
        self.assertEqual(stored["ginesys_connection_status"], "connected")
        self.assertIsNotNone(stored["ginesys_last_authenticated_at"])

    def test_error_012_requires_confirmation_before_killing_existing_session(self):
        with self.assertRaises(GinesysLoginConflict) as raised:
            request_ginesys_token(
                "https://example.test", "GIN-OP-1", "secret", session=ConflictSession()
            )
        self.assertEqual(raised.exception.ginesys_user_id, 64230)
        self.assertNotIn("<BR>", str(raised.exception))

    def test_ginesys_user_code_is_captured_automatically_at_login(self):
        user = auth_store.create_user(
            display_name="Operator Two", ginesys_username="GIN-OP-2", role="operator"
        )
        logged_in = login_portal_user(
            "https://example.test", user, "GIN-OP-2", "ginesys-pass", session=AuthSession()
        )
        self.assertEqual(logged_in["ginesys_user_code"], 64230)

    def test_confirmed_error_012_kills_session_then_retries_login(self):
        session = ConflictSession()
        result = request_ginesys_token(
            "https://example.test", "GIN-OP-1", "secret", session=session,
            logout_existing_session=True,
        )
        self.assertEqual(session.killed_user_id, 64230)
        self.assertEqual(result["token"], "new-token")


if __name__ == "__main__":
    unittest.main()
