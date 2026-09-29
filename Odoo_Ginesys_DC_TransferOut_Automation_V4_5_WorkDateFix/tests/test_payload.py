import sys
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_loader import load_config
from ginesys_client import GinesysAuthenticationError, GinesysClient, GinesysError


class FakeResponse:
    def __init__(self, body, status=200, reason="OK"):
        self.body, self.status_code, self.reason, self.text = body, status, reason, ""
    def json(self):
        return self.body


class FakeSession:
    def __init__(self, *responses):
        self.headers = {}
        self.responses = list(responses)
        self.requests = []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


def sample_doc():
    return {
        "document_key": "a" * 64, "reference": "TEST/001", "date": "2026-08-11",
        "store_name": "EBO Store -Tiruppur", "site_code": 743, "state_code": "TN",
        "gst_state_code": "33", "site_tax_code": 4, "site_gstin": "33AAFCT5162N1Z1",
        "site_ou_code": 1, "invoice_doc_code": 215, "agent_code": None, "transporter_code": None,
        "items": [{"sku": "TS41854", "barcode": "8905639210627", "qty": 1.0, "factor": 60.0}],
    }


def priced_item():
    return {
        "itemId": "TS41854", "barcode": "8905639210627", "mrp": 699, "listedMrp": 699,
        "basicRate": 699, "discountFactor": 60, "discount": 419.4, "rateCal": 279.6,
        "wsp": 250, "costRate": 250, "negativeStockAlert": "I", "itemManagementMode": "I",
    }


def config():
    return replace(load_config(), api_token="TEST_ONLY")


class WebApiPayloadTests(unittest.TestCase):
    def test_bearer_header_and_scheme_defaults(self):
        session = FakeSession()
        client = GinesysClient(config(), session)
        self.assertEqual(session.headers["Authorization"], "Bearer TEST_ONLY")
        self.assertNotIn("Ginesys_Api_Key", session.headers)
        self.assertEqual(client.config.doc_code_tn, 215)
        self.assertEqual(client.config.doc_code_interstate, 215)
        self.assertEqual(client.config.gst_appl, "Y")

    def test_transfer_out_payload_is_gst_applicable(self):
        detail = {
            "challanCode": 828089, "challanNo": "DC/1", "dcDataVersion": 9,
            "itemDetails": [{
                "dcDetCode": 12, "itemId": "TS41854", "barcode": "8905639210627",
                "quantity": 1, "rate": 279.6, "mrp": 699, "factor": 60,
            }],
        }
        payload = GinesysClient(config(), FakeSession()).invoice_payload(sample_doc(), detail, {})
        self.assertEqual(payload["gSTAppl"], "Y")

    def test_transfer_out_payload_uses_logged_in_ginesys_user_code(self):
        detail = {
            "challanCode": 828089, "challanNo": "DC/1", "dcDataVersion": 9,
            "itemDetails": [{
                "dcDetCode": 12, "itemId": "TS41854", "barcode": "8905639210627",
                "quantity": 1, "rate": 279.6, "mrp": 699, "factor": 60,
            }],
        }
        payload = GinesysClient(config(), FakeSession()).invoice_payload(
            sample_doc(), detail, {}, operator={"ginesys_user_code": 64230}
        )
        self.assertEqual(payload["createdBy"], 64230)

    def test_select_item_exact_barcode_and_full_form(self):
        body = {"success": True, "result": {"data": [priced_item(), {**priced_item(), "barcode": "OTHER"}]}}
        session = FakeSession(FakeResponse(body))
        client = GinesysClient(config(), session)
        selected = client.lookup_item(sample_doc(), sample_doc()["items"][0])
        self.assertEqual(selected["itemId"], "TS41854")
        sent = session.requests[0][2]["json"]
        self.assertEqual(sent["criterias"][0]["value"], "8905639210627")
        self.assertEqual(sent["formParam"]["priceListCode"], 12)
        self.assertEqual(sent["formParam"]["documentStockPoint"], 650443)

    def test_select_item_multiple_exact_matches_block(self):
        body = {"success": True, "result": [{**priced_item()}, {**priced_item(), "mrp": 799, "basicRate": 799}]}
        client = GinesysClient(config(), FakeSession(FakeResponse(body)))
        with self.assertRaisesRegex(GinesysError, "2 exact matches"):
            client.lookup_item(sample_doc(), sample_doc()["items"][0])

    def test_exact_barcode_accepts_different_ginesys_sku_unconditionally(self):
        source = {"sku": "MPOR89BLK2XL", "barcode": "8905639104469", "qty": 1.0}
        returned = {**priced_item(), "itemId": "TS23216", "barcode": "8905639104469"}
        client = GinesysClient(config(), FakeSession(FakeResponse({"result": [returned]})))
        selected = client.lookup_item(sample_doc(), source)
        self.assertEqual(selected["itemId"], "TS23216")

    def test_dc_save_payload_uses_lookup_pricing_and_marker(self):
        client = GinesysClient(config(), FakeSession())
        payload = client.dc_payload(
            sample_doc(), [priced_item()], {"dc_code": 828089, "packet_barcode": "001828089"}
        )
        line = payload["items"][0]
        self.assertEqual(payload["code"], 828089)
        self.assertEqual(payload["packetBarcode"], "001828089")
        self.assertEqual(payload["ownerSiteCode"], 228)
        self.assertEqual(payload["docCode"], 210)
        self.assertEqual(payload["outStockpointCode"], 650443)
        self.assertIsNone(payload["createdBy"])
        self.assertEqual(line["basicRate"], 699)
        self.assertEqual(line["rate"], 279.6)
        self.assertEqual(line["discountFactor"], 60)
        self.assertIn("ODOO-", payload["remarks"])

    def test_dc_save_payload_uses_logged_in_ginesys_user_code(self):
        client = GinesysClient(config(), FakeSession())
        payload = client.dc_payload(
            sample_doc(), [priced_item()], {"dc_code": 828089, "packet_barcode": "001828089"},
            operator={"ginesys_user_code": 587300},
        )
        self.assertEqual(payload["createdBy"], 587300)

    def test_post_dc_allocates_code_and_packet_barcode_before_save(self):
        allocation = {"success": True, "result": {"code": 828089, "label": "001828089"}}
        saved = {
            "success": True,
            "result": {"dcCode": 828089, "schemeDocNo": "DC/08/26/2650", "dcBarcode": "001828089"},
        }
        session = FakeSession(FakeResponse(allocation), FakeResponse(saved))
        result = GinesysClient(config(), session).post_dc(sample_doc(), [priced_item()])
        self.assertEqual(result["dc_code"], 828089)
        self.assertEqual(result["dc_barcode"], "001828089")
        self.assertTrue(session.requests[0][1].endswith("/WebAPI/api/DC/GetPacketBarcode"))
        self.assertEqual(session.requests[0][0], "GET")
        save_payload = session.requests[1][2]["json"]
        self.assertEqual(save_payload["code"], 828089)
        self.assertEqual(save_payload["packetBarcode"], "001828089")

    def test_invalid_packet_allocation_blocks_dc_save(self):
        session = FakeSession(FakeResponse({"success": True, "result": {"code": 0, "label": ""}}))
        client = GinesysClient(config(), session)
        with self.assertRaisesRegex(GinesysError, "non-zero challan code"):
            client.post_dc(sample_doc(), [priced_item()])
        self.assertEqual(len(session.requests), 1)

    def test_get_dc_details_posts_integer_array_and_reconciles(self):
        detail = {"challanCode": 828089, "challanNo": "DC/1", "dcDataVersion": 9, "itemDetails": [{"barcode": "8905639210627", "quantity": 1}]}
        session = FakeSession(FakeResponse({"success": True, "result": [detail]}))
        client = GinesysClient(config(), session)
        self.assertEqual(client.get_dc_details(sample_doc(), 828089)["challanNo"], "DC/1")
        self.assertEqual(session.requests[0][2]["json"], [828089])
        self.assertEqual(session.requests[0][2]["params"]["tradeGrpCode"], 2)

    def test_get_adhoc_list_uses_full_site_mapping_and_captured_paging_envelope(self):
        session = FakeSession(FakeResponse({"success": True, "result": {"data": []}}))
        client = GinesysClient(config(), session)
        client.get_adhoc_list("2026-08-11", page=1, limit=50)
        sent = session.requests[0][2]["json"]
        self.assertEqual(len(sent["availableSites"]), 71)
        self.assertIn(228, sent["availableSites"])
        self.assertIn(782, sent["availableSites"])
        self.assertEqual(sent["connectedSite"], "228")
        self.assertEqual(sent["connectedSiteOUCode"], 1)
        self.assertEqual(sent["workDate"], "2026-08-11T00:00:00")
        self.assertEqual((sent["page"], sent["start"], sent["limit"]), (1, 0, 50))
        self.assertEqual(
            sent["sort"],
            '[{"property":"deliveryDate","direction":"DESC"},{"property":"deliveryNo","direction":"DESC"}]',
        )

    def test_batch_duplicate_check_matches_each_exact_odoo_reference_with_one_list_call(self):
        second = {**sample_doc(), "document_key": "b" * 64, "reference": "TEST/002"}
        rows = [
            {
                "dcCode": 91, "deliveryNo": "DC/91", "siteCode": 743,
                "odooReference": "TEST/001", "invoiceCode": 191, "invoiceNo": "TO/191",
            },
            {
                "dcCode": 92, "deliveryNo": "DC/92", "siteCode": 743,
                "odooReference": "TEST/002", "status": "Pending",
            },
        ]
        session = FakeSession(FakeResponse({"success": True, "result": {"data": rows}}))
        found = GinesysClient(config(), session).find_existing_documents([sample_doc(), second])
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(found[sample_doc()["document_key"]]["transfer_number"], "TO/191")
        self.assertEqual(found[second["document_key"]]["dc_number"], "DC/92")

    def test_default_posting_day_is_active_work_date_not_odoo_document_date(self):
        client = GinesysClient(config(), FakeSession())
        self.assertEqual(client._posting_day(sample_doc()), date.today().isoformat())
        self.assertNotEqual(client._posting_day(sample_doc()), sample_doc()["date"])

    def test_configured_work_date_overrides_today(self):
        client = GinesysClient(replace(config(), work_date="2026-08-15"), FakeSession())
        self.assertEqual(client._posting_day(sample_doc()), "2026-08-15")

    def test_charge_and_si_payload_keep_distinct_hsn_fields(self):
        detail = {
            "challanCode": 828089, "challanNo": "DC/1", "dcDataVersion": 99,
            "itemDetails": [{
                "dcDetCode": 12, "itemId": "TS41854", "barcode": "8905639210627", "mrp": 699,
                "hsnsacCode": 735, "hsnsacName": "42021220", "rate": 279.6, "quantity": 2,
                "gstItcAppl": "IP", "dcInLocationCode": 5, "factor": 60, "discount": 419.4,
                "basicRate": 699,
            }],
        }
        charges = {"challanChargeDetials": [{"itemDetails": [{"orderDetCode": 12, "effamt": 559.2, "itemCharges": [{
            "chargeCode": 7, "chargeAmount": 27.96, "appAmount": 559.2, "rate": 5,
            "sign": "+", "gLCode": 1602, "sLCode": 734, "basis": "P", "formulae": "B",
            "operationLevel": "L", "isTax": "Y", "source": "G", "gSTComponent": "IGST",
        }]}]}]}
        client = GinesysClient(config(), FakeSession())
        self.assertEqual(client.charge_payload(detail)["transactionItems"][0]["hsnsacCode"], 735)
        invoice = client.invoice_payload(sample_doc(), detail, charges)
        self.assertEqual(invoice["items"][0]["hsnSacCode"], "42021220")
        self.assertEqual(invoice["items"][0]["discount"], 419.4)
        self.assertEqual(invoice["items"][0]["invQty"], 2)
        self.assertEqual(invoice["items"][0]["invAmount"], 559.2)
        self.assertEqual(invoice["docSchemeCode"], 215)
        self.assertEqual(invoice["documentDate"], "2026-08-11T00:00:00")
        self.assertEqual(invoice["tradeGroupCode"], 2)
        self.assertEqual(invoice["sICharges"][0]["chargeCode"], 7)
        self.assertEqual(invoice["sICharges"][0]["chargeAmt"], 27.96)
        self.assertEqual(invoice["sICharges"][0]["appAmount"], 559.2)
        self.assertEqual(invoice["items"][0]["itemCharges"][0]["gLCode"], 1602)
        self.assertEqual(invoice["items"][0]["itemCharges"][0]["gSTComponent"], "IGST")
        self.assertEqual(invoice["items"][0]["itemCharges"][0]["isRoundOff"], "N")

    def test_401_has_expired_session_guidance(self):
        client = GinesysClient(config(), FakeSession(FakeResponse({"success": False, "message": "Unauthorized"}, 401, "Unauthorized")))
        with self.assertRaisesRegex(GinesysAuthenticationError, "expired"):
            client.validate_token()

    def test_401_renews_per_user_token_and_retries_once(self):
        session = FakeSession(
            FakeResponse({"success": False, "message": "Unauthorized"}, 401, "Unauthorized"),
            FakeResponse({"success": True, "result": {"data": []}}),
        )
        calls = []
        current = {"token": "old-token"}

        def provider(*, force=False):
            calls.append(force)
            if force:
                current["token"] = "new-token"
            return current["token"]

        client = GinesysClient(config(), session, token_provider=provider)
        self.assertTrue(client.validate_token())
        self.assertIn(True, calls)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.headers["Authorization"], "Bearer new-token")


if __name__ == "__main__":
    unittest.main()
