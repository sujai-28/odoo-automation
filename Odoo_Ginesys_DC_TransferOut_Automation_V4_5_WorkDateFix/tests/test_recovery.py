import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import posting
from config_loader import load_config
from ginesys_client import GinesysAuthenticationError, GinesysError


def document(key="a", reference="REF1"):
    return {
        "document_key": key * 64, "reference": reference, "date": "2026-08-11", "store_name": "Test",
        "site_code": 743, "invoice_doc_code": 215, "line_count": 1, "total_qty": 1,
        "site_tax_code": 4, "gst_state_code": "33", "site_gstin": "33AAFCT5162N1Z1", "site_ou_code": 1,
        "items": [{"sku": "TS41854", "barcode": "8905639210627", "qty": 1}],
    }


class FakeRecoveryClient:
    calls = []
    def __init__(self, config): self.calls = []; self.api_calls = []; self.__class__.calls.append(self)
    def find_existing_document(self, doc, dc_code=None):
        self.calls.append(("find", dc_code))
        if dc_code: return {"dc_code": dc_code, "dc_number": "DC/1", "status": "Pending", "transfer_code": None, "transfer_number": None}
        return {"dc_code": 91, "dc_number": "DC/1", "status": "Pending", "transfer_code": None, "transfer_number": None}
    def post_invoice(self, doc, dc_code, dc_intg=None, operator=None):
        self.calls.append(("invoice", dc_code, operator))
        return {"payload_hash": "x", "transfer_code": 92, "transfer_number": "SIC/1"}


class FakeAuthClient(FakeRecoveryClient):
    def find_existing_document(self, doc, dc_code=None):
        self.calls.append(("find", dc_code))
        raise GinesysAuthenticationError("expired", 401)


class FakeRecoveryErrorClient(FakeRecoveryClient):
    def __init__(self, config):
        self.calls = []
        self.api_calls = []
        self.__class__.calls.append(self)
    def find_existing_document(self, doc, dc_code=None):
        self.calls.append({"endpoint": "/WebAPI/api/DC/GetAdhocList"})
        raise GinesysError("GetAdhocList failed", 500)


class FakePrecheckClient:
    def __init__(self, config): self.calls = [{"endpoint": "/WebAPI/api/DC/GetAdhocList"}]
    def find_existing_documents(self, docs, dc_codes=None):
        return {
            docs[0]["document_key"]: {
                "dc_code": 101, "dc_number": "DC/101", "status": "Invoiced",
                "transfer_code": 201, "transfer_number": "TO/201",
            },
            docs[1]["document_key"]: None,
        }


class FakeEmptyPrecheckClient:
    def __init__(self, config): self.calls = [{"endpoint": "/WebAPI/api/DC/GetAdhocList"}]
    def find_existing_documents(self, docs, dc_codes=None):
        return {doc["document_key"]: None for doc in docs}


class RecoveryTests(unittest.TestCase):
    def setUp(self): self.state = {}
    def get(self, key): return self.state.get(key)
    def save(self, key, **fields):
        row = self.state.setdefault(key, {})
        row.update(fields)

    def test_remote_pending_dc_resumes_without_new_dc_or_lookup(self):
        doc = document()
        cfg = replace(load_config(), api_token="TEST")
        with patch.object(posting, "GinesysClient", FakeRecoveryClient), patch.object(posting, "get_posting", self.get), patch.object(posting, "save_posting", self.save):
            result = posting.post_documents([doc], cfg)
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(self.state[doc["document_key"]]["dc_code"], 91)
        self.assertEqual(self.state[doc["document_key"]]["transfer_number"], "SIC/1")
        self.assertFalse(any(name == "lookup" for name, *_ in FakeRecoveryClient.calls[-1].calls))

    def test_auth_failure_stops_remaining_web_calls(self):
        docs = [document("a", "REF1"), document("b", "REF2")]
        cfg = replace(load_config(), api_token="TEST")
        with patch.object(posting, "GinesysClient", FakeAuthClient), patch.object(posting, "get_posting", self.get), patch.object(posting, "save_posting", self.save):
            result = posting.post_documents(docs, cfg)
        client = FakeAuthClient.calls[-1]
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.state[docs[0]["document_key"]]["status"], "AUTH_FAILED")
        self.assertEqual(self.state[docs[1]["document_key"]]["status"], "NOT_ATTEMPTED_AUTH")
        self.assertEqual(result["summary"]["failed"], 2)

    def test_read_only_recovery_failure_is_not_save_outcome_unknown(self):
        doc = document()
        cfg = replace(load_config(), api_token="TEST")
        with patch.object(posting, "GinesysClient", FakeRecoveryErrorClient), patch.object(posting, "get_posting", self.get), patch.object(posting, "save_posting", self.save):
            result = posting.post_documents([doc], cfg)
        self.assertEqual(self.state[doc["document_key"]]["status"], "RECOVERY_CHECK_FAILED")
        self.assertEqual(result["summary"]["failed"], 1)

    def test_validation_precheck_marks_existing_transfer_and_leaves_missing_reference_to_create(self):
        docs = [document("a", "REF1"), document("b", "REF2")]
        cfg = replace(load_config(), api_token="TEST")
        with patch.object(posting, "GinesysClient", FakePrecheckClient), patch.object(posting, "get_posting", self.get), patch.object(posting, "save_posting", self.save):
            result = posting.precheck_documents(docs, cfg)
        self.assertEqual(result["summary"], {"alreadyCreated": 1, "dcOnly": 0, "notFound": 1, "postingNeeded": 1})
        self.assertEqual(docs[0]["ginesys_precheck"]["status"], "TRANSFER_EXISTS")
        self.assertEqual(docs[1]["ginesys_precheck"]["status"], "NOT_FOUND")
        self.assertEqual(self.state[docs[0]["document_key"]]["status"], "SUCCESS")

    def test_validation_precheck_displays_recorded_ginesys_numbers_when_old_row_is_not_in_live_list(self):
        doc = document("a", "TEST/AUTO POST")
        self.state[doc["document_key"]] = {
            "status": "SUCCESS", "dc_code": 830114, "dc_number": "DC/08/26/2726",
            "transfer_code": 830115, "transfer_number": "TSWTO/08-26-2144",
        }
        cfg = replace(load_config(), api_token="TEST")
        with patch.object(posting, "GinesysClient", FakeEmptyPrecheckClient), patch.object(posting, "get_posting", self.get), patch.object(posting, "save_posting", self.save):
            result = posting.precheck_documents([doc], cfg)
        self.assertEqual(result["summary"]["alreadyCreated"], 1)
        self.assertEqual(doc["ginesys_precheck"]["status"], "TRANSFER_EXISTS")
        self.assertEqual(doc["ginesys_precheck"]["dc_number"], "DC/08/26/2726")
        self.assertEqual(doc["ginesys_precheck"]["transfer_number"], "TSWTO/08-26-2144")


if __name__ == "__main__": unittest.main()
