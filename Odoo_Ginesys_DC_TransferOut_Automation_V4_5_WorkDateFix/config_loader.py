from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openpyxl import load_workbook

from utils import canonical, clean_text

_log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
MASTER_DIR = ROOT / "Master"
CONFIG_FILE = MASTER_DIR / "Ginesys_Config.xlsx"
SITE_DETAILS_FILE = MASTER_DIR / "site_master.xlsx"
STATE_OVERRIDE_FILE = MASTER_DIR / "site_state_overrides.json"
WEBAPI_OVERRIDE_FILE = MASTER_DIR / "site_webapi_overrides.json"
ENV_FILE = ROOT / ".env"

GST_STATE_CODES = {
    "JK": "01", "HP": "02", "PB": "03", "CH": "04", "UK": "05", "HR": "06", "DL": "07",
    "RJ": "08", "UP": "09", "BR": "10", "SK": "11", "AR": "12", "NL": "13", "MN": "14",
    "MZ": "15", "TR": "16", "ML": "17", "AS": "18", "WB": "19", "JH": "20", "OD": "21",
    "CG": "22", "MP": "23", "GJ": "24", "DD": "26", "MH": "27", "KA": "29", "GA": "30",
    "LD": "31", "KL": "32", "TN": "33", "PY": "34", "AN": "35", "TS": "36", "AP": "37", "LA": "38",
}

# Exact site-access list captured from the working Ginesys Web GetAdhocList request.
# It remains overrideable through GINESYS_AVAILABLE_SITE_CODES when user access changes.
# Updated 2026-09-29: added new site codes from site_master.xlsx (981-985, 1018-1022, 1055-1067).
CAPTURED_AVAILABLE_SITE_CODES = [
    228, 405, 738, 710, 433, 486, 429, 927, 883, 886, 771, 941, 767, 892, 933,
    776, 980, 889, 765, 935, 741, 780, 782, 777, 735, 779, 884, 885, 781, 757,
    770, 756, 758, 891, 977, 817, 936, 932, 769, 816, 775, 768, 928, 753, 815,
    978, 850, 762, 942, 763, 764, 773, 887, 739, 744, 934, 778, 937, 774, 894,
    890, 979, 929, 743, 893, 976, 888, 766, 939, 931, 930,
    # New sites added from site_master.xlsx update:
    981, 982, 983, 984, 985,
    1018, 1019, 1020, 1021, 1022,
    1055, 1056, 1057, 1058, 1059, 1060, 1061, 1062, 1063, 1064, 1065, 1067,
]

# ---------------------------------------------------------------------------
# Google Sheets SITE_MASTER cache
# ---------------------------------------------------------------------------
# Set GINESYS_SITE_MASTER_GSHEET_URL in your .env to the Google Sheet URL.
# The sheet must be shared as "Anyone with the link can view".
# The CSV export is cached in-memory for GSHEET_CACHE_TTL_SECONDS.
_GSHEET_CACHE: tuple[float, list[list[str]]] | None = None  # (timestamp, rows)
GSHEET_CACHE_TTL_SECONDS = 300  # 5 minutes


def _reload_env() -> None:
    if ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE, override=True)


_reload_env()


@dataclass
class Site:
    odoo_name: str
    code: int
    ginesys_name: str
    agent_code: int | None
    transporter_code: int | None
    active: bool
    state_code: str | None
    gst_state_code: str | None
    gstin: str
    site_tax_code: int
    ou_code: int
    doc_scheme_code: int | None
    gst_appl: str


@dataclass
class AppConfig:
    base_url: str
    owner_site_code: int
    doc_code_tn: int
    doc_code_interstate: int
    dc_doc_code: int
    out_stock_point_code: int
    invoice_gl_code: int | None
    invoice_sl_code: int | None
    sales_term_code: int | None
    agent_code: int | None
    transporter_code: int | None
    transit_days: int
    release_enabled: bool
    factor: float
    api_mode: str
    api_token: str
    request_timeout: int
    sites: dict[str, Site]
    warnings: list[str]
    owner_site_type: str
    owner_site_tax_code: int
    owner_state_code: str
    owner_gstin: str
    owner_ou_code: int
    price_list_code: int
    price_type: str
    trade_group_code: int
    select_item_field: str
    available_site_codes: list[int]
    work_date: str
    gst_appl: str
    party_gl_code: int
    lookup_workers: int

    def get_site(self, name: str) -> Site | None:
        return self.sites.get(canonical(name))

    def transfer_doc_code_for(self, site: Site) -> int:
        if site.doc_scheme_code is not None:
            return site.doc_scheme_code
        return self.doc_code_tn if site.gst_state_code == self.owner_state_code else self.doc_code_interstate


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _env_int(name: str, fallback: int | None) -> int | None:
    value = clean_text(os.getenv(name))
    if not value:
        return fallback
    parsed = _to_int(value)
    if parsed is None:
        raise ValueError(f"{name} must be numeric.")
    return parsed


def _env_float(name: str, fallback: float) -> float:
    value = clean_text(os.getenv(name))
    if not value:
        return fallback
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric.") from exc


def _json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return data


def _load_state_overrides() -> dict[str, str]:
    data = _json_object(STATE_OVERRIDE_FILE)
    return {canonical(k): clean_text(v).upper() for k, v in data.items() if clean_text(k) and clean_text(v)}


def _load_webapi_overrides() -> dict[str, dict]:
    data = _json_object(WEBAPI_OVERRIDE_FILE)
    return {canonical(k): v for k, v in data.items() if isinstance(v, dict)}


def _gsheet_csv_url(raw_url: str) -> str:
    """Convert any Google Sheets share/edit URL to its CSV export URL."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw_url)
    if not m:
        return raw_url
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=(\d+)", raw_url)
    gid = gid_m.group(1) if gid_m else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _load_gsheet_site_rows() -> list[list[str]] | None:
    """Fetch SITE_MASTER rows from Google Sheets (cached 5 min). Returns None on failure."""
    global _GSHEET_CACHE
    gsheet_url = clean_text(os.getenv("GINESYS_SITE_MASTER_GSHEET_URL"))
    if not gsheet_url:
        return None
    now = time.monotonic()
    if _GSHEET_CACHE is not None:
        cached_at, cached_rows = _GSHEET_CACHE
        if now - cached_at < GSHEET_CACHE_TTL_SECONDS:
            return cached_rows
    try:
        csv_url = _gsheet_csv_url(gsheet_url)
        req = urllib.request.Request(csv_url, headers={"User-Agent": "odoo-ginesys-automation/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader]
        _GSHEET_CACHE = (now, rows)
        _log.info("SITE_MASTER loaded from Google Sheets (%d rows incl. header)", len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to load SITE_MASTER from Google Sheets: %s — falling back to local Excel", exc)
        return None


def _load_site_details() -> tuple[dict[str, dict], dict[int, dict]]:
    """Load optional GSTIN/site metadata exported separately by Ginesys."""
    if not SITE_DETAILS_FILE.exists():
        return {}, {}
    wb = load_workbook(SITE_DETAILS_FILE, read_only=True, data_only=True)
    try:
        ws = wb["#Site#_EBO"] if "#Site#_EBO" in wb.sheetnames else wb[wb.sheetnames[0]]
        headers = [clean_text(c.value) for c in ws[1]]
        index = {canonical(header): i for i, header in enumerate(headers)}
        code_idx = index.get(canonical("Code"))
        odoo_idx = index.get(canonical("odoo_name"))
        name_idx = index.get(canonical("Name"))
        gstin_idx = index.get(canonical("GSTIN"))
        by_name: dict[str, dict] = {}
        by_code: dict[int, dict] = {}
        for cells in ws.iter_rows(min_row=2, values_only=True):
            code = _to_int(cells[code_idx]) if code_idx is not None else None
            odoo_name = clean_text(cells[odoo_idx]) if odoo_idx is not None else ""
            site_name = clean_text(cells[name_idx]) if name_idx is not None else ""
            gstin = clean_text(cells[gstin_idx]).upper() if gstin_idx is not None else ""
            detail = {"code": code, "odoo_name": odoo_name, "name": site_name, "gstin": gstin}
            if odoo_name:
                by_name[canonical(odoo_name)] = detail
            if code is not None and code not in by_code:
                by_code[code] = detail
        return by_name, by_code
    finally:
        wb.close()


def _infer_state(name: str, overrides: dict[str, str]) -> str | None:
    key = canonical(name)
    if key in overrides:
        return overrides[key]
    match = re.search(r"\(([A-Za-z]{2})\)", name)
    return match.group(1).upper() if match else None


def _csv_ints(name: str) -> list[int]:
    values = []
    for raw in clean_text(os.getenv(name)).split(","):
        if not raw.strip():
            continue
        value = _to_int(raw.strip())
        if value is None:
            raise ValueError(f"{name} must be a comma-separated list of numeric site codes.")
        values.append(value)
    return values


def load_config() -> AppConfig:
    _reload_env()
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Missing configuration workbook: {CONFIG_FILE}")

    wb = load_workbook(CONFIG_FILE, read_only=True, data_only=True)
    try:
        if "API_CONFIG" not in wb.sheetnames or "SITE_MASTER" not in wb.sheetnames:
            raise ValueError("Ginesys_Config.xlsx must contain API_CONFIG and SITE_MASTER sheets.")
        ws = wb["API_CONFIG"]
        headers = [clean_text(c.value) for c in ws[1]]
        values = [c.value for c in ws[2]]
        row = dict(zip(headers, values))

        base_url = (clean_text(os.getenv("GINESYS_BASE_URL")) or clean_text(row.get("Base URL"))).rstrip("/")
        base_url = re.sub(r"/(?:erp/gds/api(?:/.*)?|webapi/api(?:/.*)?)$", "", base_url, flags=re.IGNORECASE).rstrip("/")
        owner = _env_int("GINESYS_OWNER_SITE_CODE", _to_int(row.get("Owner Site Code")))
        dc_doc = _env_int("GINESYS_DC_DOC_CODE", _to_int(row.get("DC Doc Code")))
        stock_point = _env_int("GINESYS_OUT_STOCK_POINT_CODE", _to_int(row.get("Out Stock Point Code")))
        invoice_gl = _env_int("GINESYS_INVOICE_GL_CODE", _to_int(row.get("Invoice GL Code")))

        # All transfers in this deployment are GST outward supplies. The captured
        # GST Transfer Out numbering scheme is 215, used by default for both
        # same-state and interstate unless a site override is configured.
        same_state_scheme = _env_int("GINESYS_DOC_SCHEME_SAME_STATE", 215)
        interstate_scheme = _env_int("GINESYS_DOC_SCHEME_INTERSTATE", 215)
        discount_factor = _env_float("GINESYS_DISCOUNT_FACTOR", 60.0)
        warnings: list[str] = [
            "All transfers are GST outward supply; default Transfer Out scheme is 215 for same-state and interstate."
        ]
        required = {
            "Base URL": base_url, "Owner Site Code": owner, "Same-state Transfer Out Scheme": same_state_scheme,
            "Interstate Transfer Out Scheme": interstate_scheme, "DC Doc Code": dc_doc,
            "Out Stock Point Code": stock_point, "Invoice GL Code": invoice_gl,
        }
        missing = [k for k, v in required.items() if v in (None, "")]
        if missing:
            raise ValueError("Missing required Ginesys configuration: " + ", ".join(missing))
        if same_state_scheme == interstate_scheme:
            warnings.append(
                f"Same-state and interstate Transfer Out both use scheme {same_state_scheme}. This is valid when the same Ginesys GST numbering scheme is approved for all transfers."
            )

        owner_state = clean_text(os.getenv("GINESYS_OWNER_STATE_CODE")) or "33"
        if owner_state.isdigit():
            owner_state = owner_state.zfill(2)
        else:
            owner_state = GST_STATE_CODES.get(owner_state.upper(), owner_state)
        default_site_tax = _env_int("GINESYS_SITE_TAX_CODE_DEFAULT", 4) or 4
        default_site_ou = _env_int("GINESYS_SITE_OU_CODE_DEFAULT", 1) or 1
        default_counterparty_gstin = clean_text(os.getenv("GINESYS_COUNTERPARTY_GSTIN_DEFAULT"))
        # Every stock transfer in this deployment is a GST outward supply.
        # Keep this configurable for diagnostics, but check_config deliberately
        # blocks any value other than Y before validation or live posting.
        default_gst_appl = clean_text(os.getenv("GINESYS_GST_APPL")) or "Y"
        state_overrides = _load_state_overrides()
        webapi_overrides = _load_webapi_overrides()
        site_details_by_name, site_details_by_code = _load_site_details()
        sites: dict[str, Site] = {}
        missing_gstin_sites: list[str] = []

        # Prefer Google Sheets; fall back to local SITE_MASTER sheet in the Excel
        gsheet_rows = _load_gsheet_site_rows()
        if gsheet_rows and len(gsheet_rows) >= 2:
            site_headers = [clean_text(v) for v in gsheet_rows[0]]
            index = {h: i for i, h in enumerate(site_headers)}
            raw_site_rows: list[Any] = gsheet_rows[1:]
            _source = "Google Sheets"
        else:
            sws = wb["SITE_MASTER"]
            site_headers = [clean_text(c.value) for c in sws[1]]
            index = {h: i for i, h in enumerate(site_headers)}
            raw_site_rows = list(sws.iter_rows(min_row=2, values_only=True))
            _source = "local Excel"
        _log.debug("Parsing SITE_MASTER from %s (%d data rows)", _source, len(raw_site_rows))

        for cells in raw_site_rows:
            def site_value(*headers: str):
                for header in headers:
                    column = index.get(header)
                    if column is not None and column < len(cells) and clean_text(cells[column]):
                        return cells[column]
                return None

            name = clean_text(cells[index.get("Store / Odoo Name", 0)])
            code = _to_int(cells[index.get("Ginesys Destination Site Code", 1)])
            if not name or code is None:
                continue
            active_idx = index.get("Active")
            if active_idx is not None and not _to_bool(cells[active_idx], True):
                continue
            gname_idx = index.get("Ginesys Destination Site Name")
            gname = clean_text(cells[gname_idx]) if gname_idx is not None else ""
            override = webapi_overrides.get(canonical(name)) or webapi_overrides.get(canonical(gname)) or {}
            site_detail = site_details_by_name.get(canonical(name)) or site_details_by_code.get(code) or {}
            state = clean_text(override.get("state")) or clean_text(site_value("State Code", "State")) or _infer_state(name, state_overrides)
            state = state.upper() if state else None
            gstin = clean_text(override.get("gstin")) or clean_text(site_value("GSTIN", "Destination GSTIN", "GST")) or clean_text(site_detail.get("gstin")) or default_counterparty_gstin
            gst_state = clean_text(override.get("gst_state_code")) or clean_text(site_value("GST State Code"))
            if gst_state.isdigit():
                gst_state = gst_state.zfill(2)
            elif state:
                gst_state = GST_STATE_CODES.get(state, "")
            if not gst_state and len(gstin) >= 2 and gstin[:2].isdigit():
                gst_state = gstin[:2]
            if not gst_state:
                warnings.append(f"GST state is not explicit for '{name}'; interstate scheme will be selected and Transfer Out may be blocked by Ginesys.")
            if not gstin and gst_state == owner_state:
                gstin = clean_text(os.getenv("GINESYS_OWNER_GSTIN")) or "33AAFCT5162N1Z1"
            if not gstin:
                missing_gstin_sites.append(name)
            agent_idx = index.get("Default Agent Code")
            transp_idx = index.get("Default Transporter Code")
            sites[canonical(name)] = Site(
                odoo_name=name, code=code, ginesys_name=gname,
                agent_code=_to_int(cells[agent_idx]) if agent_idx is not None else None,
                transporter_code=_to_int(cells[transp_idx]) if transp_idx is not None else None,
                active=True, state_code=state, gst_state_code=gst_state or None, gstin=gstin,
                site_tax_code=_to_int(override.get("site_tax_code")) or default_site_tax,
                ou_code=_to_int(override.get("ou_code")) or default_site_ou,
                doc_scheme_code=_to_int(override.get("doc_scheme_code")) or _to_int(site_value("Document Scheme Code", "Doc Scheme Code")),
                gst_appl=clean_text(override.get("gst_appl")) or clean_text(site_value("GST Applicable")) or default_gst_appl,
            )
        if missing_gstin_sites:
            sample = ", ".join(missing_gstin_sites[:8])
            more = f" and {len(missing_gstin_sites) - 8} more" if len(missing_gstin_sites) > 8 else ""
            warnings.append(
                f"Counterparty GSTIN is blank for {len(missing_gstin_sites)} site(s): {sample}{more}. GST posting for a selected site is blocked until its GSTIN is added to SITE_MASTER or Master/site_webapi_overrides.json."
            )
    finally:
        wb.close()

    if not sites:
        raise ValueError("SITE_MASTER contains no active site mappings.")
    available = _csv_ints("GINESYS_AVAILABLE_SITE_CODES")
    if not available:
        available = list(CAPTURED_AVAILABLE_SITE_CODES)

    return AppConfig(
        base_url=base_url, owner_site_code=int(owner), doc_code_tn=int(same_state_scheme),
        doc_code_interstate=int(interstate_scheme), dc_doc_code=int(dc_doc),
        out_stock_point_code=int(stock_point), invoice_gl_code=invoice_gl,
        invoice_sl_code=_to_int(row.get("Invoice SL Code")), sales_term_code=_to_int(row.get("Sales Term Code")),
        agent_code=_to_int(row.get("Agent Code")), transporter_code=_to_int(row.get("Transporter Code")),
        transit_days=_to_int(row.get("Transit Days")) or 0, release_enabled=_to_bool(row.get("Release Enabled"), True),
        factor=discount_factor, api_mode="per_user_password", api_token="",
        request_timeout=_to_int(os.getenv("REQUEST_TIMEOUT_SECONDS")) or 60,
        sites=sites, warnings=warnings,
        owner_site_type=clean_text(os.getenv("GINESYS_OWNER_SITE_TYPE")) or "OS-OO-CM",
        owner_site_tax_code=_env_int("GINESYS_OWNER_SITE_TAX_CODE", 4) or 4,
        owner_state_code=owner_state,
        owner_gstin=clean_text(os.getenv("GINESYS_OWNER_GSTIN")) or "33AAFCT5162N1Z1",
        owner_ou_code=_env_int("GINESYS_OWNER_OU_CODE", 1) or 1,
        price_list_code=_env_int("GINESYS_PRICE_LIST_CODE", 12) or 12,
        price_type=clean_text(os.getenv("GINESYS_PRICE_TYPE")) or "M",
        # Captured successful GST Transfer Out request uses trade group 2.
        trade_group_code=_env_int("GINESYS_TRADE_GROUP_CODE", 2) or 2,
        select_item_field=clean_text(os.getenv("GINESYS_SELECT_ITEM_FIELD")) or "Barcode",
        available_site_codes=available,
        work_date=clean_text(os.getenv("GINESYS_WORK_DATE")),
        gst_appl=default_gst_appl,
        party_gl_code=_env_int("GINESYS_PARTY_GL_CODE", 6) or 6,
        lookup_workers=max(1, _env_int("GINESYS_LOOKUP_WORKERS", 6) or 6),
    )


def check_config(config: AppConfig) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings = list(config.warnings)
    if not config.base_url.startswith("https://"):
        warnings.append("GINESYS_BASE_URL is not HTTPS.")
    if config.factor < 0 or config.factor > 100:
        errors.append("GINESYS_DISCOUNT_FACTOR must be between 0 and 100.")
    if config.gst_appl.upper() != "Y":
        errors.append("GINESYS_GST_APPL must be Y because all transfers are GST outward supplies.")
    if not config.available_site_codes:
        errors.append("No available Ginesys site codes are configured.")
    if config.lookup_workers < 1 or config.lookup_workers > 16:
        errors.append("GINESYS_LOOKUP_WORKERS must be between 1 and 16.")
    if config.work_date:
        try:
            from datetime import date
            date.fromisoformat(config.work_date)
        except ValueError:
            errors.append("GINESYS_WORK_DATE must be blank or YYYY-MM-DD.")
    missing_state = [s.odoo_name for s in config.sites.values() if not s.gst_state_code]
    if missing_state:
        errors.append("GST state metadata is missing for active site(s): " + ", ".join(missing_state[:8]))
    invalid_gst_appl = [s.odoo_name for s in config.sites.values() if s.gst_appl.upper() != "Y"]
    if invalid_gst_appl:
        errors.append("gst_appl must be Y for every site because all transfers are GST outward supplies: " + ", ".join(invalid_gst_appl[:8]))
    return errors, warnings
