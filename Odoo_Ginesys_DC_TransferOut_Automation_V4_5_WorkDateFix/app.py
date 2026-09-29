from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from auth_store import create_user, get_session_secret, get_user, get_user_by_ginesys_username, list_users, public_user, save_ginesys_login, set_active, update_user, user_count
from config_loader import MASTER_DIR, check_config, load_config
from ginesys_client import GinesysClient
from ginesys_auth import GinesysLoginConflict, login_portal_user, request_ginesys_token
from job_store import RESULTS, UPLOADS, load_job, save_job
from parser import parse_files, refresh_document_site_config
from posting import post_documents, precheck_documents
from result_writer import write_result
from utils import safe_filename
from audit_store import list_events, log_event

ROOT = Path(__file__).resolve().parent
COMBO_FILE = MASTER_DIR / "combo_mapping.xlsx"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.secret_key = get_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def _config():
    return load_config()


def _current_user() -> dict | None:
    user = get_user(session.get("user_id"))
    if not user or not user.get("active"):
        session.pop("user_id", None)
        return None
    return user


def _api_user(*, admin: bool = False):
    user = _current_user()
    if not user:
        return None, (jsonify({"success": False, "error": "Login required."}), 401)
    if admin and user.get("role") != "admin":
        return None, (jsonify({"success": False, "error": "Administrator access required."}), 403)
    return user, None


def _audit(action: str, status: str, *, user: dict | None = None, details: str = "", username: str | None = None) -> None:
    try:
        log_event(
            action=action, status=status, user=user, details=details, username=username,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        )
    except Exception:
        app.logger.exception("Could not write audit event")


@app.get("/login")
def login_page():
    if _current_user():
        return redirect(url_for("index"))
    if user_count() == 0:
        return redirect(url_for("setup_page"))
    return render_template("login.html")


@app.get("/setup")
def setup_page():
    if user_count() > 0:
        return redirect(url_for("login_page"))
    return render_template("setup.html")


@app.get("/admin")
def admin_page():
    user = _current_user()
    if not user:
        return redirect(url_for("login_page"))
    if user.get("role") != "admin":
        return redirect(url_for("index"))
    return render_template("admin.html", user=public_user(user))


@app.get("/audit")
def audit_page():
    user = _current_user()
    if not user:
        return redirect(url_for("login_page"))
    if user.get("role") != "admin":
        return redirect(url_for("index"))
    return render_template("audit.html", user=public_user(user))


@app.get("/")
def index():
    user = _current_user()
    if not user:
        return redirect(url_for("setup_page" if user_count() == 0 else "login_page"))
    return render_template("index.html", user=public_user(user))


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "version": "4.5", "resultWorkbook": "Summary + Items + API_Log", "auth": True})


@app.get("/api/auth/me")
def auth_me():
    user = _current_user()
    return jsonify({
        "success": True,
        "authenticated": bool(user),
        "setupNeeded": user_count() == 0,
        "user": public_user(user),
    })


@app.post("/api/setup")
def setup_admin():
    if user_count() > 0:
        return jsonify({"success": False, "error": "Initial setup is already complete. Please login."}), 409
    data = request.get_json(silent=True) or request.form
    try:
        c = _config()
        ginesys_username = data.get("username") or data.get("ginesys_username")
        ginesys_password = data.get("password") or data.get("ginesys_password")
        token = request_ginesys_token(
            c.base_url, ginesys_username, ginesys_password, timeout=c.request_timeout,
            logout_existing_session=bool(data.get("logout_existing_session")),
        )
        user = create_user(
            username=ginesys_username, display_name=data.get("display_name"),
            ginesys_username=ginesys_username, ginesys_password=ginesys_password, role="admin",
        )
        user = save_ginesys_login(
            user["id"], ginesys_username, ginesys_password, token["token"], token["expires_at"], token["authenticated_at"],
            token["body"].get("userId") if isinstance(token.get("body"), dict) else None,
        )
    except GinesysLoginConflict as exc:
        _audit("LOGIN", "CONFIRMATION_REQUIRED", username=ginesys_username, details="Existing Ginesys Web session detected")
        return jsonify({"success": False, "requiresSessionLogout": True, "error": str(exc)}), 409
    except ValueError as exc:
        _audit("LOGIN", "FAILED", username=ginesys_username, details=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 422
    session.clear()
    session["user_id"] = user["id"]
    _audit("LOGIN", "SUCCESS", user=user, details="Initial administrator setup")
    return jsonify({"success": True, "user": public_user(user)})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or request.form
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    user = get_user_by_ginesys_username(username)
    if not user:
        _audit("LOGIN", "DENIED", username=username, details="Ginesys username is not authorized for the portal")
        return jsonify({"success": False, "error": "This Ginesys user is not authorized for this portal."}), 401
    try:
        c = _config()
        user = login_portal_user(
            c.base_url, user, username, password, timeout=c.request_timeout,
            logout_existing_session=bool(data.get("logout_existing_session")),
        )
    except GinesysLoginConflict as exc:
        _audit("LOGIN", "CONFIRMATION_REQUIRED", user=user, details="Existing Ginesys Web session detected")
        return jsonify({"success": False, "requiresSessionLogout": True, "error": str(exc)}), 409
    except ValueError as exc:
        _audit("LOGIN", "FAILED", user=user, details=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 401
    session.clear()
    session["user_id"] = user["id"]
    _audit("LOGIN", "SUCCESS", user=user, details="Existing Ginesys session was closed" if data.get("logout_existing_session") else "")
    return jsonify({"success": True, "user": public_user(user)})


@app.post("/api/logout")
def logout():
    user = _current_user()
    if user:
        _audit("LOGOUT", "SUCCESS", user=user)
    session.clear()
    return jsonify({"success": True})


@app.get("/api/admin/users")
def admin_users():
    _, error = _api_user(admin=True)
    if error:
        return error
    return jsonify({"success": True, "users": list_users()})


@app.get("/api/audit-events")
def audit_events():
    _, error = _api_user(admin=True)
    if error:
        return error
    return jsonify({"success": True, "events": list_events(request.args.get("limit", 500))})


@app.post("/api/admin/users")
def admin_create_user():
    _, error = _api_user(admin=True)
    if error:
        return error
    data = request.get_json(silent=True) or request.form
    try:
        user = create_user(
            username=data.get("ginesys_username"), display_name=data.get("display_name"),
            ginesys_username=data.get("ginesys_username"),
            role=data.get("role", "operator"),
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 422
    _audit("USER_CREATE", "SUCCESS", user=_current_user(), details=f"Authorized Ginesys user {user.get('ginesys_username')} as {user.get('role')}")
    return jsonify({"success": True, "user": public_user(user)})


@app.post("/api/admin/users/<int:user_id>/active")
def admin_set_user_active(user_id: int):
    current, error = _api_user(admin=True)
    if error:
        return error
    if current.get("id") == user_id:
        return jsonify({"success": False, "error": "You cannot deactivate your own account."}), 422
    data = request.get_json(silent=True) or request.form
    try:
        user = set_active(user_id, bool(data.get("active")))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 422
    _audit("USER_STATUS", "SUCCESS", user=current, details=f"{user.get('ginesys_username')}: {'activated' if user.get('active') else 'deactivated'}")
    return jsonify({"success": True, "user": public_user(user)})


@app.post("/api/admin/users/<int:user_id>")
def admin_update_user(user_id: int):
    current, error = _api_user(admin=True)
    if error:
        return error
    data = request.get_json(silent=True) or request.form
    try:
        user = update_user(
            user_id,
            display_name=data.get("display_name"),
            ginesys_user_code=None,
            ginesys_username=data.get("ginesys_username"),
            ginesys_password=data.get("ginesys_password") or None,
            role=data.get("role", "operator"),
            password=None,
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 422
    if current.get("id") == user_id and user.get("role") != "admin":
        session.clear()
    _audit("USER_UPDATE", "SUCCESS", user=current, details=f"Updated {user.get('ginesys_username')} ({user.get('role')})")
    return jsonify({"success": True, "user": public_user(user)})


@app.post("/api/admin/users/<int:user_id>/test-ginesys")
def admin_test_ginesys(user_id: int):
    _, error = _api_user(admin=True)
    if error:
        return error
    user = get_user(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404
    try:
        c = _config()
        GinesysClient(c, operator=get_user(user_id)).validate_token()
        refreshed = public_user(get_user(user_id))
        _audit("CONNECTION_TEST", "SUCCESS", user=user)
        return jsonify({"success": True, "message": "Connection successful.", "user": refreshed})
    except Exception as exc:
        _audit("CONNECTION_TEST", "FAILED", user=user, details=str(exc))
        return jsonify({"success": False, "error": str(exc), "user": public_user(get_user(user_id))}), 502


@app.get("/api/config-status")
def config_status():
    _, error = _api_user()
    if error:
        return error
    try:
        c = _config()
        config_errors, _ = check_config(c)
        if config_errors:
            raise ValueError("; ".join(config_errors))
        user = _current_user()
        return jsonify({
            "success": True,
            "version": "4.5",
            "tokenConfigured": bool(user and user.get("ginesys_username") and user.get("ginesys_password_encrypted")),
            "connectionStatus": user.get("ginesys_connection_status") if user else "not_configured",
            "lastAuthenticatedAt": user.get("ginesys_last_authenticated_at") if user else None,
            "ginesysUsername": user.get("ginesys_username") if user else None,
            "apiMode": c.api_mode,
            "baseUrl": c.base_url,
            "ownerSiteCode": c.owner_site_code,
            "dcDocCode": c.dc_doc_code,
            "outStockPointCode": c.out_stock_point_code,
            "invoiceGLCode": c.invoice_gl_code,
            "docCodeTN": c.doc_code_tn,
            "docCodeInterstate": c.doc_code_interstate,
            "discountFactor": c.factor,
            "siteCount": len(c.sites),
            "rateHandling": "Utility/SelectItem exact barcode lookup supplies Ginesys pricing",
            "warningCount": len(c.warnings),
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.get("/api/check-session")
def check_session():
    user, error = _api_user()
    if error:
        return error
    try:
        c = _config()
        errors, warnings = check_config(c)
        if errors:
            return jsonify({"success": False, "errors": errors, "warnings": warnings}), 422
        GinesysClient(c, operator=user).validate_token()
        refreshed = public_user(get_user(user["id"]))
        _audit("CONNECTION_TEST", "SUCCESS", user=user)
        return jsonify({"success": True, "message": "Ginesys connection is valid.", "warnings": warnings,
                        "connectionStatus": refreshed["ginesys_connection_status"],
                        "lastAuthenticatedAt": refreshed["ginesys_last_authenticated_at"]})
    except Exception as exc:
        _audit("CONNECTION_TEST", "FAILED", user=user, details=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 401 if getattr(exc, "status_code", None) == 401 else 502


@app.post("/api/validate")
def validate_job():
    user, error = _api_user()
    if error:
        return error
    files = request.files.getlist("files")
    if not files:
        return jsonify({"success": False, "error": "Upload at least one Odoo Excel/CSV file."}), 400

    job_id = uuid.uuid4().hex
    job_upload_dir = UPLOADS / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    seen_names = set()

    try:
        for f in files:
            name = safe_filename(f.filename or "input.xlsx")
            if Path(name).suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                raise ValueError(f"Unsupported file type: {name}")
            if name.casefold() in seen_names:
                raise ValueError(f"Two selected files have the same filename ({name}). Rename one file and upload again to prevent accidental quantity duplication.")
            seen_names.add(name.casefold())
            path = job_upload_dir / name
            f.save(path)
            paths.append(path)

        c = _config()
        parsed = parse_files(paths, c, COMBO_FILE)
        precheck = precheck_documents(parsed["documents"], c, operator=user)
        ready = bool(parsed["documents"])
        status = "VALIDATED" if ready else "BLOCKED"

        job = {
            "job_id": job_id,
            "documents": parsed["documents"],
            "warnings": parsed["warnings"],
            "stats": parsed["stats"],
            "combo_mappings": parsed["combo_mappings"],
            "ginesys_precheck": precheck,
            "ready": ready,
            "status": status,
            "rate_handling": "Exact barcode lookup from Utility/SelectItem; pricing is carried into DC/Save",
        }
        save_job(job_id, job)

        preview = [{
            "reference": d["reference"],
            "date": d["date"],
            "store": d["store_name"],
            "siteCode": d["site_code"],
            "state": d.get("state_code"),
            "gstAppl": d.get("gst_appl"),
            "gstinConfigured": bool(d.get("site_gstin")),
            "docCode": d["invoice_doc_code"],
            "lines": d["line_count"],
            "qty": d["total_qty"],
            "ginesysStatus": d["ginesys_precheck"]["status"],
            "dcNumber": d["ginesys_precheck"].get("dc_number") or d["ginesys_precheck"].get("dc_code"),
            "transferNumber": d["ginesys_precheck"].get("transfer_number") or d["ginesys_precheck"].get("transfer_code"),
        } for d in parsed["documents"][:100]]

        _audit(
            "VALIDATE", "SUCCESS" if ready else "BLOCKED", user=user,
            details=(f"Job {job_id[:8]}: {len(parsed['documents'])} documents, {parsed['stats']['valid_lines']} valid lines; "
                     f"Ginesys check: {precheck['summary']['alreadyCreated']} already created, "
                     f"{precheck['summary']['dcOnly']} DC only, {precheck['summary']['notFound']} not found"),
        )
        return jsonify({
            "success": True,
            "jobId": job_id,
            "ready": ready,
            # The frontend must fail closed when an old portal process is still
            # serving a response that did not execute the Ginesys duplicate check.
            "ginesysCheckCompleted": True,
            "ginesysCheckVersion": 1,
            "tokenConfigured": bool(user.get("ginesys_username") and user.get("ginesys_password_encrypted")),
            "summary": {
                "stores": len({str(d["store_name"]).strip().casefold() for d in parsed["documents"] if d.get("store_name")}),
                "documents": len(parsed["documents"]),
                "sourceLines": parsed["stats"]["source_lines"],
                "validLines": parsed["stats"]["valid_lines"],
                "comboLines": parsed["stats"]["combo_lines"],
                "totalQty": sum(float(d["total_qty"]) for d in parsed["documents"]),
                **precheck["summary"],
            },
            "rateHandling": "Every barcode will be matched exactly in Utility/SelectItem before DC/Save; zero or multiple matches block posting.",
            "warnings": parsed["warnings"][:30],
            "preview": preview,
        })
    except Exception as exc:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        _audit("VALIDATE", "FAILED", user=user, details=str(exc))
        return jsonify({"success": False, "error": str(exc)}), 422


@app.post("/api/post/<job_id>")
def post_job(job_id: str):
    user, error = _api_user()
    if error:
        return error
    try:
        if not user.get("ginesys_username") or not user.get("ginesys_password_encrypted"):
            return jsonify({"success": False, "error": "Live posting is blocked: your Ginesys credentials are not configured. Ask an administrator to update your user and test the connection."}), 422
        c = _config()
        config_errors, _ = check_config(c)
        if config_errors:
            return jsonify({"success": False, "error": "Live posting is blocked: " + "; ".join(config_errors)}), 422
        job = load_job(job_id)
        if not job.get("ready"):
            return jsonify({"success": False, "error": "This job is not ready for posting. Validate the source files again."}), 422
        # Refresh stored jobs from current site configuration so a corrected
        # GSTIN/scheme/GST flag is used on Retry Failed without recreating a DC.
        refresh_document_site_config(job["documents"], c)
        posted = post_documents(job["documents"], c, operator=user)
        result_name = f"Ginesys_Posting_Result_{job_id[:8]}.xlsx"
        write_result(RESULTS / result_name, job["documents"], posted["results"], posted["api_calls"], validation_status="POSTED")
        job["post_results"] = posted["results"]
        job["api_calls"] = posted["api_calls"]
        job["post_summary"] = posted["summary"]
        save_job(job_id, job)
        _audit(
            "POST", "SUCCESS" if posted["summary"]["failed"] == 0 else "PARTIAL",
            user=user, details=f"Job {job_id[:8]}: {posted['summary']['success']} successful, {posted['summary']['failed']} failed",
        )
        return jsonify({
            "success": True,
            "summary": posted["summary"],
            "results": posted["results"],
            "downloadUrl": f"/api/results/{result_name}",
        })
    except Exception as exc:
        _audit("POST", "FAILED", user=user, details=f"Job {job_id[:8]}: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.get("/api/results/<path:filename>")
def result_file(filename: str):
    user = _current_user()
    if not user:
        return redirect(url_for("login_page"))
    safe_name = safe_filename(filename)
    _audit("RESULT_DOWNLOAD", "SUCCESS", user=user, details=safe_name)
    return send_from_directory(RESULTS, safe_name, as_attachment=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify({"success": False, "error": "Upload is too large. Maximum total request size is 50 MB."}), 413
