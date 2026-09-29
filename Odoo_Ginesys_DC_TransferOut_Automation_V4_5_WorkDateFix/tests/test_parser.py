import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_loader import load_config
from parser import parse_files, refresh_document_site_config


class ConfigMappingTests(unittest.TestCase):
    def test_all_active_sites_have_numeric_gst_state(self):
        config = load_config()
        self.assertGreaterEqual(len(config.sites), 70)
        self.assertTrue(all(site.gst_state_code and site.gst_state_code.isdigit() for site in config.sites.values()))

    def test_same_state_uses_gst_scheme_215(self):
        config = load_config()
        same_state = next(site for site in config.sites.values() if site.gst_state_code == config.owner_state_code)
        self.assertEqual(config.transfer_doc_code_for(same_state), 215)

    def test_interstate_is_independently_configured(self):
        config = load_config()
        interstate = next(site for site in config.sites.values() if site.gst_state_code != config.owner_state_code)
        self.assertEqual(config.transfer_doc_code_for(interstate), 215)

    def test_all_transfers_are_gst_applicable(self):
        config = load_config()
        self.assertEqual(config.gst_appl, "Y")
        self.assertTrue(all(site.gst_appl == "Y" for site in config.sites.values()))

    def test_selected_site_without_gstin_is_blocked(self):
        config = load_config()
        key, site = next(iter(config.sites.items()))
        config = replace(config, sites=dict(config.sites))
        config.sites[key] = replace(site, gstin="")
        doc = {"store_name": site.odoo_name}
        with self.assertRaisesRegex(ValueError, "GSTIN is blank"):
            refresh_document_site_config([doc], config)

    def test_durgapur_gstin_is_loaded_from_separate_site_master(self):
        config = load_config()
        site = config.get_site("TECHNO SPORTSWEAR PRIVATE LIMITED(WB), EBO Store - Junction Mall Durgapur")
        self.assertEqual(site.gstin, "19AAFCT5162N1ZR")
        self.assertEqual(site.gst_state_code, "19")

    def test_wagholi_gst_state_is_inferred_from_gstin(self):
        config = load_config()
        site = config.get_site("EBO Store - Wagholi")
        self.assertEqual(site.gstin, "27AAFCT5162N1ZU")
        self.assertEqual(site.gst_state_code, "27")

    def test_incomplete_item_row_blocks_partial_posting(self):
        config = load_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.csv"
            path.write_text(
                "Operations/Date,Operations/Reference,Operations/Product/Barcode,Operations/Product/Internal Reference,Operations/Qty Done,Customer\n"
                "2026-08-12,REF1,,TS41854,1,EBO Store -Tiruppur\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "barcode is blank"):
                parse_files([path], config, ROOT / "Master" / "combo_mapping.xlsx")

    def test_group_row_without_product_remains_benign(self):
        config = load_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "grouped.csv"
            path.write_text(
                "Operations/Date,Operations/Reference,Operations/Product/Barcode,Operations/Product/Internal Reference,Operations/Qty Done,Customer\n"
                "2026-08-12,GROUP,,,,EBO Store -Tiruppur\n"
                "2026-08-12,REF1,8905639210627,TS41854,1,EBO Store -Tiruppur\n",
                encoding="utf-8",
            )
            parsed = parse_files([path], config, ROOT / "Master" / "combo_mapping.xlsx")
            self.assertEqual(len(parsed["documents"]), 1)
            self.assertEqual(parsed["stats"]["skipped_lines"], 1)

    def test_sku_column_is_optional_when_barcode_is_present(self):
        config = load_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "barcode_only.csv"
            path.write_text(
                "Operations/Date,Operations/Reference,Operations/Product/Barcode,Operations/Qty Done,Customer\n"
                "2026-08-12,REF1,8905639104469,1,EBO Store -Tiruppur\n",
                encoding="utf-8",
            )
            parsed = parse_files([path], config, ROOT / "Master" / "combo_mapping.xlsx")
            self.assertEqual(parsed["documents"][0]["items"][0]["barcode"], "8905639104469")
            self.assertEqual(parsed["documents"][0]["items"][0]["sku"], "")


if __name__ == "__main__":
    unittest.main()
