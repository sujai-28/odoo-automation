import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from config_loader import AppConfig
from ginesys_auth import GinesysTokenManager
from auth_store import set_ginesys_connection_error
from utils import canonical, clean_text, extract_error_message, normalize_barcode, payload_hash

_GLOBAL_ITEM_LOOKUP_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.Lock()


def _create_pooled_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


class GinesysError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None, unsupported_endpoint: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.unsupported_endpoint = unsupported_endpoint


class GinesysAuthenticationError(GinesysError):
    pass


@dataclass
class ApiCall:
    endpoint: str
    http_status: int | None
    success: bool
    message: str
    request_id: str | None
    duration_ms: int


def _iso_midnight(value: Any = None) -> str:
    text = clean_text(value)
    if text:
        try:
            return datetime.fromisoformat(text[:10]).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            pass
    return date.today().strftime("%Y-%m-%dT00:00:00")


def _ci_get(data: dict, *names: str, default=None):
    lookup = {str(k).casefold(): v for k, v in data.items()}
    for name in names:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


class GinesysClient:
    """Client for the Bearer-authenticated endpoints used by Ginesys Web itself."""

    def __init__(self, config: AppConfig, session: requests.Session | None = None, *, operator: dict | None = None,
                 token_provider=None):
        self.config = config
        self.session = session or _create_pooled_session()
        self.token_manager = None
        self._operator_user_id = int(operator["id"]) if operator and operator.get("id") else None
        self.ginesys_username = operator.get("ginesys_username") if operator else None
        self._renewable_auth = bool(token_provider or (operator and operator.get("id")))
        if token_provider:
            self._token_provider = token_provider
        elif operator and operator.get("id"):
            self.token_manager = GinesysTokenManager(config.base_url, int(operator["id"]), config.request_timeout)
            self._token_provider = self.token_manager.token
        elif config.api_token:
            # Retained only for isolated payload tests and migrations. The portal never
            # supplies this value; production tokens are always obtained per user.
            self._token_provider = lambda force=False: config.api_token
        else:
            raise ValueError("Ginesys credentials are not configured for the logged-in user.")
        self.session.headers.update({
            "Authorization": f"Bearer {self._token_provider(force=False)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.calls: list[dict] = []
        self._item_lookup_cache = _GLOBAL_ITEM_LOOKUP_CACHE

    def _request(self, method: str, endpoint: str, *, payload: Any = None, params: dict | None = None, read_only: bool = False) -> dict:
        url = self.config.base_url + endpoint
        max_attempts = 3 if read_only else 2
        last_error: Exception | None = None
        auth_refreshed = False
        for attempt in range(max_attempts):
            self.session.headers["Authorization"] = f"Bearer {self._token_provider(force=False)}"
            started = time.perf_counter()
            try:
                response = self.session.request(method, url, json=payload, params=params, timeout=self.config.request_timeout)
                duration = int((time.perf_counter() - started) * 1000)
                try:
                    body = response.json()
                except Exception:
                    body = {"message": response.text[:1000]}
                top_success = not isinstance(body, dict) or body.get("success") is not False
                nested = body.get("result") if isinstance(body, dict) else None
                nested_success = not isinstance(nested, dict) or nested.get("success") is not False
                success = response.status_code in (200, 201) and top_success and nested_success
                message = extract_error_message(body, response.reason)
                self.calls.append({
                    "endpoint": endpoint, "http_status": response.status_code, "success": success,
                    "message": message if not success else "OK", "request_id": body.get("requestId") if isinstance(body, dict) else None,
                    "duration_ms": duration, "ginesys_username": self.ginesys_username,
                })
                is_invalid_session = (
                    response.status_code == 401 or
                    (response.status_code == 500 and any(kw in str(body).lower() for kw in ("user session is invalid", "please re-login", "session has expired")))
                )
                if is_invalid_session:
                    if self._renewable_auth and not auth_refreshed:
                        auth_refreshed = True
                        self.session.headers["Authorization"] = f"Bearer {self._token_provider(force=True)}"
                        continue
                    if self._operator_user_id:
                        set_ginesys_connection_error(self._operator_user_id, "Ginesys rejected the renewed Bearer token (session expired or invalid).")
                    raise GinesysAuthenticationError(
                        "Ginesys Bearer session expired or was rejected. Automatic renewal did not produce an accepted session; test this user's saved Ginesys credentials.",
                        response.status_code, body,
                    )
                if response.status_code == 403:
                    raise GinesysAuthenticationError(
                        "The Bearer session is valid but this user is not authorized for the requested Ginesys operation (HTTP 403).",
                        403, body,
                    )
                if success:
                    if isinstance(nested, dict) and nested.get("confirmationReqd"):
                        raise GinesysError(
                            f"{endpoint} requires an interactive Ginesys confirmation and was not finalized: "
                            f"{nested.get('customMessage') or nested.get('otherInformation') or 'manual review required'}",
                            response.status_code, body,
                        )
                    return body
                if read_only and response.status_code in (408, 429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    time.sleep(1.2 * (2 ** attempt))
                    continue
                validations = nested.get("validationErrors") if isinstance(nested, dict) else None
                detail = f" Validation: {validations}" if validations else ""
                raise GinesysError(f"{endpoint} failed: {message}{detail}", response.status_code, body)
            except GinesysError:
                raise
            except requests.RequestException as exc:
                duration = int((time.perf_counter() - started) * 1000)
                self.calls.append({"endpoint": endpoint, "http_status": None, "success": False, "message": str(exc), "request_id": None, "duration_ms": duration, "ginesys_username": self.ginesys_username})
                last_error = GinesysError(f"Network error calling {endpoint}: {exc}")
                if read_only and attempt < max_attempts - 1:
                    time.sleep(1.2 * (2 ** attempt))
                    continue
                # Save calls are never automatically retried: recovery checks run first on the next manual retry.
                raise last_error
        raise last_error or GinesysError(f"Request failed: {endpoint}")

    @staticmethod
    def _result(body: Any):
        return body.get("result", body) if isinstance(body, dict) else body

    @staticmethod
    def _session_id() -> str:
        return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:5]}"

    def _posting_day(self, doc: dict) -> str:
        # Ginesys Web expects the active work/posting date here. The Odoo
        # document date remains preserved separately in the parsed document
        # and result workbook; it is not the Ginesys session work date.
        return self.config.work_date or date.today().isoformat()

    @staticmethod
    def document_marker(doc: dict) -> str:
        return f"ODOO-{doc['document_key'][:16].upper()} | {doc['reference']}"

    def validate_token(self) -> bool:
        self.get_adhoc_list(date.today().isoformat(), limit=1)
        return True

    def select_item_payload(self, doc: dict, item: dict) -> dict:
        site_tax_code = int(doc.get("site_tax_code") or self.config.owner_site_tax_code)
        field = self.config.select_item_field
        return {
            "criterias": [{
                "closeParenthesis": "", "code": 0, "condition": "AND", "displayFormat": "",
                "fieldId": -1, "fieldName": field, "fieldType": "STRING", "openParenthesis": "",
                "operatorId": 7, "operatorName": "", "systemFieldName": field,
                "value": normalize_barcode(item.get("barcode")),
            }],
            "stockSearchCriteria": None,
            "documentSearchCriteria": None,
            "formParam": {
                "selectionMode": "QTY", "rateAsDisplay": False, "callFrom": "DC", "showStock": True,
                "showSOPendingQty": False, "pcType": "P", "priceType": self.config.price_type,
                "priceListCode": self.config.price_list_code, "siteCode": doc["site_code"],
                "siteCodeOwner": self.config.owner_site_code, "siteTaxCode": site_tax_code,
                "siteTaxCodeOwner": self.config.owner_site_tax_code, "vatInclude": None,
                "transactionDate": _iso_midnight(self._posting_day(doc)), "discountFactor": self.config.factor,
                "discountMode": None, "discountBasis": None, "priceRoundoff": None, "entryMode": None,
                "companyCode": 0, "ouCode": self.config.owner_ou_code, "partyCode": None,
                "basicRateIn": None, "allowPORateChange": True, "allowNegativeQty": False,
                "allowZeroQty": False, "documentSiteCode": self.config.owner_site_code,
                "documentStockPoint": self.config.out_stock_point_code, "documentStockPointIsMandatory": True,
                "displayStock": False, "displaySOPendingQty": False,
                "batchAPIName": "Batch/GetItemBatchSerialForPositiveStock", "selectedBatchSerialList": [],
                "duplicateItemCheckRequired": True, "quantityRequired": True, "checkNegativeStock": True,
                "enableScanSet": True, "stockCode": self.config.out_stock_point_code, "saleType": "C",
                "dcCode": 0, "SetAPIName": "DC/GetSetItemsDetailsWithOpeningRate",
            },
            "stockPointSearchCriteria": None,
        }

    def lookup_item(self, doc: dict, item: dict) -> dict:
        barcode = normalize_barcode(item.get("barcode"))
        if not barcode:
            raise GinesysError(f"Exact-barcode lookup is mandatory; SKU {item.get('sku')} has no barcode.")
        cache_key = (
            int(doc["site_code"]), int(doc.get("site_tax_code") or self.config.owner_site_tax_code),
            barcode, self.config.price_type, self.config.price_list_code, self.config.factor,
            self._posting_day(doc),
        )
        with _CACHE_LOCK:
            if cache_key in self._item_lookup_cache:
                return dict(self._item_lookup_cache[cache_key])
        body = self._request("POST", "/WebAPI/api/Utility/SelectItem", payload=self.select_item_payload(doc, item), read_only=True)
        result = self._result(body)
        candidates = []
        seen = set()
        for row in _walk_dicts(result):
            row_barcode = normalize_barcode(_ci_get(row, "barcode", "barCode"))
            if row_barcode != barcode:
                continue
            item_id = clean_text(_ci_get(row, "itemId", "itemCode", "sku"))
            signature = (item_id, row_barcode, clean_text(_ci_get(row, "mrp")), clean_text(_ci_get(row, "basicRate")))
            if signature not in seen:
                seen.add(signature)
                candidates.append(row)
        if len(candidates) != 1:
            raise GinesysError(
                f"Utility/SelectItem returned {len(candidates)} exact matches for barcode {barcode}. Exactly one is required; no DC was created."
            )
        selected = candidates[0]
        selected_sku = clean_text(_ci_get(selected, "itemId", "itemCode", "sku"))
        basic = _as_float(_ci_get(selected, "basicRate", "mrp"), 0) or 0
        mrp = _as_float(_ci_get(selected, "mrp"), basic) or basic
        listed_mrp = _as_float(_ci_get(selected, "listedMrp"), mrp) or mrp
        discount_factor = _as_float(_ci_get(selected, "discountFactor"))
        discount = _as_float(_ci_get(selected, "discount"))
        rate = _as_float(_ci_get(selected, "rateCal", "rate"))
        if not selected_sku or basic <= 0 or mrp <= 0 or rate is None or discount is None or discount_factor is None:
            raise GinesysError(f"Incomplete pricing returned for barcode {barcode}: item={selected_sku}, MRP={mrp}, basicRate={basic}, rate={rate}")
        selected_item = {
            **selected,
            "itemId": selected_sku, "barcode": barcode, "basicRate": _money(basic), "mrp": _money(mrp),
            "listedMrp": _money(listed_mrp), "discountFactor": float(discount_factor or 0),
            "discount": _money(discount), "rateCal": _money(rate),
        }
        with _CACHE_LOCK:
            self._item_lookup_cache[cache_key] = dict(selected_item)
        return selected_item

    def get_packet_barcode(self) -> dict:
        """Obtain the challan identity that Ginesys Web allocates before DC/Save."""
        body = self._request(
            "GET", "/WebAPI/api/DC/GetPacketBarcode", payload={}, read_only=False
        )
        result = self._result(body) or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict):
            raise GinesysError("DC/GetPacketBarcode returned an invalid response; no DC was saved.", body=body)

        raw_code = _ci_get(result, "code", "dcCode", "challanCode")
        packet_barcode = clean_text(_ci_get(result, "label", "packetBarcode", "dcBarcode"))
        try:
            dc_code = int(raw_code)
        except (TypeError, ValueError):
            dc_code = 0
        if dc_code <= 0 or not packet_barcode:
            raise GinesysError(
                "DC/GetPacketBarcode did not return a non-zero challan code and packet barcode; no DC was saved.",
                body=body,
            )
        return {"dc_code": dc_code, "packet_barcode": packet_barcode, "raw": body}

    @staticmethod
    def _operator_user_code(operator: dict | None) -> int | None:
        if not operator:
            return None
        try:
            code = int(operator.get("ginesys_user_code") or 0)
        except (TypeError, ValueError):
            return None
        return code if code > 0 else None

    def dc_payload(self, doc: dict, priced_items: list[dict], allocation: dict, operator: dict | None = None) -> dict:
        if len(priced_items) != len(doc["items"]):
            raise ValueError("Priced-item count does not match source-item count.")
        dc_code = int(allocation.get("dc_code") or 0)
        packet_barcode = clean_text(allocation.get("packet_barcode"))
        if dc_code <= 0 or not packet_barcode:
            raise ValueError("A valid Ginesys challan code and packet barcode are required before DC/Save.")
        lines = []
        for index, (source, priced) in enumerate(zip(doc["items"], priced_items)):
            lines.append({
                "status": "N", "code": 0, "itemId": priced["itemId"],
                "negativeStockAlert": _ci_get(priced, "negativeStockAlert", default="I"),
                "barcode": normalize_barcode(source["barcode"]), "rate": priced["rateCal"], "mrp": priced["mrp"],
                "listedMrp": priced["listedMrp"], "wsp": _as_float(_ci_get(priced, "wsp")),
                "costRate": _as_float(_ci_get(priced, "costRate")), "quantity": float(source["qty"]),
                "remarks": "", "oldQty": 0, "orderCode": 0, "orderDetailCode": 0,
                "pickListDetailCode": 0, "binCode": 0, "salOrdMainOrdDate": None, "dataVersion": 0,
                "recordIndex": index, "discountFactor": priced["discountFactor"], "discount": priced["discount"],
                "basicRate": priced["basicRate"], "roundOff": None, "pendingQty": 0,
                "hasExcessQuantity": False, "batchSerialCode": None,
                "itemManagementMode": _ci_get(priced, "itemManagementMode", default="I"),
            })
        return {
            "code": dc_code, "entryMode": "Add", "docStatus": None, "status": "P", "dcType": 0,
            "ownerSiteCode": self.config.owner_site_code, "ownerSiteType": self.config.owner_site_type,
            "siteTaxCode": self.config.owner_site_tax_code, "ouCode": self.config.owner_ou_code,
            "transactionSiteCode": doc["site_code"], "dcDate": _iso_midnight(self._posting_day(doc)),
            "packetBarcode": packet_barcode, "partyCode": None, "schemeDocNo": "", "docCode": self.config.dc_doc_code,
            "transporterCode": doc.get("transporter_code"), "agentCode": doc.get("agent_code"),
            "outStockpointCode": self.config.out_stock_point_code, "priceListCode": self.config.price_list_code,
            "priceType": self.config.price_type, "tradeGrpCode": None, "termCode": None, "formCode": None,
            "discountFactor": self.config.factor, "cmpTaxCodeBasis": "S", "discountBasis": "N",
            "discountMode": "Mark Down", "includeVatInDiscount": "N", "saleType": "C",
            "remarks": self.document_marker(doc), "attachedFiles": {"timestamp": 0, "docType": "", "docCode": 0, "files": []},
            "items": lines, "deletedItems": [], "ignoreNegativeStockCheck": False,
            "ignoreCreditLimitCheck": False, "ignoreBatchExpiryCheck": False, "dataversion": 0,
            "allowPrint": True, "allowPrintPacket": True, "mdMenuShrtCode": "dcadhoc",
            "createdBy": self._operator_user_code(operator),
            "totalItemCount": len(lines),
            "udfDetails": [
                {"fieldName": "udfstring01", "value": self.document_marker(doc), "datatype": "string"},
                {"fieldName": "udfstring02", "value": None, "datatype": "editablelist"},
                {"fieldName": "udfstring03", "value": "", "datatype": "string"},
                {"fieldName": "udfstring04", "value": "", "datatype": "string"},
            ],
        }

    def post_dc(self, doc: dict, priced_items: list[dict] | None = None, operator: dict | None = None) -> dict:
        priced_items = priced_items or [self.lookup_item(doc, item) for item in doc["items"]]
        allocation = self.get_packet_barcode()
        payload = self.dc_payload(doc, priced_items, allocation, operator=operator)
        endpoint = f"/WebAPI/api/DC/Save?isFIFO=false&sessionId={self._session_id()}"
        body = self._request("POST", endpoint, payload=payload)
        result = self._result(body) or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        blocking = next((value for name in ("validationErrors", "invalidItems", "errorRecords", "invalidDetails") if (value := _ci_get(result, name))), None)
        if blocking:
            raise GinesysError(f"DC/Save returned blocking validation details: {blocking}", body=body)
        dc_code = _ci_get(result, "dcCode", "dccode", "challanCode", "code")
        if not dc_code:
            raise GinesysError("DC/Save returned success but no dcCode was present.", body=body)
        if int(dc_code) != allocation["dc_code"]:
            raise GinesysError(
                f"DC/Save returned dcCode {dc_code}, but Ginesys allocated {allocation['dc_code']}; manual review is required.",
                body=body,
            )
        return {
            "payload_hash": payload_hash(payload), "dc_code": int(dc_code),
            "dc_number": _ci_get(result, "schemeDocNo", "schemeDocNumber", "challanNo", "deliveryNo"),
            "dc_barcode": _ci_get(result, "dcBarcode") or allocation["packet_barcode"],
            "priced_items": priced_items, "raw": body,
        }

    def get_dc_details(self, doc: dict, dc_code: int) -> dict:
        params = {
            "formType": 1, "invDate": _iso_midnight(self._posting_day(doc)), "priceListCode": self.config.price_list_code,
            "siteCode": doc["site_code"], "ownerSiteCode": self.config.owner_site_code, "partyCode": "",
            "tradeGrpCode": self.config.trade_group_code, "formCode": 0,
        }
        body = self._request("POST", "/WebAPI/api/SI/GetDCDetails", payload=[int(dc_code)], params=params, read_only=True)
        result = self._result(body)
        if isinstance(result, list):
            details = result
        elif isinstance(result, dict) and result.get("itemDetails"):
            details = [result]
        else:
            details = _ci_get(result or {}, "data", "items", "records", default=[])
        if not isinstance(details, list):
            details = []
        matching = [x for x in details if clean_text(_ci_get(x, "challanCode", "dcCode")) == clean_text(dc_code)]
        if len(matching) != 1:
            raise GinesysError(f"SI/GetDCDetails returned {len(matching)} matching DC headers for dcCode {dc_code}; expected one.")
        detail = matching[0]
        if not detail.get("itemDetails") or not _ci_get(detail, "dcDataVersion"):
            raise GinesysError(f"SI/GetDCDetails returned incomplete details/data version for dcCode {dc_code}.")
        expected = {}
        for source in doc["items"]:
            key = normalize_barcode(source.get("barcode"))
            expected[key] = expected.get(key, 0) + float(source.get("qty") or 0)
        actual = {}
        for returned in detail["itemDetails"]:
            key = normalize_barcode(_ci_get(returned, "barcode"))
            actual[key] = actual.get(key, 0) + float(_ci_get(returned, "quantity", "invQty", default=0) or 0)
        if expected != actual:
            raise GinesysError(f"Created DC item reconciliation failed. Expected barcode/qty {expected}; Ginesys returned {actual}.")
        return detail

    def charge_payload(self, dc_detail: dict) -> dict:
        transaction_items = []
        for index, item in enumerate(dc_detail["itemDetails"]):
            qty = _as_float(_ci_get(item, "quantity", "invQty"), 0) or 0
            rate = _as_float(_ci_get(item, "rate", "rateCal"), 0) or 0
            transaction_items.append({
                "itemId": _ci_get(item, "itemId", "itemCode"), "barcode": _ci_get(item, "barcode"),
                "mrp": _as_float(_ci_get(item, "mrp")), "hsnsacCode": _ci_get(item, "hsnsacCode"),
                "rateCal": rate, "grsamt": _money(rate * qty), "quantity": qty, "recordIndex": index,
                "taxCode": _ci_get(item, "taxCode"), "itemCharges": [], "gstItcAppl": _ci_get(item, "gstItcAppl"),
                "glCode": 0, "slCode": 0, "referenceDetailCode": _ci_get(item, "dcDetCode"),
                "referenceMainCode": _ci_get(dc_detail, "challanCode"), "setIndex": 0, "setItemIndex": 0,
                "batchMfgDate": None, "batchSerialCode": None, "batchSerialNo": None, "batchValidTillDate": None,
            })
        return {"transactionItems": transaction_items, "applicableItems": [], "chargeData": [], "skipChargeCode": []}

    def calculate_item_charges(self, doc: dict, dc_detail: dict) -> dict:
        params = {
            "siteCode": self.config.owner_site_code, "inputGLCode": self.config.invoice_gl_code,
            "inputSLCode": "", "tranDate": _iso_midnight(self._posting_day(doc)), "termCode": 0,
            "tradeGroupCode": self.config.trade_group_code, "vendorTaxRegionCode": 0, "formCode": 0,
            "exchangeRate": 1, "isExciseApplicable": "false", "formType": 1, "orderTerm": "",
        }
        body = self._request("POST", "/WebAPI/api/SI/CalculateItemCharges", payload=self.charge_payload(dc_detail), params=params, read_only=True)
        result = self._result(body) or {}
        if not (_ci_get(result, "challanChargeDetials", "challanChargeDetails")):
            raise GinesysError("SI/CalculateItemCharges returned no challanChargeDetials.", body=body)
        return result

    @staticmethod
    def _charge_lines(charges: dict) -> dict[int, dict]:
        result = {}
        challans = _ci_get(charges, "challanChargeDetials", "challanChargeDetails", default=[])
        for challan in challans:
            for item in challan.get("itemDetails", []):
                code = _ci_get(item, "orderDetCode", "referenceDetailCode")
                if code is not None:
                    result[int(code)] = item
        return result

    @staticmethod
    def _invoice_charges(lines: list[dict]) -> list[dict]:
        """Aggregate per-item tax charges into SI/Save's header charge list."""
        aggregates: dict[str, dict] = {}
        for line in lines:
            for charge in line.get("itemCharges") or []:
                code = _ci_get(charge, "chargeCode")
                if code in (None, ""):
                    continue
                key = str(code)
                if key not in aggregates:
                    aggregates[key] = {
                        "invCode": 0, "seq": len(aggregates) + 1, "chargeCode": int(code),
                        "chargeAmt": 0.0, "rate": _as_float(_ci_get(charge, "rate"), 0) or 0,
                        "sign": _ci_get(charge, "sign", default="+"),
                        "gLCode": _ci_get(charge, "gLCode", "glCode"), "formCode": None,
                        "formNo": None, "formDt": None,
                        "withoutTermFormula": _ci_get(charge, "withoutTermFormula", default="N"),
                        "sLCode": _ci_get(charge, "sLCode", "slCode"),
                        "glCcAppl": _ci_get(charge, "glCcAppl", default="N"),
                        "basis": _ci_get(charge, "basis", default="P"),
                        "appAmount": 0.0,
                        "formulae": _ci_get(charge, "formulae", default="B"),
                        "operationLevel": _ci_get(charge, "operationLevel", default="L"),
                        "isTax": _ci_get(charge, "isTax", default="Y"),
                        "source": _ci_get(charge, "source", default="G"),
                        "forAmt": None,
                        "gSTComponent": _ci_get(charge, "gSTComponent", "gstComponent"),
                        "isReverse": _ci_get(charge, "isReverse") or "N",
                    }
                aggregates[key]["chargeAmt"] += _as_float(_ci_get(charge, "chargeAmount", "chargeAmt"), 0) or 0
                aggregates[key]["appAmount"] += _as_float(_ci_get(charge, "appAmount"), 0) or 0
        for charge in aggregates.values():
            charge["chargeAmt"] = _money(charge["chargeAmt"])
            charge["appAmount"] = _money(charge["appAmount"])
        return list(aggregates.values())

    @staticmethod
    def _normalize_item_charges(charges: list[dict] | None) -> list[dict]:
        """Map CalculateItemCharges response objects to SI/Save request fields."""
        normalized = []
        for charge in charges or []:
            normalized.append({
                "seq": _ci_get(charge, "seq"),
                "chargeCode": _ci_get(charge, "chargeCode"),
                "gLCode": _ci_get(charge, "gLCode", "glCode"),
                "sLCode": _ci_get(charge, "sLCode", "slCode"),
                "rate": _as_float(_ci_get(charge, "rate"), 0) or 0,
                "basis": _ci_get(charge, "basis"), "sign": _ci_get(charge, "sign"),
                "chargeAmount": _as_float(_ci_get(charge, "chargeAmount"), 0) or 0,
                "appAmount": _as_float(_ci_get(charge, "appAmount"), 0) or 0,
                "formulae": _ci_get(charge, "formulae"),
                "isTax": _ci_get(charge, "isTax"),
                "source": _ci_get(charge, "source"),
                "operationLevel": _ci_get(charge, "operationLevel"),
                "gSTComponent": _ci_get(charge, "gSTComponent", "gstComponent"),
                "roundoffAdjAmount": _ci_get(charge, "roundoffAdjAmount"),
                "invCode": _ci_get(charge, "invCode", default=0) or 0,
                "salInvDetCode": _ci_get(charge, "salInvDetCode", default=0) or 0,
                "iCode": _ci_get(charge, "iCode", "itemCode"),
                "itemBasicValue": _as_float(_ci_get(charge, "itemBasicValue"), 0) or 0,
                "chargeName": _ci_get(charge, "chargeName"),
                "originalRate": _as_float(_ci_get(charge, "originalRate", "rate"), 0) or 0,
                "isRoundOff": _ci_get(charge, "isRoundOff", default="N") or "N",
            })
        return normalized

    def invoice_payload(self, doc: dict, dc_detail: dict, charges: dict, operator: dict | None = None) -> dict:
        charge_lines = self._charge_lines(charges)
        lines = []
        for index, item in enumerate(dc_detail["itemDetails"]):
            detail_code = int(_ci_get(item, "dcDetCode"))
            calculated = charge_lines.get(detail_code, {})
            qty = _as_float(_ci_get(item, "quantity", "invQty"), 0) or 0
            rate = _as_float(_ci_get(item, "rate", "rateCal"), 0) or 0
            gross = _money(rate * qty)
            lines.append({
                "code": 0, "siInvCode": 0, "dcCode": int(_ci_get(dc_detail, "challanCode")),
                "outLocationCode": _ci_get(item, "dcInLocationCode", "outLocCode", default=5),
                "itemCode": _ci_get(item, "itemId", "itemCode"), "invQty": qty, "rtQty": 0,
                "mrp": _as_float(_ci_get(item, "mrp")), "remarks": "", "invAmount": gross,
                "rate": rate, "costRate": _as_float(_ci_get(item, "costRate")),
                "chgamt": _as_float(_ci_get(calculated, "chgamt")),
                "effAmt": _as_float(_ci_get(calculated, "effamt"), gross),
                "invDcDetCode": detail_code, "excisemainCode": 0, "exBasis": "", "exEffRate": 0,
                "exAbtfactor": 0, "exDutyfactor": 0, "exCessfactor": 0, "exAppAmt": 0,
                "exDutyAmt": 0, "exCessAmt": 0, "exRoundOff": 0,
                "taxAmt": _as_float(_ci_get(calculated, "taxAmt")),
                "factor": _as_float(_ci_get(item, "factor"), self.config.factor),
                "discount": _as_float(_ci_get(item, "discount")), "basicRate": _as_float(_ci_get(item, "basicRate")),
                "roundOff": _as_float(_ci_get(item, "roundOff")), "exApplicableFrom": None,
                "hsnSacCode": _ci_get(item, "hsnsacName", "hsnSacName", "hsnsacCode"),
                "gSTITCAppl": _ci_get(item, "gstItcAppl", default="IP"), "gLCODE": 0, "slCode": 0,
                "gLCCAppl": "N", "barcode": _ci_get(item, "barcode"),
                "negativeStockAlert": _ci_get(item, "negativeStockAlert", default="I"),
                "itemCharges": self._normalize_item_charges(_ci_get(calculated, "itemCharges", default=[])),
                "dcDataVersion": int(_ci_get(dc_detail, "dcDataVersion")), "dataVersion": 0,
                "dcIndex": 0, "batchSerialCode": None, "challanNo": _ci_get(dc_detail, "challanNo"),
            })
        return {
            "code": 0, "entryMode": "Add", "invoiceDate": _iso_midnight(self._posting_day(doc)), "invoiceNo": 0,
            "partyCode": None, "documentNo": doc["reference"], "documentDate": _iso_midnight(doc.get("date")),
            "dueDate": _iso_midnight(self._posting_day(doc)), "agentCode": None, "agRate": None,
            "gLCode": self.config.invoice_gl_code, "remarks": f"Auto generated - {self.document_marker(doc)}",
            "termCode": None, "schemeDocNo": "", "documentCode": "",
            "docSchemeCode": doc["invoice_doc_code"], "transporterCode": doc.get("transporter_code"),
            "saleType": "C", "sLCode": self.config.invoice_sl_code, "glCCAppl": "Y", "transitDays": self.config.transit_days,
            "destinationSiteOUCode": int(doc.get("site_ou_code") or 1), "ownerSiteCode": self.config.owner_site_code,
            "ownerSiteOUCode": self.config.owner_ou_code, "transitDueDate": None, "destinationSite": doc["site_code"],
            "tradeGroupCode": self.config.trade_group_code, "ownerGSTINNo": self.config.owner_gstin,
            "ownerGSTINStatus": "", "ownerTaxPayerType": "", "ownerGSTINVerifiedOn": None,
            "ownerGSTINStateCode": self.config.owner_state_code, "counterPartyGSTINNo": doc.get("site_gstin") or "",
            "counterPartyGSTINStateCode": doc.get("gst_state_code") or "", "gSTAppl": doc.get("gst_appl") or self.config.gst_appl, "lgtCode": None,
            "sICharges": self._invoice_charges(lines), "stockPointCode": 0, "stockSiteCode": 0, "deletedItems": [], "items": lines,
            "attachedFiles": {"timestamp": 0, "docType": "", "docCode": 0, "files": []},
            "dataVersion": 0, "allowPrint": True, "mdMenuShrtCode": "transferout", "releaseStatus": None,
            "createdBy": self._operator_user_code(operator), "ignoreCreditLimitCheck": False, "ignoreDocumentSchemeCheck": False,
            "authorizeBy": None, "dataversion": None, "logisticCode": None, "dataSendOn": None,
            "adjAmount": None, "saveAndRelease": 1 if self.config.release_enabled else 0,
            "partySLCode": None, "partyGLCode": self.config.party_gl_code, "intgCode": None, "crationTime": None,
            "priceType": self.config.price_type, "priceListCode": self.config.price_list_code,
            "discountFactor": self.config.factor, "priceRoundOff": None, "roundOffLimit": "N",
            "inclVATInDist": "N", "discountMode": "D", "discountBasis": "N",
            "ignoreBatchExpiryCheck": False,
            "udfDetails": [
                {"fieldName": "udfstring01", "value": "NA", "datatype": "string"},
                {"fieldName": "udfstring02", "value": "", "datatype": "string"},
                {"fieldName": "udfstring03", "value": None, "datatype": "editablelist"},
            ],
        }

    def post_invoice(self, doc: dict, dc_code: int, dc_intg: str | None = None, operator: dict | None = None) -> dict:
        dc_detail = self.get_dc_details(doc, dc_code)
        charges = self.calculate_item_charges(doc, dc_detail)
        payload = self.invoice_payload(doc, dc_detail, charges, operator=operator)
        endpoint = f"/WebAPI/api/SI/Save?isFIFO=false&sessionId={self._session_id()}"
        body = self._request("POST", endpoint, payload=payload)
        result = self._result(body) or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        blocking = next((value for name in ("validationErrors", "invalidDC", "errorRecords", "invalidDetails", "documentSchemeMismatch") if (value := _ci_get(result, name))), None)
        if blocking:
            raise GinesysError(f"SI/Save returned blocking validation details: {blocking}", body=body)
        si_code = _ci_get(result, "siCode", "invoiceCode", "code")
        number = _ci_get(result, "schemeDocNo", "schemeDocNumber", "invoiceNo", "siNo")
        if not si_code or not number:
            raise GinesysError("SI/Save returned success but no Transfer Out code/number was present.", body=body)
        return {
            "payload_hash": payload_hash(payload), "transfer_code": int(si_code),
            "transfer_number": number, "raw": body,
        }

    def get_adhoc_list(self, work_date: Any = None, *, page: int = 1, limit: int = 500) -> Any:
        payload = {
            "availableSites": self.config.available_site_codes, "connectedSite": str(self.config.owner_site_code),
            "connectedSiteOUCode": self.config.owner_ou_code, "workDate": _iso_midnight(work_date),
            "page": page, "start": (page - 1) * limit, "limit": limit,
            "sort": json.dumps([
                {"property": "deliveryDate", "direction": "DESC"},
                {"property": "deliveryNo", "direction": "DESC"},
            ], separators=(",", ":")),
        }
        body = self._request("POST", "/WebAPI/api/DC/GetAdhocList", payload=payload, read_only=True)
        return self._result(body)

    @staticmethod
    def _adhoc_records(result: Any) -> list[dict]:
        records = []
        seen = set()
        for row in _walk_dicts(result):
            code = _ci_get(row, "dcCode", "deliveryCode", "challanCode")
            number = _ci_get(row, "deliveryNo", "challanNo", "dcNo")
            if code is None and number is None:
                continue
            signature = (clean_text(code), clean_text(number))
            if signature not in seen:
                seen.add(signature)
                records.append(row)
        return records

    @staticmethod
    def _scalar_values(value: Any):
        if isinstance(value, dict):
            for child in value.values():
                yield from GinesysClient._scalar_values(child)
        elif isinstance(value, list):
            for child in value:
                yield from GinesysClient._scalar_values(child)
        elif isinstance(value, (str, int, float)):
            yield value

    @staticmethod
    def _normalize_adhoc(row: dict) -> dict:
        transfer_number = _ci_get(row, "invoiceNo", "invoiceSchemeDocNo", "transferOutNo", "salesInvoiceNo", "siSchemeDocNo")
        if not transfer_number:
            for key, value in row.items():
                if "invoice" in str(key).casefold() and isinstance(value, str) and "/" in value:
                    transfer_number = value
                    break
        return {
            "dc_code": _ci_get(row, "dcCode", "deliveryCode", "challanCode"),
            "dc_number": _ci_get(row, "deliveryNo", "challanNo", "dcNo", "schemeDocNo"),
            "site_code": _ci_get(row, "destinationSiteCode", "transactionSiteCode", "siteCode"),
            "status": clean_text(_ci_get(row, "status", "deliveryStatus", "docStatus")),
            "transfer_code": _ci_get(row, "invoiceCode", "siCode", "transferOutCode"),
            "transfer_number": transfer_number,
            "raw": row,
        }

    def find_existing_document(self, doc: dict, dc_code: int | None = None) -> dict | None:
        return self.find_existing_documents([doc], {doc["document_key"]: dc_code}).get(doc["document_key"])

    def find_existing_documents(self, documents: list[dict], dc_codes: dict[str, int | None] | None = None) -> dict[str, dict | None]:
        """Find DC/Transfer Out records for Odoo references with one paged list scan.

        Validation can contain many Odoo documents. Fetching GetAdhocList once per
        document is both slow and more likely to fail partway through validation,
        so all references for the active work date are matched in a single scan.
        """
        if not documents:
            return {}
        dc_codes = dc_codes or {}
        wanted = {
            doc["document_key"]: {
                "doc": doc,
                "marker": canonical(self.document_marker(doc)),
                "reference": canonical(doc.get("reference")),
                "dc_code": clean_text(dc_codes.get(doc["document_key"])),
            }
            for doc in documents
        }
        matches: dict[str, list[dict]] = {key: [] for key in wanted}
        page_size = 500
        for page in range(1, 101):
            result = self.get_adhoc_list(self._posting_day(documents[0]), page=page, limit=page_size)
            page_records = self._adhoc_records(result)
            for row in page_records:
                normalized = self._normalize_adhoc(row)
                scalar_values = [canonical(v) for v in self._scalar_values(row)]
                for key, target in wanted.items():
                    doc = target["doc"]
                    if target["dc_code"] and clean_text(normalized["dc_code"]) == target["dc_code"]:
                        matches[key].append(normalized)
                        continue
                    if normalized["site_code"] not in (None, "") and clean_text(normalized["site_code"]) != clean_text(doc["site_code"]):
                        continue
                    # Portal-created documents carry the collision-resistant marker.
                    # Documents created outside this portal can still be recovered by
                    # an exact Odoo invoice/reference value returned by Ginesys.
                    marker_found = target["marker"] and any(target["marker"] in value for value in scalar_values)
                    reference_found = target["reference"] and target["reference"] in scalar_values
                    if marker_found or reference_found:
                        matches[key].append(normalized)
            if len(page_records) < page_size:
                break
        found: dict[str, dict | None] = {}
        for key, rows in matches.items():
            unique = {clean_text(x["dc_code"]): x for x in rows if x.get("dc_code")}
            if len(unique) > 1:
                numbers = ", ".join(clean_text(x.get("dc_number") or x.get("dc_code")) for x in unique.values())
                raise GinesysError(
                    f"Duplicate check found multiple Ginesys DCs for Odoo reference {wanted[key]['doc']['reference']}: {numbers}. Manual review is required."
                )
            found[key] = next(iter(unique.values()), None)
        return found

    # Public-API methods are deliberately disabled so an expired/incorrect public token
    # cannot accidentally be used after this Bearer WebAPI upgrade.
    def post_combined(self, doc: dict) -> dict:
        raise GinesysError("Combined Public API mode was removed. This build uses the captured Bearer WebAPI DC + Transfer Out flow only.")
