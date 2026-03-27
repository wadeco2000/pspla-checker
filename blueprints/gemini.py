"""Gemini AI Phone Calls — Flask Blueprint.

Provides the /gemini page for managing AI phone calls using
Google Gemini 3.1 Flash Live + Twilio telephony.
"""
import os
import re
import json
import requests as _requests
from datetime import datetime, timezone
from flask import Blueprint, render_template_string, request, jsonify, session, redirect
from markupsafe import escape as _esc

gemini_bp = Blueprint('gemini', __name__)

# ── Injected by dashboard.py at registration ────────────────────────────────
_is_admin = lambda: False
_has_permission = lambda g: False
_require_permission = lambda g: None
SUPABASE_URL = ""
SUPABASE_SERVICE_KEY = ""
_get_git_version = lambda: ("unknown", "")

# ── Environment ─────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CALL_SERVER_URL = os.getenv("GEMINI_CALL_SERVER_URL", "http://localhost:8001")
GEMINI_CALL_SERVER_SECRET = os.getenv("GEMINI_CALL_SERVER_SECRET", "")

# ── Validation ──────────────────────────────────────────────────────────────
_RE_NZ_PHONE = re.compile(r"^\+?64[2-9]\d{7,9}$")
_RE_LOCAL_PHONE = re.compile(r"^0[2-9]\d{7,9}$")

def _validate_nz_phone(number):
    """Validate and normalise NZ phone number to +64 format."""
    num = re.sub(r"[\s\-\(\)]", "", number.strip())
    if _RE_NZ_PHONE.match(num):
        return num if num.startswith("+") else f"+{num}"
    if _RE_LOCAL_PHONE.match(num):
        return f"+64{num[1:]}"
    return None

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

def _has_pspla_access():
    if _is_admin():
        return True
    perms = session.get("permissions") or {}
    return any(perms.get(g) for g in ("searches", "database", "history", "utilities"))


# ═══════════════════════════════════════════════════════════════════════════════
#  PAGE ROUTE
# ═══════════════════════════════════════════════════════════════════════════════

@gemini_bp.before_request
def _check_gemini_permission():
    """Gate all Gemini routes behind the 'gemini' permission group."""
    return _require_permission("gemini")

@gemini_bp.route("/gemini")
def gemini_page():
    git_ver = _get_git_version()
    return render_template_string(
        GEMINI_TEMPLATE,
        is_admin=_is_admin(),
        has_pspla_access=_has_pspla_access(),
        user_email=session.get("email", ""),
        user_avatar=session.get("avatar_url", ""),
        git_version=git_ver,
        twilio_number=os.getenv("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
        call_server_url=os.getenv("GEMINI_CALL_SERVER_URL", GEMINI_CALL_SERVER_URL),
        gemini_configured=bool(os.getenv("GEMINI_API_KEY", GEMINI_API_KEY) and os.getenv("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID)),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  KNOWLEDGE BASE CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@gemini_bp.route("/api/gemini/knowledge-bases", methods=["GET", "POST"])
def gemini_knowledge_bases():
    if request.method == "GET":
        try:
            r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
                params={"select": "*", "order": "name.asc"},
                headers=_sb_headers(), timeout=10)
            return jsonify({"ok": True, "knowledge_bases": r.json() if r.ok else []})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502

    # POST — create or update
    data = request.json or {}
    name = (data.get("name") or "").strip()
    content = (data.get("content") or "").strip()
    voice_name = (data.get("voice_name") or "Kore").strip()
    kb_id = data.get("id")

    if not name or not content:
        return jsonify({"ok": False, "error": "Name and content are required."}), 400
    if len(content) > 50000:
        return jsonify({"ok": False, "error": "Content too long (max 50,000 chars)."}), 400
    if voice_name not in ("Kore", "Charon", "Fenrir", "Aoede", "Puck"):
        voice_name = "Kore"

    try:
        payload = {"name": name, "content": content, "voice_name": voice_name,
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        headers = {**_sb_headers(), "Prefer": "return=representation"}
        if kb_id:
            # Update
            r = _requests.patch(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
                params={"id": f"eq.{kb_id}"}, json=payload, headers=headers, timeout=10)
        else:
            # Create
            r = _requests.post(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
                json=payload, headers=headers, timeout=10)
        return jsonify({"ok": r.ok, "data": r.json() if r.ok else None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gemini_bp.route("/api/gemini/knowledge-bases/<int:kb_id>", methods=["DELETE"])
def gemini_delete_kb(kb_id):
    try:
        r = _requests.delete(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
            params={"id": f"eq.{kb_id}"},
            headers={**_sb_headers(), "Prefer": "return=minimal"}, timeout=10)
        return jsonify({"ok": r.ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ═══════════════════════════════════════════════════════════════════════════════
#  CALL MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@gemini_bp.route("/api/gemini/make-call", methods=["POST"])
def gemini_make_call():
    _server_url = os.getenv("GEMINI_CALL_SERVER_URL", GEMINI_CALL_SERVER_URL)
    _server_secret = os.getenv("GEMINI_CALL_SERVER_SECRET", GEMINI_CALL_SERVER_SECRET)
    _twilio_number = os.getenv("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER)
    if not _server_url or not _server_secret:
        return jsonify({"ok": False, "error": "Call server not configured."}), 500
    if not _twilio_number:
        return jsonify({"ok": False, "error": "Twilio phone number not configured."}), 500

    data = request.json or {}
    to_number = _validate_nz_phone(data.get("to_number", ""))
    if not to_number:
        return jsonify({"ok": False, "error": "Invalid NZ phone number. Use format: 021 123 4567 or +6421 123 4567"}), 400

    kb_id = data.get("knowledge_base_id")
    settings = data.get("settings", {})
    system_instruction = ""
    voice_name = "Kore"

    if kb_id:
        try:
            r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
                params={"select": "content,voice_name", "id": f"eq.{kb_id}"},
                headers=_sb_headers(), timeout=10)
            if r.ok and r.json():
                kb = r.json()[0]
                system_instruction = kb.get("content", "")
                voice_name = kb.get("voice_name", "Kore")
        except Exception:
            pass

    # Call the FastAPI server to initiate the call
    try:
        r = _requests.post(f"{_server_url}/api/make-call",
            headers={"X-Server-Secret": _server_secret, "Content-Type": "application/json"},
            json={
                "to_number": to_number,
                "from_number": _twilio_number,
                "system_instruction": system_instruction,
                "voice_name": voice_name,
                "triggered_by": session.get("email", "unknown"),
                "settings": settings,
            },
            timeout=30)
        result = r.json()
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error", "Call server error")}), 502

        # Save to call history
        try:
            _requests.post(f"{SUPABASE_URL}/rest/v1/gemini_call_history",
                json={
                    "call_sid": result.get("call_sid", ""),
                    "call_id": result.get("call_id", ""),
                    "to_number": to_number,
                    "from_number": _twilio_number,
                    "knowledge_base_id": kb_id,
                    "status": "initiated",
                    "triggered_by": session.get("email", "unknown"),
                },
                headers={**_sb_headers(), "Prefer": "return=minimal"}, timeout=10)
        except Exception:
            pass

        return jsonify({"ok": True, "call_sid": result.get("call_sid"), "call_id": result.get("call_id")})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to reach call server: {e}"}), 502


@gemini_bp.route("/api/gemini/end-call", methods=["POST"])
def gemini_end_call():
    data = request.json or {}
    call_sid = data.get("call_sid", "")
    if not call_sid or not re.match(r"^CA[a-f0-9]{32}$", call_sid):
        return jsonify({"ok": False, "error": "Invalid call SID."}), 400

    try:
        r = _requests.post(f"{os.getenv('GEMINI_CALL_SERVER_URL', GEMINI_CALL_SERVER_URL)}/api/end-call",
            headers={"X-Server-Secret": os.getenv('GEMINI_CALL_SERVER_SECRET', GEMINI_CALL_SERVER_SECRET), "Content-Type": "application/json"},
            json={"call_sid": call_sid},
            timeout=15)
        return jsonify(r.json() if r.ok else {"ok": False, "error": "Call server error"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ═══════════════════════════════════════════════════════════════════════════════
#  CALL HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

@gemini_bp.route("/api/gemini/call-history", methods=["GET"])
def gemini_call_history():
    try:
        r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"select": "*", "order": "started_at.desc", "limit": "100"},
            headers=_sb_headers(), timeout=10)
        return jsonify({"ok": True, "calls": r.json() if r.ok else []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gemini_bp.route("/api/gemini/call/<call_sid>", methods=["GET"])
def gemini_call_detail(call_sid):
    if not re.match(r"^CA[a-f0-9]{32}$", call_sid):
        return jsonify({"ok": False, "error": "Invalid call SID."}), 400
    try:
        r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"select": "*", "call_sid": f"eq.{call_sid}"},
            headers=_sb_headers(), timeout=10)
        data = r.json()
        if r.ok and data:
            return jsonify({"ok": True, "call": data[0]})
        return jsonify({"ok": False, "error": "Call not found."}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gemini_bp.route("/api/gemini/recording/<call_sid>", methods=["GET"])
def gemini_recording_proxy(call_sid):
    """Proxy Twilio recording download (requires Twilio auth)."""
    if not re.match(r"^CA[a-f0-9]{32}$", call_sid):
        return jsonify({"ok": False, "error": "Invalid call SID."}), 400
    # Get recording URL from Supabase
    try:
        r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"select": "recording_url", "call_sid": f"eq.{call_sid}"},
            headers=_sb_headers(), timeout=10)
        data = r.json()
        if not r.ok or not data or not data[0].get("recording_url"):
            return jsonify({"ok": False, "error": "No recording found."}), 404
        rec_url = data[0]["recording_url"]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Validate URL is from Twilio (prevent SSRF)
    if not re.match(r"^https://api\.twilio\.com/", rec_url):
        return jsonify({"ok": False, "error": "Invalid recording URL."}), 400

    # Fetch from Twilio with auth
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    try:
        resp = _requests.get(rec_url, auth=(sid, token), timeout=30, stream=True)
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"Twilio returned {resp.status_code}"}), 502
        from flask import Response as FlaskResponse
        return FlaskResponse(
            resp.content,
            content_type=resp.headers.get("Content-Type", "audio/mpeg"),
            headers={"Content-Disposition": f"attachment; filename=call-{call_sid}.mp3"}
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gemini_bp.route("/api/gemini/debug", methods=["GET"])
def gemini_debug():
    """Fetch debug info from all components."""
    server_url = os.getenv("GEMINI_CALL_SERVER_URL", "http://localhost:8001")
    secret = os.getenv("GEMINI_CALL_SERVER_SECRET", "")
    headers = {"X-Server-Secret": secret}
    result = {"call_server": {}, "twilio": {}, "gemini": {}, "supabase": {}}

    # 1. Call server health + errors + active calls
    try:
        h = _requests.get(f"{server_url}/health", headers=headers, timeout=5)
        result["call_server"]["health"] = h.json() if h.ok else {"status": h.status_code}
    except Exception as e:
        result["call_server"]["health"] = {"error": str(e)}
    try:
        e = _requests.get(f"{server_url}/debug/errors", headers=headers, timeout=5)
        result["call_server"]["errors"] = e.json().get("errors", []) if e.ok else []
    except Exception as e:
        result["call_server"]["errors"] = [str(e)]
    try:
        a = _requests.get(f"{server_url}/debug/active-calls", headers=headers, timeout=5)
        result["call_server"]["active_calls"] = a.json() if a.ok else {}
    except Exception as e:
        result["call_server"]["active_calls"] = {"error": str(e)}

    # 2. Gemini API test
    try:
        g = _requests.get(f"{server_url}/debug/test-gemini", headers=headers, timeout=15)
        result["gemini"] = g.json() if g.ok else {"error": f"HTTP {g.status_code}"}
    except Exception as e:
        result["gemini"] = {"error": str(e)}

    # 3. Twilio — check credentials by fetching account info
    try:
        from twilio.rest import Client as TwilioClient
        tc = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID", ""),
            os.getenv("TWILIO_AUTH_TOKEN", "")
        )
        acct = tc.api.accounts(os.getenv("TWILIO_ACCOUNT_SID", "")).fetch()
        result["twilio"] = {
            "ok": True,
            "friendly_name": acct.friendly_name,
            "status": acct.status,
            "phone": os.getenv("TWILIO_PHONE_NUMBER", ""),
        }
    except ImportError:
        result["twilio"] = {"ok": True, "note": "twilio SDK not installed on dashboard, call server handles it"}
    except Exception as e:
        result["twilio"] = {"ok": False, "error": str(e)}

    # 4. Supabase — check tables exist
    try:
        r = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_knowledge_bases",
            params={"select": "id", "limit": "1"}, headers=_sb_headers(), timeout=5)
        kb_ok = r.ok
        r2 = _requests.get(f"{SUPABASE_URL}/rest/v1/gemini_call_history",
            params={"select": "id", "limit": "1"}, headers=_sb_headers(), timeout=5)
        ch_ok = r2.ok
        result["supabase"] = {"ok": kb_ok and ch_ok, "knowledge_bases": "OK" if kb_ok else "ERROR", "call_history": "OK" if ch_ok else "ERROR"}
    except Exception as e:
        result["supabase"] = {"ok": False, "error": str(e)}

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
#  PAGE TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════

GEMINI_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script>(function(){var _f=window.fetch;window.fetch=function(u,o){o=o||{};var m=(o.method||'GET').toUpperCase();if(m!=='GET'&&m!=='HEAD'){o.headers=o.headers||{};o.headers['X-CSRF-Token']=document.querySelector('meta[name="csrf-token"]').content;}return _f.call(this,u,o);};})();</script>
    <title>Gemini AI Calls</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        *{margin:0;padding:0;box-sizing:border-box;}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#333;}
        .header{background:#1a2332;color:white;padding:12px 24px;display:flex;justify-content:space-between;align-items:center;}
        .header h1{font-size:18px;font-weight:600;} .header h1 i{color:#4CAF50;margin-right:6px;}
        .header-right{display:flex;align-items:center;gap:12px;font-size:13px;}
        .header-right a{color:#aaa;text-decoration:none;} .header-right a:hover{color:white;}
        .btn{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;display:inline-flex;align-items:center;gap:5px;transition:opacity 0.15s;}
        .btn:hover{opacity:0.85;}
        .btn-green{background:#27ae60;color:white;} .btn-red{background:#e74c3c;color:white;}
        .btn-blue{background:#3498db;color:white;} .btn-orange{background:#e67e22;color:white;}
        .btn-purple{background:#8e44ad;color:white;} .btn-grey{background:#95a5a6;color:white;}
        .btn-lg{padding:12px 28px;font-size:15px;border-radius:8px;}
        .card{background:white;border-radius:10px;padding:20px;margin:16px 24px;box-shadow:0 1px 3px rgba(0,0,0,0.08);}
        .card h2{font-size:15px;font-weight:600;color:#555;margin-bottom:12px;display:flex;align-items:center;gap:8px;}
        input[type="text"],input[type="tel"],textarea,select{border:1px solid #ddd;border-radius:6px;padding:8px 12px;font-size:13px;width:100%;font-family:inherit;}
        textarea{resize:vertical;min-height:200px;}
        select{width:auto;min-width:200px;}
        .form-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px;}
        .form-label{font-size:12px;font-weight:600;color:#555;min-width:100px;}
        .status-bar{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:10px 16px;margin:0 24px;font-size:13px;display:none;}
        .status-bar.active{display:block;}
        .status-bar.error{background:#f8d7da;border-color:#dc3545;color:#721c24;}
        .status-bar.success{background:#d4edda;border-color:#28a745;color:#155724;}
        /* Transcript panel */
        .transcript-panel{background:#1a1a2e;border-radius:8px;min-height:300px;max-height:500px;overflow-y:auto;padding:16px;font-family:'Courier New',monospace;font-size:13px;}
        .transcript-line{padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05);}
        .transcript-line.ai{color:#64B5F6;} .transcript-line.caller{color:#E0E0E0;}
        .transcript-line .speaker{font-weight:bold;margin-right:8px;}
        .transcript-line .time{color:#666;font-size:10px;margin-right:8px;}
        .debug-row{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;}
        .debug-label{font-weight:600;min-width:120px;color:#555;}
        /* Call controls */
        .call-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px;}
        .call-timer{font-size:20px;font-weight:bold;color:#333;font-family:'Courier New',monospace;}
        /* History table */
        .history-table{width:100%;border-collapse:collapse;font-size:12px;}
        .history-table th{background:#f8f9fa;padding:8px;text-align:left;border-bottom:2px solid #e2e8f0;color:#555;font-size:11px;}
        .history-table td{padding:8px;border-bottom:1px solid #f0f0f0;}
        .history-table tr:hover{background:#f8f9fa;}
        .badge{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;}
        .badge-green{background:#d4edda;color:#155724;} .badge-red{background:#f8d7da;color:#721c24;}
        .badge-blue{background:#cce5ff;color:#004085;} .badge-grey{background:#e2e8f0;color:#555;}
        /* Modal */
        .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;justify-content:center;align-items:center;}
        .modal-overlay.active{display:flex;}
        .modal{background:white;border-radius:12px;padding:24px;max-width:700px;width:90%;max-height:80vh;overflow-y:auto;}
        .modal h3{margin-bottom:16px;font-size:16px;}
        .empty-state{text-align:center;padding:40px;color:#aaa;font-size:14px;}
        .config-warning{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:16px;margin:16px 24px;color:#856404;}
    </style>
</head>
<body>
    <div class="header">
        <h1><i class="fa-solid fa-phone"></i> Gemini AI Calls</h1>
        <div class="header-right">
            {% if has_pspla_access %}<a href="/"><i class="fa-solid fa-arrow-left"></i> Dashboard</a>{% endif %}
            <a href="/my-account"><i class="fa-solid fa-user"></i> My Account</a>
            <span>{{ user_email }}</span>
            <a href="/auth/logout" class="btn btn-red" style="font-size:11px;padding:4px 10px;"><i class="fa-solid fa-right-from-bracket"></i> Sign out</a>
            <button class="btn" style="font-size:11px;padding:4px 10px;background:#6c757d;" onclick="openDebug()"><i class="fa-solid fa-bug"></i> Debug</button>
            <span style="font-size:10px;color:#666;">&#9765; {{ git_version[0] }} {{ git_version[1] }}</span>
        </div>
    </div>

    {% if not gemini_configured %}
    <div class="config-warning">
        <i class="fa-solid fa-triangle-exclamation"></i> <strong>Not configured.</strong>
        Add GEMINI_API_KEY and TWILIO_ACCOUNT_SID to environment variables to enable calling.
    </div>
    {% endif %}

    <!-- Status bar -->
    <div id="status-bar" class="status-bar"></div>

    <!-- Knowledge Base + Call Setup -->
    <div class="card">
        <h2><i class="fa-solid fa-book"></i> Knowledge Base & Call Setup</h2>
        <div class="form-row">
            <span class="form-label">Knowledge Base:</span>
            <select id="kb-select" onchange="loadKbContent()">
                <option value="">— Select or create —</option>
            </select>
            <button class="btn btn-green" onclick="showKbModal(false)"><i class="fa-solid fa-plus"></i> New</button>
            <button class="btn btn-blue" onclick="showKbModal(true)" id="btn-edit-kb" style="display:none;"><i class="fa-solid fa-pen"></i> Edit</button>
            <button class="btn btn-red" onclick="deleteKb()" id="btn-delete-kb" style="display:none;"><i class="fa-solid fa-trash"></i> Delete</button>
        </div>
        <div id="kb-preview" style="display:none;margin-top:10px;padding:10px;background:#f8f9fa;border-radius:6px;font-size:12px;max-height:150px;overflow-y:auto;white-space:pre-wrap;color:#555;"></div>

        <hr style="margin:16px 0;border:none;border-top:1px solid #eee;">

        <div class="form-row">
            <span class="form-label">Call Number:</span>
            <input type="tel" id="call-number" placeholder="021 123 4567 or +6421 123 4567" style="max-width:300px;">
            <span style="font-size:11px;color:#888;">Calling from: <strong>{{ twilio_number or 'Not set' }}</strong></span>
        </div>
        <div class="form-row" style="margin-top:12px;">
            <button class="btn btn-green btn-lg" onclick="makeCall()" id="btn-call" {% if not gemini_configured %}disabled{% endif %}>
                <i class="fa-solid fa-phone"></i> Call
            </button>
            <button class="btn btn-grey" onclick="toggleSettings()" style="margin-left:8px;">
                <i class="fa-solid fa-sliders"></i> Settings
            </button>
        </div>
    </div>

    <!-- Settings Panel (collapsible) -->
    <div class="card" id="settings-panel" style="display:none;">
        <h2><i class="fa-solid fa-sliders"></i> Call Settings</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div>
                <label class="form-label">Language</label>
                <select id="set-language" style="width:100%;">
                    <option value="en" selected>English</option>
                    <option value="en-NZ">English (NZ)</option>
                    <option value="en-AU">English (AU)</option>
                    <option value="en-GB">English (UK)</option>
                    <option value="en-US">English (US)</option>
                    <option value="mi">Te Reo Māori</option>
                    <option value="es">Spanish</option>
                    <option value="fr">French</option>
                    <option value="de">German</option>
                    <option value="zh">Chinese</option>
                    <option value="ja">Japanese</option>
                    <option value="ko">Korean</option>
                    <option value="hi">Hindi</option>
                </select>
                <span style="font-size:10px;color:#888;">The language the AI will speak and listen in. The caller can still speak another language but transcription accuracy may be reduced.</span>
            </div>
            <div>
                <label class="form-label">Thinking Level</label>
                <select id="set-thinking" style="width:100%;">
                    <option value="minimal" selected>Minimal (fastest)</option>
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High (most thoughtful)</option>
                </select>
                <span style="font-size:10px;color:#888;">Controls how much the AI "thinks" before responding. Higher levels give more considered answers but add latency. Minimal is best for natural phone conversations.</span>
            </div>
            <div>
                <label class="form-label">Start of Speech Sensitivity</label>
                <select id="set-start-sensitivity" style="width:100%;">
                    <option value="low" selected>Low</option>
                    <option value="default">Default</option>
                    <option value="high">High</option>
                </select>
                <span style="font-size:10px;color:#888;">How easily the caller's voice interrupts the AI. Low means background noise, coughs, and "hmm" sounds won't interrupt. High means the AI stops at the slightest sound. Low is recommended for phone calls.</span>
            </div>
            <div>
                <label class="form-label">End of Speech Sensitivity</label>
                <select id="set-end-sensitivity" style="width:100%;">
                    <option value="low">Low</option>
                    <option value="default" selected>Default</option>
                    <option value="high">High</option>
                </select>
                <span style="font-size:10px;color:#888;">How quickly the AI decides the caller has finished speaking. Low waits longer for the caller to continue (good for slow or thoughtful speakers). High responds quickly after any pause (good for fast-paced conversations).</span>
            </div>
            <div>
                <label class="form-label">Silence Duration (ms)</label>
                <input type="number" id="set-silence-ms" value="500" min="100" max="5000" step="100" style="width:100%;">
                <span style="font-size:10px;color:#888;">How many milliseconds of silence before the AI considers the caller's turn finished and starts responding. Lower values (300ms) feel snappier but may cut people off. Higher values (1000ms+) give the caller more time to pause and continue.</span>
            </div>
            <div>
                <label class="form-label">Include AI Thoughts</label>
                <label style="font-weight:normal;display:flex;align-items:center;gap:6px;margin-top:4px;">
                    <input type="checkbox" id="set-include-thoughts"> Show AI reasoning in transcript
                </label>
                <span style="font-size:10px;color:#888;">When enabled, the AI's internal reasoning is included in the live transcript. Useful for debugging knowledge base prompts but adds noise to the transcript.</span>
            </div>
        </div>
        <div style="margin-top:12px;padding:8px;background:#fff3cd;border-radius:6px;font-size:11px;color:#856404;">
            <i class="fa-solid fa-info-circle"></i> <strong>Affective Dialog</strong> and <strong>Proactive Audio</strong> require Gemini 2.5 Flash Live (not available on 3.1). These features will be added when model support is confirmed.
        </div>
    </div>

    <!-- Active Call Panel (hidden until call active) -->
    <div class="card" id="active-call-panel" style="display:none;">
        <h2>
            <i class="fa-solid fa-phone-volume" style="color:#27ae60;"></i> Active Call
            <span id="call-timer" class="call-timer" style="margin-left:auto;">00:00</span>
        </h2>
        <div class="call-controls">
            <button class="btn btn-red btn-lg" onclick="hangUp()"><i class="fa-solid fa-phone-slash"></i> Hang Up</button>
            <button class="btn btn-orange" onclick="toggleMonitor()" id="btn-monitor"><i class="fa-solid fa-headphones"></i> Monitor</button>
            <button class="btn btn-purple" onclick="toggleBarge()" id="btn-barge"><i class="fa-solid fa-microphone"></i> Barge In</button>
            <span id="call-status" style="font-size:12px;color:#888;margin-left:auto;"></span>
        </div>
        <div style="margin-top:12px;">
            <strong style="font-size:12px;color:#555;"><i class="fa-solid fa-file-lines"></i> Live Transcript</strong>
            <div class="transcript-panel" id="transcript-panel">
                <div class="empty-state" style="color:#666;">Waiting for call to connect...</div>
            </div>
        </div>
    </div>

    <!-- Call History -->
    <div class="card">
        <h2><i class="fa-solid fa-clock-rotate-left"></i> Call History
            <button class="btn btn-grey" onclick="loadHistory()" style="margin-left:auto;font-size:11px;"><i class="fa-solid fa-rotate"></i> Refresh</button>
        </h2>
        <div id="history-container">
            <div class="empty-state">Loading...</div>
        </div>
    </div>

    <!-- KB Edit Modal -->
    <div class="modal-overlay" id="kb-modal">
        <div class="modal">
            <h3 id="kb-modal-title">New Knowledge Base</h3>
            <div class="form-row">
                <span class="form-label">Name:</span>
                <input type="text" id="kb-name" placeholder="e.g. Alarm Monitoring Script">
            </div>
            <div class="form-row">
                <span class="form-label">Voice:</span>
                <select id="kb-voice">
                    <option value="Kore">Kore (female, calm)</option>
                    <option value="Charon">Charon (male, deep)</option>
                    <option value="Fenrir">Fenrir (male, energetic)</option>
                    <option value="Aoede">Aoede (female, warm)</option>
                    <option value="Puck">Puck (male, casual)</option>
                </select>
            </div>
            <div style="margin-top:10px;">
                <label class="form-label">System Instructions / Knowledge Base Content:</label>
                <textarea id="kb-content" placeholder="You are Sarah, a professional alarm monitoring operator for Alarm Watch NZ.

COMPANY INFO:
- We provide 24/7 alarm monitoring for residential and commercial properties
- Emergency line: 0800 123 456
- Office hours: Mon-Fri 8am-5pm

CALL PROCEDURE:
1. Introduce yourself: 'Hi, this is Sarah from Alarm Watch'
2. Confirm their name and address
3. Explain why you're calling
..."></textarea>
            </div>
            <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
                <button class="btn btn-grey" onclick="closeKbModal()">Cancel</button>
                <button class="btn btn-green" onclick="saveKb()"><i class="fa-solid fa-save"></i> Save</button>
            </div>
        </div>
    </div>

    <!-- Transcript Modal (for history) -->
    <div class="modal-overlay" id="transcript-modal">
        <div class="modal" style="max-width:800px;">
            <h3>Call Transcript</h3>
            <div id="transcript-modal-content" class="transcript-panel" style="max-height:60vh;"></div>
            <div style="margin-top:12px;text-align:right;">
                <button class="btn btn-grey" onclick="document.getElementById('transcript-modal').classList.remove('active')">Close</button>
            </div>
        </div>
    </div>

    <!-- Debug Modal -->
    <div class="modal-overlay" id="debug-modal">
        <div class="modal" style="max-width:900px;">
            <h3><i class="fa-solid fa-bug"></i> System Debug</h3>
            <div id="debug-content" style="max-height:65vh;overflow-y:auto;">
                <div class="empty-state" style="color:#666;">Loading...</div>
            </div>
            <div style="margin-top:12px;text-align:right;display:flex;gap:8px;justify-content:flex-end;">
                <button class="btn" style="background:#6c757d;" onclick="openDebug()"><i class="fa-solid fa-refresh"></i> Refresh</button>
                <button class="btn btn-grey" onclick="document.getElementById('debug-modal').classList.remove('active')">Close</button>
            </div>
        </div>
    </div>

<script>
var _callServerUrl = '{{ call_server_url }}';
var _activeCallSid = null;
var _activeCallId = null;
var _callTimerInterval = null;
var _callStartTime = null;
var _transcriptWs = null;
var _monitorActive = false;
var _bargeActive = false;
var _knowledgeBases = [];
var _editingKbId = null;

function esc(s) { var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

function showStatus(msg, type) {
    var bar = document.getElementById('status-bar');
    bar.textContent = msg;
    bar.className = 'status-bar active' + (type ? ' ' + type : '');
    if (type === 'success') setTimeout(function(){ bar.className = 'status-bar'; }, 5000);
}

// ── Knowledge Base ──
function loadKnowledgeBases() {
    fetch('/api/gemini/knowledge-bases').then(r=>r.json()).then(d=>{
        if (!d.ok) return;
        _knowledgeBases = d.knowledge_bases;
        var sel = document.getElementById('kb-select');
        var current = sel.value;
        sel.innerHTML = '<option value="">— Select or create —</option>';
        _knowledgeBases.forEach(function(kb){
            sel.innerHTML += '<option value="' + kb.id + '">' + esc(kb.name) + ' (' + esc(kb.voice_name) + ')</option>';
        });
        if (current) sel.value = current;
        loadKbContent();
    });
}

function loadKbContent() {
    var id = document.getElementById('kb-select').value;
    var preview = document.getElementById('kb-preview');
    var editBtn = document.getElementById('btn-edit-kb');
    var delBtn = document.getElementById('btn-delete-kb');
    if (!id) {
        preview.style.display = 'none';
        editBtn.style.display = 'none';
        delBtn.style.display = 'none';
        return;
    }
    var kb = _knowledgeBases.find(function(k){ return k.id == id; });
    if (kb) {
        preview.style.display = 'block';
        preview.textContent = kb.content.substring(0, 500) + (kb.content.length > 500 ? '...' : '');
        editBtn.style.display = '';
        delBtn.style.display = '';
    }
}

function showKbModal(editing) {
    _editingKbId = null;
    document.getElementById('kb-modal-title').textContent = 'New Knowledge Base';
    document.getElementById('kb-name').value = '';
    document.getElementById('kb-content').value = '';
    document.getElementById('kb-voice').value = 'Kore';

    if (editing) {
        var id = document.getElementById('kb-select').value;
        var kb = _knowledgeBases.find(function(k){ return k.id == id; });
        if (!kb) return;
        _editingKbId = kb.id;
        document.getElementById('kb-modal-title').textContent = 'Edit: ' + kb.name;
        document.getElementById('kb-name').value = kb.name;
        document.getElementById('kb-content').value = kb.content;
        document.getElementById('kb-voice').value = kb.voice_name;
    }
    document.getElementById('kb-modal').classList.add('active');
}

function closeKbModal() { document.getElementById('kb-modal').classList.remove('active'); }

function saveKb() {
    var payload = {
        name: document.getElementById('kb-name').value,
        content: document.getElementById('kb-content').value,
        voice_name: document.getElementById('kb-voice').value
    };
    if (_editingKbId) payload.id = _editingKbId;
    fetch('/api/gemini/knowledge-bases', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
    .then(r=>r.json()).then(d=>{
        if (!d.ok) { alert('Error: ' + (d.error||'Unknown')); return; }
        closeKbModal();
        loadKnowledgeBases();
        showStatus('Knowledge base saved.', 'success');
    });
}

function deleteKb() {
    var id = document.getElementById('kb-select').value;
    if (!id || !confirm('Delete this knowledge base?')) return;
    fetch('/api/gemini/knowledge-bases/' + id, {method:'DELETE'}).then(r=>r.json()).then(d=>{
        if (!d.ok) { alert('Error deleting.'); return; }
        document.getElementById('kb-select').value = '';
        loadKnowledgeBases();
        showStatus('Knowledge base deleted.', 'success');
    });
}

// ── Call Management ──
function toggleSettings() {
    var panel = document.getElementById('settings-panel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function getCallSettings() {
    return {
        language: document.getElementById('set-language').value,
        thinking_level: document.getElementById('set-thinking').value,
        include_thoughts: document.getElementById('set-include-thoughts').checked,
        start_sensitivity: document.getElementById('set-start-sensitivity').value,
        end_sensitivity: document.getElementById('set-end-sensitivity').value,
        silence_duration_ms: parseInt(document.getElementById('set-silence-ms').value) || 500,
    };
}

function makeCall() {
    var number = document.getElementById('call-number').value.trim();
    if (!number) { alert('Enter a phone number.'); return; }
    var kbId = document.getElementById('kb-select').value;
    if (!kbId) { if (!confirm('No knowledge base selected. The AI will have no context. Continue?')) return; }
    if (!confirm('Call ' + number + '?')) return;

    showStatus('Initiating call...', '');
    var payload = {to_number: number, knowledge_base_id: kbId ? parseInt(kbId) : null, settings: getCallSettings()};
    fetch('/api/gemini/make-call', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
    }).then(r=>r.json()).then(d=>{
        if (!d.ok) { showStatus('Error: ' + (d.error||'Unknown'), 'error'); return; }
        _activeCallSid = d.call_sid;
        _activeCallId = d.call_id;
        showStatus('Call initiated! Waiting for connection...', 'success');
        showActiveCall();
        connectTranscriptWs();
    }).catch(function(e){ showStatus('Network error: ' + e, 'error'); });
}

function showActiveCall() {
    document.getElementById('active-call-panel').style.display = 'block';
    document.getElementById('transcript-panel').innerHTML = '<div class="empty-state" style="color:#666;">Waiting for call to connect...</div>';
    _callStartTime = Date.now();
    _callTimerInterval = setInterval(updateCallTimer, 1000);
    document.getElementById('btn-call').disabled = true;
}

function updateCallTimer() {
    if (!_callStartTime) return;
    var secs = Math.floor((Date.now() - _callStartTime) / 1000);
    var mins = Math.floor(secs / 60);
    secs = secs % 60;
    document.getElementById('call-timer').textContent = String(mins).padStart(2,'0') + ':' + String(secs).padStart(2,'0');
    // 15-minute warning
    if (mins === 14 && secs === 0) showStatus('Warning: Gemini session limit is 15 minutes. Call will end soon.', 'error');
}

function hangUp() {
    if (!_activeCallSid) return;
    if (!confirm('Hang up this call?')) return;
    fetch('/api/gemini/end-call', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({call_sid: _activeCallSid})
    }).then(r=>r.json()).then(function(d){
        endCall();
        showStatus('Call ended.', 'success');
    });
}

function endCall() {
    _activeCallSid = null;
    _activeCallId = null;
    if (_callTimerInterval) clearInterval(_callTimerInterval);
    _callTimerInterval = null;
    _callStartTime = null;
    if (_transcriptWs) { _transcriptWs.close(); _transcriptWs = null; }
    // Clean up monitor and barge resources
    if (_monitorActive) _stopMonitor();
    if (_bargeActive) _stopBarge();
    document.getElementById('active-call-panel').style.display = 'none';
    document.getElementById('call-status').style.color = '#888';
    document.getElementById('btn-call').disabled = false;
    loadHistory();
}

function connectTranscriptWs() {
    if (!_activeCallId || !_callServerUrl) return;
    var wsUrl = _callServerUrl.replace('http', 'ws') + '/ws/transcript/' + _activeCallId;
    try {
        _transcriptWs = new WebSocket(wsUrl);
        _transcriptWs.onmessage = function(e) {
            var msg = JSON.parse(e.data);
            if (msg.type === 'transcript') {
                addTranscriptLine(msg.speaker, msg.text, msg.timestamp);
            } else if (msg.type === 'status') {
                document.getElementById('call-status').textContent = msg.status;
                if (msg.status === 'ended' || msg.status === 'error') {
                    endCall();
                    var detail = msg.error ? ': ' + msg.error : '.';
                    showStatus('Call ' + msg.status + detail, msg.status === 'error' ? 'error' : 'success');
                }
            }
        };
        _transcriptWs.onclose = function() {
            if (_activeCallSid) {
                // WS closed but we still think call is active — poll once to check
                setTimeout(function() {
                    if (_activeCallSid) {
                        endCall();
                        showStatus('Call ended (connection closed).', 'success');
                    }
                }, 3000);
            }
        };
    } catch(e) {
        console.error('Transcript WS error:', e);
    }
}

function addTranscriptLine(speaker, text, timestamp) {
    var panel = document.getElementById('transcript-panel');
    // Remove empty state
    if (panel.querySelector('.empty-state')) panel.innerHTML = '';
    var time = timestamp ? new Date(timestamp).toLocaleTimeString('en-NZ', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '';
    var cls = speaker === 'ai' ? 'ai' : 'caller';
    var label = speaker === 'ai' ? '🤖 AI' : '👤 Caller';
    panel.innerHTML += '<div class="transcript-line ' + cls + '"><span class="time">' + time + '</span><span class="speaker">' + label + ':</span>' + esc(text) + '</div>';
    panel.scrollTop = panel.scrollHeight;
}

// ── Mulaw codec (ITU-T G.711) ──
var _MULAW_DECODE_TABLE = (function() {
    // Build mulaw → int16 lookup table
    var t = new Int16Array(256);
    for (var i = 0; i < 256; i++) {
        var v = ~i & 0xFF;
        var sign = v & 0x80;
        var exponent = (v >> 4) & 0x07;
        var mantissa = v & 0x0F;
        var sample = ((mantissa << 3) + 132) << exponent;
        sample -= 132;
        t[i] = sign ? -sample : sample;
    }
    return t;
})();

function _mulawEncode(sample) {
    // int16 PCM → mulaw byte
    var BIAS = 132, CLIP = 32635;
    var sign = 0;
    if (sample < 0) { sign = 0x80; sample = -sample; }
    if (sample > CLIP) sample = CLIP;
    sample += BIAS;
    var exponent = 7;
    for (var expMask = 0x4000; exponent > 0; exponent--, expMask >>= 1) {
        if (sample & expMask) break;
    }
    var mantissa = (sample >> (exponent + 3)) & 0x0F;
    return ~(sign | (exponent << 4) | mantissa) & 0xFF;
}

function _downsample(buffer, fromRate, toRate) {
    // Linear interpolation downsample
    var ratio = fromRate / toRate;
    var outLen = Math.floor(buffer.length / ratio);
    var out = new Float32Array(outLen);
    for (var i = 0; i < outLen; i++) {
        var srcIdx = i * ratio;
        var lo = Math.floor(srcIdx);
        var hi = Math.min(lo + 1, buffer.length - 1);
        var frac = srcIdx - lo;
        out[i] = buffer[lo] * (1 - frac) + buffer[hi] * frac;
    }
    return out;
}

// ── Monitor state ──
var _monitorWs = null;
var _monitorAudioCtx = null;
var _monitorNextTime = 0;

function toggleMonitor() {
    if (!_monitorActive) {
        // Activate
        if (!_activeCallId || !_callServerUrl) return;
        _monitorActive = true;
        var btn = document.getElementById('btn-monitor');
        btn.style.background = '#27ae60';
        btn.innerHTML = '<i class="fa-solid fa-headphones"></i> Listening...';
        document.getElementById('call-status').textContent = 'monitoring';

        _monitorAudioCtx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 8000});
        _monitorNextTime = 0;

        var wsUrl = _callServerUrl.replace('http', 'ws') + '/ws/monitor/' + _activeCallId;
        _monitorWs = new WebSocket(wsUrl);
        _monitorWs.onmessage = function(e) {
            var msg = JSON.parse(e.data);
            if (msg.type !== 'audio' || !msg.payload) return;
            if (!_monitorAudioCtx || _monitorAudioCtx.state === 'closed') return;

            // Decode base64 mulaw → float32 PCM
            var raw = atob(msg.payload);
            var buf = _monitorAudioCtx.createBuffer(1, raw.length, 8000);
            var channel = buf.getChannelData(0);
            for (var i = 0; i < raw.length; i++) {
                channel[i] = _MULAW_DECODE_TABLE[raw.charCodeAt(i) & 0xFF] / 32768.0;
            }

            // Schedule playback
            var src = _monitorAudioCtx.createBufferSource();
            src.buffer = buf;
            src.connect(_monitorAudioCtx.destination);
            var now = _monitorAudioCtx.currentTime;
            if (_monitorNextTime < now) _monitorNextTime = now;
            src.start(_monitorNextTime);
            _monitorNextTime += buf.duration;
        };
        _monitorWs.onclose = function() {
            if (_monitorActive) {
                _monitorActive = false;
                btn.style.background = '#e67e22';
                btn.innerHTML = '<i class="fa-solid fa-headphones"></i> Monitor';
            }
        };
    } else {
        // Deactivate
        _stopMonitor();
    }
}

function _stopMonitor() {
    _monitorActive = false;
    var btn = document.getElementById('btn-monitor');
    btn.style.background = '#e67e22';
    btn.innerHTML = '<i class="fa-solid fa-headphones"></i> Monitor';
    if (_monitorWs) { try { _monitorWs.close(); } catch(e){} _monitorWs = null; }
    if (_monitorAudioCtx) { try { _monitorAudioCtx.close(); } catch(e){} _monitorAudioCtx = null; }
    _monitorNextTime = 0;
}

// ── Barge In state ──
var _bargeWs = null;
var _bargeAudioCtx = null;
var _bargeMicStream = null;
var _bargeProcessor = null;

function toggleBarge() {
    if (!_bargeActive) {
        // Activate
        if (!_activeCallId || !_callServerUrl) return;

        navigator.mediaDevices.getUserMedia({audio: true}).then(function(stream) {
            _bargeActive = true;
            _bargeMicStream = stream;
            var btn = document.getElementById('btn-barge');
            btn.style.background = '#e74c3c';
            btn.innerHTML = '<i class="fa-solid fa-microphone-slash"></i> Release';
            document.getElementById('call-status').textContent = 'BARGED IN — caller hears you';
            document.getElementById('call-status').style.color = '#e74c3c';

            // Auto-activate monitor so operator can hear the caller
            if (!_monitorActive) toggleMonitor();

            // Connect barge WebSocket
            var wsUrl = _callServerUrl.replace('http', 'ws') + '/ws/barge/' + _activeCallId;
            _bargeWs = new WebSocket(wsUrl);

            // Set up mic capture and encoding
            _bargeAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var source = _bargeAudioCtx.createMediaStreamSource(stream);
            // ScriptProcessorNode: 4096 samples buffer, 1 input channel, 1 output channel
            _bargeProcessor = _bargeAudioCtx.createScriptProcessor(4096, 1, 1);

            _bargeProcessor.onaudioprocess = function(e) {
                if (!_bargeWs || _bargeWs.readyState !== WebSocket.OPEN) return;

                var inputData = e.inputBuffer.getChannelData(0);
                // Downsample from mic rate (usually 48kHz) to 8kHz
                var pcm8k = _downsample(inputData, _bargeAudioCtx.sampleRate, 8000);

                // Convert float32 → mulaw bytes
                var mulaw = new Uint8Array(pcm8k.length);
                for (var i = 0; i < pcm8k.length; i++) {
                    var s = Math.max(-1, Math.min(1, pcm8k[i]));
                    var int16 = Math.round(s * 32767);
                    mulaw[i] = _mulawEncode(int16);
                }

                // Base64 encode
                var binary = '';
                for (var i = 0; i < mulaw.length; i++) binary += String.fromCharCode(mulaw[i]);
                var b64 = btoa(binary);

                // Send to server
                _bargeWs.send(JSON.stringify({payload: b64}));
            };

            source.connect(_bargeProcessor);
            _bargeProcessor.connect(_bargeAudioCtx.destination);

            _bargeWs.onclose = function() {
                if (_bargeActive) _stopBarge();
            };

        }).catch(function(err) {
            showStatus('Microphone access required for Barge In: ' + err.message, 'error');
        });
    } else {
        // Deactivate
        _stopBarge();
    }
}

function _stopBarge() {
    _bargeActive = false;
    var btn = document.getElementById('btn-barge');
    btn.style.background = '#8e44ad';
    btn.innerHTML = '<i class="fa-solid fa-microphone"></i> Barge In';
    document.getElementById('call-status').style.color = '#888';
    if (_bargeProcessor) { try { _bargeProcessor.disconnect(); } catch(e){} _bargeProcessor = null; }
    if (_bargeAudioCtx) { try { _bargeAudioCtx.close(); } catch(e){} _bargeAudioCtx = null; }
    if (_bargeMicStream) { _bargeMicStream.getTracks().forEach(function(t){ t.stop(); }); _bargeMicStream = null; }
    if (_bargeWs) { try { _bargeWs.close(); } catch(e){} _bargeWs = null; }
}

// ── Call History ──
function loadHistory() {
    fetch('/api/gemini/call-history').then(r=>r.json()).then(d=>{
        if (!d.ok) { document.getElementById('history-container').innerHTML = '<div class="empty-state">Error loading history.</div>'; return; }
        if (!d.calls.length) { document.getElementById('history-container').innerHTML = '<div class="empty-state">No calls yet.</div>'; return; }
        var html = '<table class="history-table"><thead><tr><th>Date</th><th>To</th><th>Duration</th><th>Status</th><th>Knowledge Base</th><th>User</th><th>Transcript</th><th>Recording</th></tr></thead><tbody>';
        d.calls.forEach(function(c){
            var date = c.started_at ? new Date(c.started_at).toLocaleString('en-NZ', {day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '-';
            var dur = c.duration_seconds ? Math.floor(c.duration_seconds/60) + 'm ' + (c.duration_seconds%60) + 's' : '-';
            var statusCls = c.status === 'completed' ? 'badge-green' : c.status === 'error' ? 'badge-red' : c.status === 'initiated' ? 'badge-blue' : 'badge-grey';
            var transcriptBtn = c.transcript ? '<button class="btn btn-grey" style="font-size:10px;padding:2px 8px;" onclick="showTranscript(\'' + esc(c.call_sid) + '\')"><i class="fa-solid fa-file-lines"></i></button>' : '-';
            var recordingBtns = '-';
            if (c.recording_url) {
                recordingBtns = '<button class="btn" style="font-size:10px;padding:2px 8px;background:#27ae60;" onclick="playRecording(\'' + esc(c.call_sid) + '\', this)"><i class="fa-solid fa-play"></i></button> '
                    + '<a href="/api/gemini/recording/' + esc(c.call_sid) + '" class="btn btn-grey" style="font-size:10px;padding:2px 8px;text-decoration:none;" download><i class="fa-solid fa-download"></i></a>';
            }
            html += '<tr><td>' + date + '</td><td>' + esc(c.to_number||'') + '</td><td>' + dur + '</td>';
            html += '<td><span class="badge ' + statusCls + '">' + esc(c.status||'unknown') + '</span></td>';
            html += '<td>' + (c.knowledge_base_id || '-') + '</td><td>' + esc(c.triggered_by||'') + '</td>';
            html += '<td>' + transcriptBtn + '</td><td>' + recordingBtns + '</td></tr>';
        });
        html += '</tbody></table>';
        document.getElementById('history-container').innerHTML = html;
    });
}

function showTranscript(callSid) {
    fetch('/api/gemini/call/' + callSid).then(r=>r.json()).then(d=>{
        if (!d.ok) { alert('Error loading transcript.'); return; }
        var transcript = d.call.transcript || [];
        var html = '';
        if (typeof transcript === 'string') { try { transcript = JSON.parse(transcript); } catch(e) { transcript = []; } }
        if (Array.isArray(transcript) && transcript.length) {
            transcript.forEach(function(t){
                var cls = t.speaker === 'ai' ? 'ai' : 'caller';
                var label = t.speaker === 'ai' ? '🤖 AI' : '👤 Caller';
                html += '<div class="transcript-line ' + cls + '"><span class="speaker">' + label + ':</span>' + esc(t.text||'') + '</div>';
            });
        } else {
            html = '<div class="empty-state" style="color:#666;">No transcript available.</div>';
        }
        document.getElementById('transcript-modal-content').innerHTML = html;
        document.getElementById('transcript-modal').classList.add('active');
    });
}

// ── Debug ──
function openDebug() {
    document.getElementById('debug-modal').classList.add('active');
    document.getElementById('debug-content').innerHTML = '<div class="empty-state" style="color:#666;">Loading debug info...</div>';
    fetch('/api/gemini/debug').then(r=>r.json()).then(function(d) {
        var html = '';

        // Call Server
        html += _debugSection('Call Server', 'fa-server', function() {
            var h = d.call_server.health || {};
            var s = '<div class="debug-row"><span class="debug-label">Status:</span>' + _debugBadge(h.ok !== undefined ? h.ok : !h.error) + '</div>';
            if (h.active_calls !== undefined) s += '<div class="debug-row"><span class="debug-label">Active calls:</span>' + h.active_calls + '</div>';
            if (h.error) s += '<div class="debug-row"><span class="debug-label">Error:</span><span style="color:#e74c3c;">' + esc(h.error) + '</span></div>';
            // Active calls detail
            var ac = d.call_server.active_calls || {};
            var acKeys = Object.keys(ac);
            if (acKeys.length) {
                s += '<div class="debug-row"><span class="debug-label">Active call IDs:</span>' + acKeys.map(esc).join(', ') + '</div>';
            }
            return s;
        });

        // Gemini
        html += _debugSection('Gemini API', 'fa-robot', function() {
            var g = d.gemini || {};
            var s = '<div class="debug-row"><span class="debug-label">API Key:</span>' + _debugBadge(g.ok) + '</div>';
            if (g.live_model) s += '<div class="debug-row"><span class="debug-label">Live Model:</span>' + esc(g.live_model) + '</div>';
            if (g.text_test) s += '<div class="debug-row"><span class="debug-label">Text test:</span>' + esc(g.text_test) + '</div>';
            if (g.error) s += '<div class="debug-row"><span class="debug-label">Error:</span><span style="color:#e74c3c;">' + esc(g.error) + '</span></div>';
            return s;
        });

        // Twilio
        html += _debugSection('Twilio', 'fa-phone', function() {
            var t = d.twilio || {};
            var s = '<div class="debug-row"><span class="debug-label">Status:</span>' + _debugBadge(t.ok) + '</div>';
            if (t.friendly_name) s += '<div class="debug-row"><span class="debug-label">Account:</span>' + esc(t.friendly_name) + '</div>';
            if (t.status) s += '<div class="debug-row"><span class="debug-label">Account status:</span>' + esc(t.status) + '</div>';
            if (t.phone) s += '<div class="debug-row"><span class="debug-label">Phone:</span>' + esc(t.phone) + '</div>';
            if (t.note) s += '<div class="debug-row"><span class="debug-label">Note:</span>' + esc(t.note) + '</div>';
            if (t.error) s += '<div class="debug-row"><span class="debug-label">Error:</span><span style="color:#e74c3c;">' + esc(t.error) + '</span></div>';
            return s;
        });

        // Supabase
        html += _debugSection('Supabase', 'fa-database', function() {
            var sb = d.supabase || {};
            var s = '<div class="debug-row"><span class="debug-label">Connection:</span>' + _debugBadge(sb.ok) + '</div>';
            if (sb.knowledge_bases) s += '<div class="debug-row"><span class="debug-label">Knowledge Bases table:</span>' + esc(sb.knowledge_bases) + '</div>';
            if (sb.call_history) s += '<div class="debug-row"><span class="debug-label">Call History table:</span>' + esc(sb.call_history) + '</div>';
            if (sb.error) s += '<div class="debug-row"><span class="debug-label">Error:</span><span style="color:#e74c3c;">' + esc(sb.error) + '</span></div>';
            return s;
        });

        // Error Log
        var errors = d.call_server.errors || [];
        html += _debugSection('Recent Errors (' + errors.length + ')', 'fa-triangle-exclamation', function() {
            if (!errors.length) return '<div style="color:#27ae60;font-size:12px;">No recent errors</div>';
            var s = '';
            errors.slice().reverse().forEach(function(e) {
                s += '<div style="font-family:monospace;font-size:11px;padding:4px 0;border-bottom:1px solid #eee;white-space:pre-wrap;word-break:break-all;">' + esc(e) + '</div>';
            });
            return s;
        });

        document.getElementById('debug-content').innerHTML = html;
    }).catch(function(e) {
        document.getElementById('debug-content').innerHTML = '<div style="color:#e74c3c;">Failed to load debug info: ' + esc(String(e)) + '</div>';
    });
}

function _debugSection(title, icon, contentFn) {
    return '<div style="margin-bottom:16px;border:1px solid #dee2e6;border-radius:8px;overflow:hidden;">' +
        '<div style="background:#f8f9fa;padding:8px 12px;font-weight:600;font-size:13px;border-bottom:1px solid #dee2e6;"><i class="fa-solid ' + icon + '" style="margin-right:6px;"></i>' + title + '</div>' +
        '<div style="padding:10px 12px;">' + contentFn() + '</div></div>';
}

function _debugBadge(ok) {
    return ok ? '<span style="background:#27ae60;color:#fff;padding:1px 8px;border-radius:10px;font-size:11px;">OK</span>'
              : '<span style="background:#e74c3c;color:#fff;padding:1px 8px;border-radius:10px;font-size:11px;">ERROR</span>';
}

// ── Recording Playback ──
var _playingAudio = null;

function playRecording(callSid, btn) {
    // If already playing this one, stop it
    if (_playingAudio && btn.dataset.playing === 'true') {
        _playingAudio.pause();
        _playingAudio = null;
        btn.innerHTML = '<i class="fa-solid fa-play"></i>';
        btn.dataset.playing = 'false';
        return;
    }
    // Stop any other playing audio
    if (_playingAudio) {
        _playingAudio.pause();
        _playingAudio = null;
        document.querySelectorAll('[data-playing="true"]').forEach(function(b) {
            b.innerHTML = '<i class="fa-solid fa-play"></i>';
            b.dataset.playing = 'false';
        });
    }
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    _playingAudio = new Audio('/api/gemini/recording/' + callSid);
    _playingAudio.oncanplay = function() {
        btn.innerHTML = '<i class="fa-solid fa-stop"></i>';
        btn.dataset.playing = 'true';
        _playingAudio.play();
    };
    _playingAudio.onended = function() {
        btn.innerHTML = '<i class="fa-solid fa-play"></i>';
        btn.dataset.playing = 'false';
        _playingAudio = null;
    };
    _playingAudio.onerror = function() {
        btn.innerHTML = '<i class="fa-solid fa-play"></i>';
        showStatus('Failed to load recording.', 'error');
        _playingAudio = null;
    };
}

// ── Init ──
loadKnowledgeBases();
loadHistory();
</script>
</body>
</html>
"""
