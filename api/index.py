"""
api/index.py  –  Vercel serverless entry point for the Odoo Automation Flask app.

Vercel invokes this module and expects an WSGI callable named `app`.
We patch a few things that don't work in the serverless / read-only environment:

  • SQLite history DB  → stored in /tmp (ephemeral, wiped after each cold start)
  • portal.db (auth)  → stored in /tmp  (operators must be re-added on each cold start
                         OR pre-seeded via the GINESYS_OPERATOR_* env vars – see auth_store)
  • job_store dirs    → /tmp/portal-data/…   (jobs are in-memory within one request)
  • File writes       → /tmp only; the /var tree is read-only on Vercel

Everything that is READ-ONLY works perfectly:
  • Ginesys_Config.xlsx / site_master.xlsx   (bundled in the repo)
  • Ginesys live API calls                   (outbound HTTPS is allowed)
  • File upload → parse → validate → post    (all in-memory within one request)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── 1. Re-point writable paths to /tmp ──────────────────────────────────────
TMP = Path("/tmp")

# Override the directories that job_store / auth_store / app.py write to
os.environ.setdefault("PORTAL_DATA_DIR", str(TMP / "portal-data"))
os.environ.setdefault("PORTAL_DB_PATH",  str(TMP / "portal.db"))
os.environ.setdefault("HISTORY_DB_PATH", str(TMP / "history.db"))

# Create /tmp subdirs so imports that mkdir at module level don't fail
for _d in ["portal-data/jobs", "portal-data/results", "portal-data/uploads"]:
    (TMP / _d).mkdir(parents=True, exist_ok=True)

# ── 2. Add the project root and Ginesys sub-package to sys.path ─────────────
HERE = Path(__file__).resolve().parent        # api/
ROOT = HERE.parent                             # project root  (d:\odoo automation)
GINESYS = ROOT / "Odoo_Ginesys_DC_TransferOut_Automation_V4_5_WorkDateFix"

for p in [str(ROOT), str(GINESYS)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── 3. Patch job_store to use /tmp ───────────────────────────────────────────
import job_store as _js
_js.JOBS    = TMP / "portal-data" / "jobs"
_js.RESULTS = TMP / "portal-data" / "results"
_js.UPLOADS = TMP / "portal-data" / "uploads"

# ── 4. Patch auth_store DB path to /tmp ─────────────────────────────────────
import shutil
import auth_store as _as
_as.DB_PATH = TMP / "portal.db"
_as.SESSION_SECRET_PATH = TMP / "portal-data" / "session_secret.key"

# ── 4b. Seed portal.db from bundled copy on cold start ──────────────────────
# The repo ships a portal.db with pre-configured operators.  On Vercel the
# filesystem under /var is read-only but we can copy the file to /tmp so that
# operators are available immediately without manual re-creation.
_SEED_DB = GINESYS / "portal.db"
if _SEED_DB.exists() and not (TMP / "portal.db").exists():
    shutil.copy2(_SEED_DB, TMP / "portal.db")

# ── 5. Import the main Flask application ────────────────────────────────────
# app.py at the project root is the real application
import importlib.util, types

_spec = importlib.util.spec_from_file_location("_odoo_app", ROOT / "app.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)   # type: ignore[union-attr]

# Vercel expects a WSGI callable named `app`
app = _mod.app  # type: ignore[attr-defined]

# ── 6. Initialise the SQLite DBs on first cold start ────────────────────────
with app.app_context():
    try:
        _mod.init_db()
    except Exception:
        pass
