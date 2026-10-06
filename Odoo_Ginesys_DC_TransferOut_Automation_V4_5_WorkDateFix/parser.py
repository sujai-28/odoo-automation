from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any

import pandas as pd

from config_loader import AppConfig
from utils import canonical, clean_text, display_number, document_key, iso_date, normalize_barcode

COLUMN_OPTIONS = {
    "date": ["Invoice lines/Date", "Operations/Date", "invoice_line_ids/date"],
    "reference": ["Invoice lines/Number", "Operations/Reference", "invoice_line_ids/move_name"],
    "barcode": ["Invoice lines/Product/Barcode", "Operations/Product/Barcode", "invoice_line_ids/product_id/barcode"],
    "sku": ["Invoice lines/Product/Internal Reference", "Operations/Product/Internal Reference", "invoice_line_ids/product_id/default_code"],
    "quantity": ["Invoice lines/Quantity", "Operations/Qty Done", "invoice_line_ids/quantity"],
    "address": ["Delivery Address/Display Name", "Customer", "partner_shipping_id/display_name"],
    "bill_reference": ["Bill Reference", "Reference", "ref"],
}


def _read(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=object)
    return pd.read_excel(path, dtype=object)


def _find_column(df: pd.DataFrame, choices: list[str], required: bool, label: str) -> str | None:
    lookup = {canonical(c): c for c in df.columns}
    for choice in choices:
        found = lookup.get(canonical(choice))
        if found:
            return found
    if required:
        raise ValueError(f"Missing {label} column. Expected one of: {', '.join(choices)}")
    return None


def detect_source_type(df: pd.DataFrame) -> str:
    headers = {canonical(c) for c in df.columns}
    if canonical("Invoice lines/Number") in headers:
        return "INVOICE"
    if canonical("Operations/Reference") in headers:
        return "INTERNAL_TRANSFER"
    if canonical("invoice_line_ids/move_name") in headers:
        return "INVOICE"
    return "ODOO_EXPORT"


def load_combo_map(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {}
    df = pd.read_excel(path, dtype=object)
    df.columns = [clean_text(c) for c in df.columns]
    result: dict[str, list[dict[str, str]]] = {}
    for _, row in df.iterrows():
        combo_sku = clean_text(row.get("COMBO SKU"))
        combo_ean = normalize_barcode(row.get("COMBO EAN"))
        children = []
        for i in (1, 2):
            sku = clean_text(row.get(f"SKU{i}"))
            ean = normalize_barcode(row.get(f"EAN{i}"))
            if sku:
                children.append({"sku": sku, "barcode": ean})
        if children:
            if combo_sku:
                result[canonical(combo_sku)] = children
            if combo_ean:
                result[canonical(combo_ean)] = children
    return result


def _split_combo(combo_map: dict[str, list[dict[str, str]]], sku: str, barcode: str) -> list[dict[str, str]]:
    for key in (canonical(sku), canonical(barcode)):
        if key and key in combo_map:
            return combo_map[key]
    return [{"sku": sku, "barcode": barcode}]


def refresh_document_site_config(documents: list[dict], config: AppConfig) -> None:
    """Apply current site/GST metadata to new or retried documents and validate it."""
    gstin_issues: list[str] = []
    for doc in documents:
        site = config.get_site(doc.get("store_name", ""))
        if site is None:
            raise ValueError(f"No SITE_MASTER mapping for: {doc.get('store_name', '')}")
        doc.update({
            "site_code": site.code,
            "site_name": site.ginesys_name,
            "state_code": site.state_code,
            "gst_state_code": site.gst_state_code,
            "site_tax_code": site.site_tax_code,
            "site_gstin": site.gstin,
            "site_ou_code": site.ou_code,
            "gst_appl": "Y",
            "invoice_doc_code": config.transfer_doc_code_for(site),
            "agent_code": site.agent_code if site.agent_code is not None else config.agent_code,
            "transporter_code": site.transporter_code if site.transporter_code is not None else config.transporter_code,
        })
        if not site.gstin:
            gstin_issues.append(f"{site.odoo_name} (site {site.code}): GSTIN is blank")
        elif not re.fullmatch(r"[0-9A-Z]{15}", site.gstin.upper()):
            gstin_issues.append(f"{site.odoo_name} (site {site.code}): GSTIN '{site.gstin}' is not 15 characters")
        elif site.gst_state_code and not site.gstin.startswith(site.gst_state_code):
            gstin_issues.append(
                f"{site.odoo_name} (site {site.code}): GSTIN '{site.gstin}' does not match GST state {site.gst_state_code}"
            )
    if gstin_issues:
        raise ValueError(
            "GST posting is blocked because destination GSTIN validation failed: "
            + "; ".join(gstin_issues)
            + ". Correct Master/site_master.xlsx, the SITE_MASTER GSTIN column, or Master/site_webapi_overrides.json and validate/retry again."
        )


def parse_files(paths: list[Path], config: AppConfig, combo_path: Path) -> dict:
    combo_map = load_combo_map(combo_path)
    grouped: dict[str, dict] = {}
    warnings: list[str] = []
    stats = {"files": 0, "source_lines": 0, "valid_lines": 0, "skipped_lines": 0, "combo_lines": 0}
    row_issues: list[str] = []
    missing_sites: dict[str, int] = {}  # store_name -> first row number seen

    for path in paths:
        df = _read(path)
        if df.empty:
            warnings.append(f"{path.name}: no rows found.")
            continue
        stats["files"] += 1
        source_type = detect_source_type(df)
        date_col = _find_column(df, COLUMN_OPTIONS["date"], True, "date")
        ref_col = _find_column(df, COLUMN_OPTIONS["reference"], True, "reference")
        barcode_col = _find_column(df, COLUMN_OPTIONS["barcode"], True, "barcode")
        sku_col = _find_column(df, COLUMN_OPTIONS["sku"], False, "SKU/Internal Reference")
        qty_col = _find_column(df, COLUMN_OPTIONS["quantity"], True, "quantity")
        address_col = _find_column(df, COLUMN_OPTIONS["address"], True, "store/customer")
        bill_ref_col = _find_column(df, COLUMN_OPTIONS["bill_reference"], False, "bill reference")

        df[address_col] = df[address_col].ffill()
        stats["source_lines"] += len(df)

        for excel_row, (_, row) in enumerate(df.iterrows(), start=2):
            qty = pd.to_numeric(row.get(qty_col), errors="coerce")
            barcode = normalize_barcode(row.get(barcode_col))
            sku = clean_text(row.get(sku_col)) if sku_col else ""
            # Pure group/subtotal rows in Odoo exports have neither product identifier.
            # They remain benign. Any row that looks like an item must be complete;
            # otherwise validation blocks the whole upload instead of posting a partial DC.
            if not barcode and not sku:
                stats["skipped_lines"] += 1
                continue
            line_errors = []
            if not barcode:
                line_errors.append("barcode is blank")
            if pd.isna(qty):
                line_errors.append("quantity is not numeric")
            elif float(qty) <= 0:
                line_errors.append(f"quantity must be positive (found {qty})")
            ref = clean_text(row.get(ref_col))
            store_name = clean_text(row.get(address_col))
            doc_date = iso_date(row.get(date_col))
            if not ref:
                line_errors.append("document reference is blank")
            if not store_name:
                line_errors.append("store/customer is blank")
            if not doc_date:
                line_errors.append("document date is blank or invalid")
            if line_errors:
                row_issues.append(f"{path.name}, row {excel_row}: " + "; ".join(line_errors))
                stats["skipped_lines"] += 1
                continue
            site = config.get_site(store_name)
            if site is None:
                # Collect ALL missing stores — do not raise immediately.
                if store_name not in missing_sites:
                    missing_sites[store_name] = excel_row
                stats["skipped_lines"] += 1
                continue

            key = document_key(ref, site.code, doc_date, source_type)
            doc = grouped.get(key)
            if doc is None:
                doc = {
                    "document_key": key,
                    "source_type": source_type,
                    "source_files": [path.name],
                    "reference": ref,
                    "bill_reference": clean_text(row.get(bill_ref_col)) if bill_ref_col else "",
                    "date": doc_date,
                    "store_name": store_name,
                    "site_code": site.code,
                    "site_name": site.ginesys_name,
                    "state_code": site.state_code,
                    "gst_state_code": site.gst_state_code,
                    "site_tax_code": site.site_tax_code,
                    "site_gstin": site.gstin,
                    "site_ou_code": site.ou_code,
                    "gst_appl": site.gst_appl,
                    "invoice_doc_code": config.transfer_doc_code_for(site),
                    "agent_code": site.agent_code if site.agent_code is not None else config.agent_code,
                    "transporter_code": site.transporter_code if site.transporter_code is not None else config.transporter_code,
                    "items": [],
                }
                grouped[key] = doc
            elif path.name not in doc["source_files"]:
                doc["source_files"].append(path.name)

            children = _split_combo(combo_map, sku, barcode)
            if len(children) > 1:
                stats["combo_lines"] += 1
            for child in children:
                child_sku = clean_text(child.get("sku"))
                child_barcode = normalize_barcode(child.get("barcode"))
                doc["items"].append({
                    "sku": child_sku,
                    "barcode": child_barcode or barcode,
                    "qty": float(qty),
                    "factor": config.factor,
                })
            stats["valid_lines"] += 1

    # Raise all missing SITE_MASTER entries at once so the user can update them all in one go.
    if missing_sites:
        site_list = ", ".join(
            f"'{name}' (first seen row {row})" for name, row in sorted(missing_sites.items())
        )
        raise ValueError(
            f"SITE_MASTER mapping is missing for {len(missing_sites)} store(s). "
            "Please add ALL of the following to SITE_MASTER and reload before retrying: "
            + site_list
        )

    if row_issues:
        shown = row_issues[:20]
        suffix = f"; plus {len(row_issues) - 20} more issue(s)" if len(row_issues) > 20 else ""
        raise ValueError("Item-row validation failed; no document was posted. " + " | ".join(shown) + suffix)

    # Consolidate duplicate SKU lines within each document.
    docs = []
    for doc in grouped.values():
        item_groups: dict[tuple[str, str], dict] = {}
        for item in doc["items"]:
            k = (canonical(item["sku"]), canonical(item["barcode"]))
            if k not in item_groups:
                item_groups[k] = dict(item)
            else:
                item_groups[k]["qty"] += item["qty"]
        doc["items"] = sorted(item_groups.values(), key=lambda x: x["sku"])
        doc["total_qty"] = display_number(sum(float(i["qty"]) for i in doc["items"]))
        doc["line_count"] = len(doc["items"])
        docs.append(doc)

    docs.sort(key=lambda x: (x["date"], x["reference"], x["site_code"]))
    refresh_document_site_config(docs, config)
    return {"documents": docs, "warnings": warnings + config.warnings, "stats": stats, "combo_mappings": len(combo_map)}
