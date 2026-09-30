from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config_loader import AppConfig
from ginesys_client import GinesysAuthenticationError, GinesysClient, GinesysError
from storage import get_posting, save_posting
from utils import normalize_barcode


_DUPLICATE_DOC_NO_PATTERNS = (
    "duplicate value of document no",
    "duplicate document no",
    "document no. is not allowed",
    "document number already exists",
)


def _is_duplicate_document_no_error(exc: Exception) -> bool:
    """Return True when Ginesys rejected SI/Save because the document number already exists."""
    msg = str(exc).lower()
    return any(pat in msg for pat in _DUPLICATE_DOC_NO_PATTERNS)


def _row_from_state(doc: dict, state: dict, status_override: str | None = None) -> dict:
    return {
        "document_key": doc["document_key"], "reference": doc["reference"], "date": doc["date"],
        "store_name": doc["store_name"], "site_code": doc["site_code"],
        "invoice_doc_code": doc["invoice_doc_code"], "line_count": doc["line_count"],
        "total_qty": doc["total_qty"], "dc_code": state.get("dc_code"), "dc_number": state.get("dc_number"),
        "transfer_code": state.get("transfer_code"), "transfer_number": state.get("transfer_number"),
        "status": status_override or state.get("status"), "error": state.get("error") or "",
        "created_by_ginesys_username": state.get("created_by_ginesys_username"),
        "created_by_ginesys_user_code": state.get("created_by_ginesys_user_code"),
        "dc_created_by_ginesys_username": state.get("dc_created_by_ginesys_username"),
        "dc_created_by_ginesys_user_code": state.get("dc_created_by_ginesys_user_code"),
        "transfer_created_by_ginesys_username": state.get("transfer_created_by_ginesys_username"),
        "transfer_created_by_ginesys_user_code": state.get("transfer_created_by_ginesys_user_code"),
    }


_POST_LOCK = threading.Lock()


def _has_remote_transfer(remote: dict) -> bool:
    return bool(remote.get("transfer_number") or remote.get("transfer_code"))


def precheck_documents(documents: list[dict], config: AppConfig, operator: dict | None = None) -> dict:
    """Read-only duplicate validation before a job is offered for posting."""
    client = GinesysClient(config, operator=operator) if operator else GinesysClient(config)
    current = {doc["document_key"]: get_posting(doc["document_key"]) for doc in documents}
    dc_codes = {
        key: row.get("dc_code") if row else None
        for key, row in current.items()
    }
    remote_by_key = client.find_existing_documents(documents, dc_codes)
    results = []

    for doc in documents:
        key = doc["document_key"]
        local = current.get(key) or {}
        remote = remote_by_key.get(key)
        if remote:
            dc_code = remote.get("dc_code") or local.get("dc_code")
            dc_number = remote.get("dc_number") or local.get("dc_number")
            if _has_remote_transfer(remote):
                status = "TRANSFER_EXISTS"
                transfer_code = remote.get("transfer_code") or local.get("transfer_code")
                transfer_number = remote.get("transfer_number") or local.get("transfer_number")
                save_posting(
                    key, status="SUCCESS", dc_code=dc_code, dc_number=dc_number,
                    transfer_code=transfer_code, transfer_number=transfer_number,
                    error="Found in Ginesys during upload validation; no duplicate transaction will be created.",
                )
            else:
                remote_status = str(remote.get("status") or "").strip().casefold()
                if remote_status in {"invoiced", "invoice created", "completed", "complete"}:
                    raise GinesysError(
                        f"Ginesys reports DC {dc_number or dc_code} for Odoo reference {doc['reference']} as {remote.get('status')}, "
                        "but no Transfer Out number was returned. Manual review is required."
                    )
                status = "DC_EXISTS"
                transfer_code = transfer_number = None
                save_posting(key, status="DC_CREATED", dc_code=dc_code, dc_number=dc_number, error="")
        elif local.get("status") == "SUCCESS" and (local.get("transfer_number") or local.get("transfer_code")):
            # The live list was checked, but an older locally-recorded success may
            # be outside its current result window. It still must never be recreated.
            status = "TRANSFER_EXISTS"
            dc_code, dc_number = local.get("dc_code"), local.get("dc_number")
            transfer_code, transfer_number = local.get("transfer_code"), local.get("transfer_number")
        elif local.get("dc_code"):
            status = "DC_EXISTS"
            dc_code, dc_number = local.get("dc_code"), local.get("dc_number")
            transfer_code = transfer_number = None
        else:
            status = "NOT_FOUND"
            dc_code = dc_number = transfer_code = transfer_number = None

        check = {
            "status": status,
            "dc_code": dc_code, "dc_number": dc_number,
            "transfer_code": transfer_code, "transfer_number": transfer_number,
        }
        doc["ginesys_precheck"] = check
        results.append({"document_key": key, "reference": doc["reference"], **check})

    complete = sum(row["status"] == "TRANSFER_EXISTS" for row in results)
    dc_only = sum(row["status"] == "DC_EXISTS" for row in results)
    return {
        "results": results,
        "api_calls": client.calls,
        "summary": {
            "alreadyCreated": complete,
            "dcOnly": dc_only,
            "notFound": len(results) - complete - dc_only,
            "postingNeeded": len(results) - complete,
        },
    }


def _lookup_key(client: GinesysClient, doc: dict, item: dict) -> tuple:
    return (
        int(doc["site_code"]),
        int(doc.get("site_tax_code") or client.config.owner_site_tax_code),
        normalize_barcode(item.get("barcode")),
        client.config.price_type,
        client.config.price_list_code,
        client.config.factor,
        client._posting_day(doc),
    )


def _worker_lookup(config: AppConfig, doc: dict, item: dict, operator: dict | None) -> tuple[dict, list[dict]]:
    worker = GinesysClient(config, operator=operator) if operator else GinesysClient(config)
    priced = worker.lookup_item(doc, item)
    return priced, worker.calls


def _lookup_priced_items(client: GinesysClient, doc: dict, config: AppConfig, operator: dict | None = None) -> list[dict]:
    items = doc["items"]
    if len(items) <= 1 or config.lookup_workers <= 1:
        return [client.lookup_item(doc, item) for item in items]

    unique_items: dict[tuple, dict] = {}
    ordered_keys = []
    for item in items:
        key = _lookup_key(client, doc, item)
        if not key[2]:
            raise GinesysError(f"Exact-barcode lookup is mandatory; SKU {item.get('sku')} has no barcode.")
        ordered_keys.append(key)
        unique_items.setdefault(key, item)

    priced_by_key = {}
    max_workers = min(config.lookup_workers, len(unique_items))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_worker_lookup, config, doc, item, operator): key
            for key, item in unique_items.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            priced, calls = future.result()
            client.calls.extend(calls)
            priced_by_key[key] = priced
    return [dict(priced_by_key[key]) for key in ordered_keys]


def _post_single_document(doc: dict, config: AppConfig, operator: dict | None = None) -> tuple[dict, list[dict]]:
    client = GinesysClient(config, operator=operator) if operator else GinesysClient(config)
    if operator:
        doc["posted_by_username"] = operator.get("username")
        doc["posted_by_display"] = operator.get("display_name")
        doc["posted_by_ginesys_user_code"] = operator.get("ginesys_user_code")
        doc["posted_by_ginesys_username"] = operator.get("ginesys_username")

    current = get_posting(doc["document_key"])
    if current and current.get("status") == "SUCCESS":
        return _row_from_state(doc, current, "ALREADY_POSTED"), client.calls

    try:
        dc_code = current.get("dc_code") if current else None
        dc_number = current.get("dc_number") if current else None

        if not dc_code:
            remote = client.find_existing_document(doc, None)
            if remote:
                dc_code = remote.get("dc_code") or dc_code
                dc_number = remote.get("dc_number") or dc_number
                remote_status = str(remote.get("status") or "").strip().casefold()
                if _has_remote_transfer(remote):
                    save_posting(
                        doc["document_key"], status="SUCCESS", dc_code=dc_code, dc_number=dc_number,
                        transfer_code=remote.get("transfer_code"), transfer_number=remote.get("transfer_number"),
                        error="Recovered from Ginesys GetAdhocList; no duplicate transaction was created.",
                    )
                    return _row_from_state(doc, get_posting(doc["document_key"])), client.calls
                if remote_status in {"invoiced", "invoice created", "completed", "complete"}:
                    raise GinesysError(
                        f"GetAdhocList reports DC {dc_number or dc_code} as {remote.get('status')}, but returned no linked Transfer Out identifier. Manual recovery is required before retrying."
                    )
                save_posting(doc["document_key"], status="DC_CREATED", dc_code=dc_code, dc_number=dc_number, error="")

        if not dc_code:
            priced_items = _lookup_priced_items(client, doc, config, operator)
            for source, priced in zip(doc["items"], priced_items):
                source.update({
                    "ginesys_item_id": priced.get("itemId"), "mrp": priced.get("mrp"),
                    "basic_rate": priced.get("basicRate"), "discount_factor": priced.get("discountFactor"),
                    "discount": priced.get("discount"), "rate": priced.get("rateCal"), "wsp": priced.get("wsp"),
                })
            dc = client.post_dc(doc, priced_items, operator=operator)
            dc_code = dc.get("dc_code")
            dc_number = dc.get("dc_number")
            if not dc_code:
                raise GinesysError("DC/Save returned success but no DC code was present in the response.")
            save_posting(
                doc["document_key"], status="DC_CREATED", payload_hash=dc.get("payload_hash"),
                dc_code=dc_code, dc_number=dc_number, error="",
                created_by_user_id=operator.get("id") if operator else None,
                created_by_portal_username=operator.get("username") if operator else None,
                created_by_ginesys_username=operator.get("ginesys_username") if operator else None,
                created_by_ginesys_user_code=operator.get("ginesys_user_code") if operator else None,
                dc_created_by_user_id=operator.get("id") if operator else None,
                dc_created_by_portal_username=operator.get("username") if operator else None,
                dc_created_by_ginesys_username=operator.get("ginesys_username") if operator else None,
                dc_created_by_ginesys_user_code=operator.get("ginesys_user_code") if operator else None,
            )

        try:
            invoice = client.post_invoice(doc, int(dc_code), operator=operator)
        except GinesysError as inv_exc:
            if _is_duplicate_document_no_error(inv_exc):
                # The "Duplicate Document No." error PROVES the SI was already created in Ginesys
                # in a prior run. Try GetAdhocList first to retrieve the actual transfer details.
                transfer_code_recovered = None
                transfer_number_recovered = None
                remote = client.find_existing_document(doc, dc_code)
                if remote and _has_remote_transfer(remote):
                    transfer_code_recovered = remote.get("transfer_code")
                    transfer_number_recovered = remote.get("transfer_number")
                    recovery_note = "Transfer Out already existed; recovered transfer details from GetAdhocList."
                else:
                    # GetAdhocList did not return the linked SI — this is common when the DC
                    # list row does not include the SI reference. The duplicate error itself
                    # is conclusive proof that the SI exists, so mark it SUCCESS and flag
                    # for manual transfer-number lookup in Ginesys if needed.
                    recovery_note = (
                        f"Transfer Out for documentNo '{doc['reference']}' already existed in Ginesys "
                        "(confirmed by duplicate-documentNo rejection). Transfer number could not be "
                        "retrieved automatically — verify the Transfer Out number in Ginesys if required."
                    )
                save_posting(
                    doc["document_key"], status="SUCCESS",
                    dc_code=dc_code, dc_number=dc_number,
                    transfer_code=transfer_code_recovered,
                    transfer_number=transfer_number_recovered,
                    error=recovery_note,
                )
                return _row_from_state(doc, get_posting(doc["document_key"])), client.calls
            raise
        save_posting(
            doc["document_key"], status="SUCCESS", payload_hash=invoice.get("payload_hash"),
            dc_code=dc_code, dc_number=dc_number, transfer_code=invoice.get("transfer_code"),
            transfer_number=invoice.get("transfer_number"), error="",
            created_by_user_id=operator.get("id") if operator else None,
            created_by_portal_username=operator.get("username") if operator else None,
            created_by_ginesys_username=operator.get("ginesys_username") if operator else None,
            created_by_ginesys_user_code=operator.get("ginesys_user_code") if operator else None,
            transfer_created_by_user_id=operator.get("id") if operator else None,
            transfer_created_by_portal_username=operator.get("username") if operator else None,
            transfer_created_by_ginesys_username=operator.get("ginesys_username") if operator else None,
            transfer_created_by_ginesys_user_code=operator.get("ginesys_user_code") if operator else None,
        )
        return _row_from_state(doc, get_posting(doc["document_key"])), client.calls
    except Exception as exc:
        current = get_posting(doc["document_key"]) or {}
        if isinstance(exc, GinesysAuthenticationError):
            status = "AUTH_FAILED"
        else:
            last_endpoint = client.calls[-1].get("endpoint", "") if client.calls else ""
            if "/DC/GetAdhocList" in last_endpoint:
                status = "DC_CREATED" if current.get("dc_code") else "RECOVERY_CHECK_FAILED"
                save_posting(
                    doc["document_key"], status=status, dc_code=current.get("dc_code"),
                    dc_number=current.get("dc_number"), transfer_code=current.get("transfer_code"),
                    transfer_number=current.get("transfer_number"), error=str(exc),
                )
                return _row_from_state(doc, get_posting(doc["document_key"])), client.calls
            outcome_unknown = isinstance(exc, GinesysError) and (
                (exc.status_code is not None and exc.status_code >= 500) or "network error" in str(exc).casefold()
            )
            if outcome_unknown:
                status = "SI_OUTCOME_UNKNOWN" if current.get("dc_code") else "DC_OUTCOME_UNKNOWN"
            else:
                status = "INVOICE_FAILED" if current.get("dc_code") else "FAILED"
        save_posting(
            doc["document_key"], status=status, dc_code=current.get("dc_code"),
            dc_number=current.get("dc_number"), transfer_code=current.get("transfer_code"),
            transfer_number=current.get("transfer_number"), error=str(exc),
        )
        return _row_from_state(doc, get_posting(doc["document_key"])), client.calls


def _post_documents_locked(documents: list[dict], config: AppConfig, operator: dict | None = None) -> dict:
    """Post or safely resume documents using parallel workers."""
    all_calls = []
    results_by_key = {}
    
    max_workers = min(8, len(documents))
    if max_workers <= 1:
        for doc in documents:
            row, calls = _post_single_document(doc, config, operator)
            results_by_key[doc["document_key"]] = row
            all_calls.extend(calls)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_post_single_document, doc, config, operator): doc
                for doc in documents
            }
            for future in as_completed(futures):
                doc = futures[future]
                try:
                    row, calls = future.result()
                    results_by_key[doc["document_key"]] = row
                    all_calls.extend(calls)
                except Exception as exc:
                    results_by_key[doc["document_key"]] = _row_from_state(doc, {"status": "FAILED", "error": str(exc)})

    results = [results_by_key[doc["document_key"]] for doc in documents if doc["document_key"] in results_by_key]
    success = sum(1 for row in results if row["status"] in {"SUCCESS", "ALREADY_POSTED"})
    return {
        "results": results, "api_calls": all_calls,
        "summary": {"total": len(results), "success": success, "failed": len(results) - success},
    }


def post_documents(documents: list[dict], config: AppConfig, operator: dict | None = None) -> dict:
    # Prevent two browser clicks from racing past the same local/remote duplicate checks.
    with _POST_LOCK:
        return _post_documents_locked(documents, config, operator=operator)
