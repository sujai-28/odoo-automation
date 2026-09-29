import tempfile
import unittest
from pathlib import Path

import audit_store


class AuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = audit_store.DB_PATH
        audit_store.DB_PATH = Path(self.temp.name) / "portal.db"

    def tearDown(self):
        audit_store.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_header_level_event_is_persisted(self):
        audit_store.log_event(
            action="VALIDATE", status="SUCCESS",
            user={"id": 7, "ginesys_username": "GIN-7", "display_name": "Seven"},
            details="Job abc: 2 documents", ip_address="127.0.0.1",
        )
        event = audit_store.list_events()[0]
        self.assertEqual(event["action"], "VALIDATE")
        self.assertEqual(event["ginesys_username"], "GIN-7")
        self.assertEqual(event["details"], "Job abc: 2 documents")
