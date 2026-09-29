from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def canonical(value: Any) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ",", text)
    return text.strip()


def normalize_barcode(value: Any) -> str:
    text = clean_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    return text


def as_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def display_number(value: float) -> int | float:
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 6)


def iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        import pandas as pd
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()
    except Exception:
        return None


def deterministic_id(prefix: str, *parts: Any, max_len: int = 48) -> str:
    raw = "|".join(clean_text(x) for x in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16].upper()
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "", prefix)[:12] or "INTG"
    result = f"{safe_prefix}-{digest}"
    return result[:max_len]


def document_key(reference: str, site_code: int, doc_date: str, source_type: str) -> str:
    raw = f"{source_type}|{reference}|{site_code}|{doc_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "file"


def extract_error_message(body: Any, fallback: str = "Unknown API error") -> str:
    if isinstance(body, dict):
        for field in ("exceptionMessage", "errorMessage", "detail", "error_description"):
            val = clean_text(body.get(field))
            if val:
                val = re.sub(r"^\d+@GINI@", "", val).strip()
                if val:
                    return val
        error = body.get("error")
        if isinstance(error, dict):
            message = clean_text(error.get("message"))
            inner = error.get("innerError")
            if not message and isinstance(inner, dict):
                message = clean_text(inner.get("code"))
            if message:
                return message
        message = clean_text(body.get("message"))
        if message and message != "An error has occurred.":
            return message
    text = clean_text(body)
    return text[:800] if text else fallback

