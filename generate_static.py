import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PAGES_PASSWORD = os.getenv("PAGES_PASSWORD", "")

STATIC_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" referrerpolicy="no-referrer" />
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f4f4f4; }

        /* ── Header ── */
        .page-header {
            background: #2c3e50;
            color: white;
            padding: 14px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.25);
        }
        .page-header h1 { margin: 0; font-size: 18px; font-weight: bold; color: white; }
        .page-header .subtitle { margin: 2px 0 0; font-size: 12px; color: #aac; }
        .header-right { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
        .updated-badge {
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 6px;
            padding: 5px 12px;
            font-size: 12px;
            color: #cde;
            white-space: nowrap;
        }
        .refresh-badge {
            background: rgba(39,174,96,0.2);
            border: 1px solid rgba(39,174,96,0.4);
            border-radius: 6px;
            padding: 5px 12px;
            font-size: 12px;
            color: #7defa7;
            white-space: nowrap;
        }

        /* ── Main content ── */
        .content { padding: 20px 24px; }

        /* ── Notice ── */
        .notice {
            background: #eaf4fb; border: 1px solid #aed6f1;
            padding: 10px 16px; border-radius: 6px;
            font-size: 13px; color: #2471a3; margin-bottom: 20px;
        }

        /* ── Stats ── */
        .stats { display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap; }
        .stat-box {
            background: white; padding: 15px 20px; border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1); min-width: 130px; text-align: center;
        }
        .stat-box h2 { margin: 0; font-size: 2em; }
        .stat-box p { margin: 5px 0 0; color: #666; font-size: 13px; }
        .unlicensed h2 { color: #e74c3c; }
        .licensed h2 { color: #27ae60; }
        .expired h2 { color: #e67e22; }
        .unknown h2 { color: #f39c12; }

        /* ── Filters ── */
        .filters { margin-bottom: 15px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .filters select, .filters input {
            padding: 8px 12px; border: 1px solid #ddd;
            border-radius: 4px; font-size: 14px;
        }

        /* ── Table ── */
        table {
            width: 100%; border-collapse: collapse; background: white;
            border-radius: 8px; overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1); font-size: 13px;
        }
        th { background: #2c3e50; color: white; padding: 10px 12px; text-align: left; white-space: nowrap; }
        td { padding: 8px 12px; border-bottom: 1px solid #eee; vertical-align: top; }
        tr:hover td { background: #f9f9f9; }
        .company-cell { font-weight: bold; }

        /* ── Badges ── */
        .badge { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; white-space: nowrap; }
        .badge-licensed   { background: #d4efdf; color: #1e8449; }
        .badge-unlicensed { background: #fadbd8; color: #c0392b; }
        .badge-expired    { background: #fdebd0; color: #d35400; }
        .badge-unknown    { background: #eaecee; color: #666; }

        .fb-tag  { display:inline-block; background:#1877f2; color:white; border-radius:4px; padding:2px 6px; font-size:11px; font-weight:bold; margin-left:6px; vertical-align:middle; white-space:nowrap; text-decoration:none; }
        .li-tag  { display:inline-block; background:#0a66c2; color:white; border-radius:4px; padding:2px 6px; font-size:11px; font-weight:bold; margin-left:4px; vertical-align:middle; white-space:nowrap; text-decoration:none; }
        .nzsa-tag{ display:inline-block; background:#c0392b; color:white; border-radius:4px; padding:2px 6px; font-size:11px; font-weight:bold; margin-left:4px; vertical-align:middle; white-space:nowrap; }

        /* ── Detail rows ── */
        .expand-btn { background: none; border: none; cursor: pointer; color: #2980b9; font-size: 12px; padding: 0; }
        .detail-row { display: none; }
        .detail-row.open { display: table-row; }
        .detail-row td { background: #f8f9fa; padding: 16px; }
        .detail-block { font-size: 11px; color: #888; margin-top: 2px; }

        /* Card grid inside detail row */
        .card-row { display: grid; gap: 12px; margin-bottom: 12px; }
        .card-row-2 { grid-template-columns: 1fr 1fr; }
        .card-row-4 { grid-template-columns: repeat(4, 1fr); }
        @media(max-width:900px) { .card-row-4 { grid-template-columns: 1fr 1fr; } }
        @media(max-width:600px) { .card-row-2, .card-row-4 { grid-template-columns: 1fr; } }

        .card {
            background: white; border-radius: 8px; padding: 12px 14px;
            border: 1px solid #e0e0e0; font-size: 12px;
        }
        .card-title {
            font-size: 11px; font-weight: bold; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 8px; padding-bottom: 6px;
            border-bottom: 2px solid currentColor;
        }
        .card-row-item { margin-bottom: 4px; }
        .card-row-item label { color: #888; font-size: 10px; display: block; margin-bottom: 1px; }
        .card-row-item span { font-size: 12px; color: #333; }

        .card-pspla  .card-title { color: #1a5276; }
        .card-co     .card-title { color: #555; }
        .card-fb     .card-title { color: #1877f2; }
        .card-nzsa   .card-title { color: #c0392b; }
        .card-google .card-title { color: #e67e22; }
        .card-li     .card-title { color: #0a66c2; }

        .reason-block {
            background: #eaf4fb; border-left: 4px solid #2980b9;
            padding: 10px 14px; border-radius: 4px;
            font-size: 13px; margin-bottom: 12px;
        }
        .reason-block strong { color: #2471a3; }

        .meta-strip {
            display: flex; flex-wrap: wrap; gap: 16px; font-size: 11px;
            color: #888; padding: 8px 4px;
            border-top: 1px solid #eee; border-bottom: 1px solid #eee;
            margin-bottom: 12px;
        }
        .meta-strip strong { color: #555; }
        .svc-tag { padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; color: white; }

        a { color: #2980b9; text-decoration: none; }
        a:hover { text-decoration: underline; }

        /* ── Password overlay ── */
        #password-overlay {
            display: none; position: fixed; top: 0; left: 0;
            width: 100%; height: 100%;
            background: rgba(44,62,80,0.97);
            z-index: 9999; align-items: center; justify-content: center;
        }
        .pw-box {
            background: white; padding: 40px; border-radius: 12px;
            text-align: center; max-width: 360px; width: 90%;
        }
        .pw-box h2 { margin: 0 0 6px; color: #2c3e50; }
        .pw-box p { color: #666; font-size: 14px; margin-bottom: 20px; }
        .pw-box input {
            width: 100%; padding: 10px 14px; border: 1px solid #ddd;
            border-radius: 6px; font-size: 15px; margin-bottom: 12px;
        }
        .pw-box .pw-error { color: #e74c3c; font-size: 13px; margin-bottom: 10px; display: none; }
        .pw-box button {
            width: 100%; padding: 10px; background: #2c3e50; color: white;
            border: none; border-radius: 6px; font-size: 15px; cursor: pointer;
        }
        .pw-box button:hover { background: #34495e; }
    </style>
</head>
<body>

<!-- Password overlay -->
<div id="password-overlay">
    <div class="pw-box">
        <h2>PSPLA Checker</h2>
        <p>Enter the password to access this tool.</p>
        <input type="password" id="pw-input" placeholder="Password"
               onkeydown="if(event.key==='Enter') checkPassword()">
        <div class="pw-error" id="pw-error">Incorrect password. Please try again.</div>
        <button onclick="checkPassword()">Enter</button>
    </div>
</div>

<!-- Header -->
<div class="page-header">
    <div>
        <h1><i class="fa-solid fa-shield-halved"></i> PSPLA Security Camera Checker</h1>
        <p class="subtitle">NZ companies found installing security cameras — checked against PSPLA licensing register.</p>
    </div>
    <div class="header-right">
        <div class="updated-badge"><i class="fa-regular fa-clock"></i> Updated: {updated}</div>
        <div class="refresh-badge" id="refresh-badge"><i class="fa-solid fa-rotate"></i> Refreshing in <span id="countdown">5:00</span></div>
    </div>
</div>

<div class="content">

    <div class="notice">
        This is a read-only public view. Data refreshes automatically every 5 minutes.
        <strong>Note:</strong> This tool is not official — always verify licensing directly at
        <a href="https://forms.justice.govt.nz/search/PSPLA/" target="_blank">forms.justice.govt.nz</a>.
    </div>

    <!-- Stats -->
    <div class="stats">
        <div class="stat-box"><h2>{total}</h2><p>Total Companies</p></div>
        <div class="stat-box licensed"><h2>{licensed}</h2><p>PSPLA Licensed</p></div>
        <div class="stat-box unlicensed"><h2>{unlicensed}</h2><p>Not Licensed</p></div>
        <div class="stat-box expired"><h2>{expired}</h2><p>Expired License</p></div>
        <div class="stat-box unknown"><h2>{unknown}</h2><p>Unverified</p></div>
    </div>

    <!-- Filters -->
    <div class="filters">
        <input type="text" id="searchBox" placeholder="Search company name..." onkeyup="filterTable()">
        <select id="regionFilter" onchange="filterTable()">
            <option value="">All Regions</option>
            {region_options}
        </select>
        <select id="statusFilter" onchange="filterTable()">
            <option value="">All Statuses</option>
            <option value="licensed">Licensed</option>
            <option value="unlicensed">Not Licensed</option>
            <option value="expired">Expired</option>
            <option value="unknown">Unknown</option>
        </select>
        <select id="serviceFilter" onchange="filterTable()">
            <option value="">All Services</option>
            <option value="alarm_systems">Alarm Systems</option>
            <option value="cctv">CCTV / Cameras</option>
            <option value="monitoring">Alarm Monitoring</option>
        </select>
    </div>

    <!-- Table -->
    <table id="companyTable">
        <thead>
            <tr>
                <th><i class="fa-solid fa-building"></i> Company</th>
                <th><i class="fa-solid fa-location-dot"></i> Region</th>
                <th><i class="fa-solid fa-phone"></i> Phone</th>
                <th><i class="fa-solid fa-envelope"></i> Email</th>
                <th><i class="fa-solid fa-certificate"></i> PSPLA Status</th>
                <th><i class="fa-solid fa-id-card"></i> PSPLA Name</th>
                <th>License #</th>
                <th>Expiry</th>
                <th></th>
            </tr>
        </thead>
        <tbody>
            {rows}
        </tbody>
    </table>

</div>

<script>
    // ── Password ──────────────────────────────────────────────────────────────
    const CORRECT_PASSWORD = '{password}';
    const overlay = document.getElementById('password-overlay');

    function checkPassword() {{
        const input = document.getElementById('pw-input').value;
        if (input === CORRECT_PASSWORD) {{
            sessionStorage.setItem('pspla_auth', '1');
            overlay.style.display = 'none';
        }} else {{
            document.getElementById('pw-error').style.display = 'block';
            document.getElementById('pw-input').value = '';
        }}
    }}

    if (CORRECT_PASSWORD && sessionStorage.getItem('pspla_auth') !== '1') {{
        overlay.style.display = 'flex';
        setTimeout(() => document.getElementById('pw-input').focus(), 100);
    }}

    // ── Countdown + auto-reload every 5 minutes ───────────────────────────────
    var secondsLeft = 300;
    function tick() {{
        secondsLeft--;
        if (secondsLeft <= 0) {{ location.reload(); return; }}
        var m = Math.floor(secondsLeft / 60);
        var s = secondsLeft % 60;
        document.getElementById('countdown').textContent = m + ':' + (s < 10 ? '0' : '') + s;
    }}
    setInterval(tick, 1000);

    // ── Filter ────────────────────────────────────────────────────────────────
    function filterTable() {{
        const search  = document.getElementById('searchBox').value.toLowerCase();
        const region  = document.getElementById('regionFilter').value.toLowerCase();
        const status  = document.getElementById('statusFilter').value.toLowerCase();
        const service = document.getElementById('serviceFilter').value;
        document.querySelectorAll('.company-row').forEach(function(row) {{
            var serviceMatch = true;
            if (service === 'alarm_systems') serviceMatch = row.dataset.alarmSystems === 'yes';
            else if (service === 'cctv')     serviceMatch = row.dataset.cctv === 'yes';
            else if (service === 'monitoring') serviceMatch = row.dataset.monitoring === 'yes';
            var visible = (!search  || row.dataset.name.includes(search))
                       && (!region  || row.dataset.region.includes(region))
                       && (!status  || row.dataset.status === status)
                       && serviceMatch;
            row.style.display = visible ? '' : 'none';
            var dr = document.getElementById('detail-' + row.dataset.id);
            if (dr && !visible) dr.classList.remove('open');
        }});
    }}

    // ── Expand/collapse detail row ────────────────────────────────────────────
    function toggleDetail(id) {{
        var row = document.getElementById('detail-' + id);
        var btn = event.target;
        if (row.classList.contains('open')) {{
            row.classList.remove('open');
            btn.innerHTML = '<i class="fa-solid fa-chevron-down"></i> Details';
        }} else {{
            row.classList.add('open');
            btn.innerHTML = '<i class="fa-solid fa-chevron-up"></i> Close';
        }}
    }}

    // ── Copy licence number + open PSPLA ─────────────────────────────────────
    function copyAndOpen(e, licNum) {{
        e.preventDefault();
        navigator.clipboard.writeText(licNum).catch(function(){{}});
        window.open('https://forms.justice.govt.nz/search/PSPLA/', '_blank');
    }}
</script>
</body>
</html>"""


def get_companies():
    url = f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=company_name.asc"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        data = response.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error fetching companies: {e}")
        return []


def escape(val):
    if not val:
        return ""
    return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_rows(companies):
    rows = []
    for i, c in enumerate(companies, 1):
        lic = str(c.get("pspla_licensed", "")).lower()
        status_val = (c.get("pspla_license_status") or "").lower()
        if lic == "true":
            status_key = "licensed"
            badge = '<span class="badge badge-licensed"><i class="fa-solid fa-circle-check"></i> LICENSED</span>'
        elif lic == "false" and status_val == "expired":
            status_key = "expired"
            badge = '<span class="badge badge-expired"><i class="fa-solid fa-clock-rotate-left"></i> EXPIRED</span>'
        elif lic == "false" and c.get("individual_license"):
            status_key = "unlicensed"
            badge = '<span class="badge badge-expired"><i class="fa-solid fa-user"></i> INDIVIDUAL ONLY</span>'
        elif lic == "false":
            status_key = "unlicensed"
            badge = '<span class="badge badge-unlicensed"><i class="fa-solid fa-circle-xmark"></i> NOT LICENSED</span>'
        else:
            status_key = "unknown"
            badge = '<span class="badge badge-unknown"><i class="fa-solid fa-circle-question"></i> UNKNOWN</span>'

        name = escape(c.get("company_name") or "-")
        website = escape(c.get("website") or "")
        company_link = f'<a href="{website}" target="_blank">{name}</a>' if website else name

        # Social tags on company name
        social = ""
        if c.get("facebook_url"):
            social += f'<a href="{escape(c["facebook_url"])}" target="_blank" class="fb-tag"><i class="fa-brands fa-facebook-f"></i></a>'
        if c.get("linkedin_url"):
            social += f'<a href="{escape(c["linkedin_url"])}" target="_blank" class="li-tag"><i class="fa-brands fa-linkedin-in"></i></a>'
        if c.get("nzsa_member") == "true" or c.get("nzsa_member") is True:
            social += '<span class="nzsa-tag">NZSA</span>'

        phone = escape(c.get("phone") or "-")
        email_raw = escape(c.get("email") or "")
        email_cell = f'<a href="mailto:{email_raw}">{email_raw}</a>' if email_raw else "-"

        pspla_name = escape(c.get("pspla_name") or "-")
        pspla_address = escape(c.get("pspla_address") or "")
        pspla_cell = pspla_name
        if pspla_address:
            pspla_cell += f'<div class="detail-block">{pspla_address}</div>'

        # Service tags
        svc_tags = ""
        if c.get("has_alarm_systems") is True or c.get("has_alarm_systems") == "true":
            svc_tags += '<span class="svc-tag" style="background:#1a6e3c;">Alarm Systems</span> '
        if c.get("has_cctv_cameras") is True or c.get("has_cctv_cameras") == "true":
            svc_tags += '<span class="svc-tag" style="background:#1a4b8a;">CCTV / Cameras</span> '
        if c.get("has_alarm_monitoring") is True or c.get("has_alarm_monitoring") == "true":
            svc_tags += '<span class="svc-tag" style="background:#7a3a99;">Alarm Monitoring</span> '

        # Data attributes for service filtering
        alarm_sys = "yes" if (c.get("has_alarm_systems") is True or c.get("has_alarm_systems") == "true") else "no"
        cctv      = "yes" if (c.get("has_cctv_cameras") is True or c.get("has_cctv_cameras") == "true") else "no"
        monitoring= "yes" if (c.get("has_alarm_monitoring") is True or c.get("has_alarm_monitoring") == "true") else "no"

        # Licence number cell
        lic_num = c.get("pspla_license_number") or ""
        if lic_num:
            lic_cell = f'<a href="#" onclick="copyAndOpen(event,\'{escape(lic_num)}\')" title="Copy &amp; open PSPLA register">{escape(lic_num)}</a>'
        else:
            lic_cell = "-"

        expiry = escape(c.get("pspla_license_expiry") or "-")
        region = escape(c.get("region") or "-")

        # ── Detail panel ──────────────────────────────────────────────────────
        match_reason = escape(c.get("match_reason") or "")
        reason_block = ""
        if match_reason:
            reason_block = f'''<div class="reason-block">
                <strong>Why this classification?</strong><br>{match_reason}</div>'''

        # PSPLA card
        pspla_card = f"""<div class="card card-pspla">
            <div class="card-title">PSPLA</div>
            <div class="card-row-item"><label>Name</label><span>{escape(c.get('pspla_name') or '-')}</span></div>
            <div class="card-row-item"><label>Status</label><span>{escape(c.get('pspla_license_status') or '-')}</span></div>
            <div class="card-row-item"><label>Licence #</label><span>{escape(c.get('pspla_license_number') or '-')}</span></div>
            <div class="card-row-item"><label>Expiry</label><span>{escape(c.get('pspla_license_expiry') or '-')}</span></div>
            <div class="card-row-item"><label>Type</label><span>{escape(c.get('license_type') or '-')}</span></div>
            <div class="card-row-item"><label>Address</label><span>{escape(c.get('pspla_address') or '-')}</span></div>
        </div>"""

        co_card = f"""<div class="card card-co">
            <div class="card-title">Companies Office</div>
            <div class="card-row-item"><label>Registered Name</label><span>{escape(c.get('companies_office_name') or '-')}</span></div>
            <div class="card-row-item"><label>Status</label><span>{escape(c.get('co_status') or '-')}</span></div>
            <div class="card-row-item"><label>NZBN</label><span>{escape(c.get('nzbn') or '-')}</span></div>
            <div class="card-row-item"><label>Incorporated</label><span>{escape(c.get('co_incorporated') or '-')}</span></div>
            <div class="card-row-item"><label>Directors</label><span>{escape(c.get('director_name') or '-')}</span></div>
            <div class="card-row-item"><label>Address</label><span>{escape(c.get('companies_office_address') or '-')}</span></div>
        </div>"""

        # Facebook card
        fb_url = escape(c.get("facebook_url") or "")
        fb_link = f'<a href="{fb_url}" target="_blank">{fb_url}</a>' if fb_url else "-"
        fb_card = f"""<div class="card card-fb">
            <div class="card-title"><i class="fa-brands fa-facebook-f"></i> Facebook</div>
            <div class="card-row-item"><label>Page</label><span>{fb_link}</span></div>
            <div class="card-row-item"><label>Followers</label><span>{escape(str(c.get('fb_followers') or '-'))}</span></div>
            <div class="card-row-item"><label>Category</label><span>{escape(c.get('fb_category') or '-')}</span></div>
            <div class="card-row-item"><label>Phone</label><span>{escape(c.get('fb_phone') or '-')}</span></div>
        </div>"""

        # NZSA card
        nzsa_member = c.get("nzsa_member") == "true" or c.get("nzsa_member") is True
        nzsa_card = f"""<div class="card card-nzsa">
            <div class="card-title"><i class="fa-solid fa-shield-halved"></i> NZSA</div>
            <div class="card-row-item"><label>Member</label><span>{'Yes' if nzsa_member else 'No'}</span></div>
            <div class="card-row-item"><label>Name</label><span>{escape(c.get('nzsa_member_name') or '-')}</span></div>
            <div class="card-row-item"><label>Accredited</label><span>{'Yes' if (c.get('nzsa_accredited') == 'true' or c.get('nzsa_accredited') is True) else 'No'}</span></div>
            <div class="card-row-item"><label>Grade</label><span>{escape(c.get('nzsa_grade') or '-')}</span></div>
        </div>"""

        # Google card
        g_rating = c.get("google_rating") or ""
        g_reviews = c.get("google_reviews") or ""
        rating_str = f"&#9733; {escape(str(g_rating))}" if g_rating else "-"
        if g_reviews and str(g_reviews).isdigit():
            rating_str += f" ({escape(str(g_reviews))} reviews)"
        google_card = f"""<div class="card card-google">
            <div class="card-title"><i class="fa-brands fa-google"></i> Google</div>
            <div class="card-row-item"><label>Rating</label><span>{rating_str}</span></div>
            <div class="card-row-item"><label>Phone</label><span>{escape(c.get('google_phone') or '-')}</span></div>
            <div class="card-row-item"><label>Address</label><span>{escape(c.get('google_address') or '-')}</span></div>
        </div>"""

        # LinkedIn card
        li_url = escape(c.get("linkedin_url") or "")
        li_link = f'<a href="{li_url}" target="_blank">{li_url}</a>' if li_url else "-"
        li_card = f"""<div class="card card-li">
            <div class="card-title"><i class="fa-brands fa-linkedin-in"></i> LinkedIn</div>
            <div class="card-row-item"><label>Page</label><span>{li_link}</span></div>
            <div class="card-row-item"><label>Followers</label><span>{escape(str(c.get('linkedin_followers') or '-'))}</span></div>
            <div class="card-row-item"><label>Industry</label><span>{escape(c.get('linkedin_industry') or '-')}</span></div>
            <div class="card-row-item"><label>Size</label><span>{escape(c.get('linkedin_size') or '-')}</span></div>
        </div>"""

        # Meta strip
        meta = f"""<div class="meta-strip">
            {'<span><strong>Website:</strong> <a href="' + website + '" target="_blank">' + website + '</a></span>' if website else ''}
            <span><strong>Found via:</strong> {escape(c.get('notes') or '-')}</span>
            <span><strong>Date added:</strong> {escape((c.get('date_added') or '')[:10]) or '-'}</span>
            <span><strong>Last checked:</strong> {escape((c.get('last_checked') or '')[:10]) or '-'}</span>
            {('<span style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"><strong>Services:</strong>' + svc_tags + '</span>') if svc_tags else ''}
        </div>"""

        detail = f"""<td colspan="9">
            {reason_block}
            <div class="card-row card-row-2">{pspla_card}{co_card}</div>
            <div class="card-row card-row-4">{fb_card}{nzsa_card}{google_card}{li_card}</div>
            {meta}
        </td>"""

        row = f"""
            <tr class="company-row"
                data-name="{escape((c.get('company_name') or '').lower())}"
                data-region="{escape((c.get('region') or '').lower())}"
                data-status="{status_key}"
                data-alarm-systems="{alarm_sys}"
                data-cctv="{cctv}"
                data-monitoring="{monitoring}"
                data-id="{i}">
                <td class="company-cell">{company_link}{social}</td>
                <td>{region}</td>
                <td>{phone}</td>
                <td>{email_cell}</td>
                <td>{badge}</td>
                <td>{pspla_cell}</td>
                <td>{lic_cell}</td>
                <td>{expiry}</td>
                <td><button class="expand-btn" onclick="toggleDetail({i})"><i class="fa-solid fa-chevron-down"></i> Details</button></td>
            </tr>
            <tr class="detail-row" id="detail-{i}">
                {detail}
            </tr>"""
        rows.append(row)
    return "\n".join(rows)


def generate():
    print("Fetching companies from Supabase...")
    companies = get_companies()
    print(f"Got {len(companies)} companies.")

    def is_licensed(c):
        v = c.get("pspla_licensed")
        return v is True or v == "true"

    def is_unlicensed(c):
        v = c.get("pspla_licensed")
        return v is False or v == "false"

    total      = len(companies)
    licensed   = sum(1 for c in companies if is_licensed(c))
    expired    = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() == "expired")
    unlicensed = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() != "expired")
    unknown    = total - licensed - unlicensed - expired

    regions = sorted(set(c.get("region", "") for c in companies if c.get("region")))
    region_options = "\n".join(f'<option value="{r.lower()}">{r}</option>' for r in regions)

    updated = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
    rows = build_rows(companies)

    html = STATIC_TEMPLATE
    html = html.replace("{updated}", updated)
    html = html.replace("{total}", str(total))
    html = html.replace("{licensed}", str(licensed))
    html = html.replace("{unlicensed}", str(unlicensed))
    html = html.replace("{expired}", str(expired))
    html = html.replace("{unknown}", str(unknown))
    html = html.replace("{region_options}", region_options)
    html = html.replace("{rows}", rows)
    html = html.replace("{password}", PAGES_PASSWORD)

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated docs/index.html ({len(companies)} companies, {licensed} licensed)")


if __name__ == "__main__":
    generate()
