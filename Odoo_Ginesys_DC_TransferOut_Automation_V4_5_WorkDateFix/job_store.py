from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "portal-data" / "jobs"
RESULTS = ROOT / "portal-data" / "results"
UPLOADS = ROOT / "portal-data" / "uploads"
for d in (JOBS, RESULTS, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)


def save_job(job_id: str, data: dict):
    (JOBS / f"{job_id}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_job(job_id: str) -> dict:
    path = JOBS / f"{job_id}.json"
    if not path.exists():
        raise FileNotFoundError("Job not found or expired.")
    return json.loads(path.read_text(encoding="utf-8"))
