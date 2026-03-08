import os
import csv
import io
import subprocess
import requests
from flask import Flask, render_template_string, redirect, url_for, request, Response
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GITHUB_PAT = os.getenv("GITHUB_PAT")
GITHUB_REPO = os.getenv("GITHUB_REPO", "wadeco2000/pspla-checker")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f4f4f4; }
        h1 { color: #2c3e50; margin-bottom: 5px; }
        .subtitle { color: #666; margin-bottom: 25px; }
        .stats { display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap; }
        .stat-box { background: white; padding: 15px 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); min-width: 130px; text-align: center; }
        .stat-box h2 { margin: 0; font-size: 2em; }
        .stat-box p { margin: 5px 0 0; color: #666; font-size: 13px; }
        .unlicensed h2 { color: #e74c3c; }
        .licensed h2 { color: #27ae60; }
        .expired h2 { color: #e67e22; }
        .unknown h2 { color: #f39c12; }
        .filters { margin-bottom: 15px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .filters select, .filters input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .btn { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .btn-dark { background: #2c3e50; color: white; }
        .btn-dark:hover { background: #34495e; }
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); font-size: 13px; }
        th { background: #2c3e50; color: white; padding: 10px 12px; text-align: left; white-space: nowrap; }
        td { padding: 8px 12px; border-bottom: 1px solid #eee; vertical-align: top; }
        tr:hover td { background: #f9f9f9; }
        .badge { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; white-space: nowrap; }
        .badge-licensed { background: #d4efdf; color: #1e8449; }
        .badge-unlicensed { background: #fadbd8; color: #c0392b; }
        .badge-expired { background: #fdebd0; color: #d35400; }
        .badge-unknown { background: #eaecee; color: #666; }
        a { color: #2980b9; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .detail-block { font-size: 11px; color: #888; margin-top: 2px; }
        .company-cell { font-weight: bold; }
        .expand-btn { background: none; border: none; cursor: pointer; color: #2980b9; font-size: 12px; padding: 0; }
        .detail-row { display: none; }
        .detail-row td { background: #f8f9fa; padding: 12px; }
        .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
        .detail-item label { font-weight: bold; color: #555; font-size: 11px; display: block; margin-bottom: 2px; }
        .detail-item span { font-size: 13px; }
    </style>
</head>
<body>
    <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
            <h1>PSPLA Security Camera Company Checker</h1>
            <p class="subtitle">NZ companies found installing security cameras — checked against PSPLA licensing register.</p>
        </div>
        <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; align-items:center;">
            {% if search_running %}
                <span style="font-size:12px; color:#27ae60; font-weight:bold;">&#9679; Search running</span>
                {% if search_paused %}
                    <form method="POST" action="/resume-search">
                        <button class="btn" style="background:#27ae60; color:white;">&#9654; Resume Search</button>
                    </form>
                {% else %}
                    <form method="POST" action="/pause-search">
                        <button class="btn" style="background:#e67e22; color:white;">&#9646;&#9646; Pause Search</button>
                    </form>
                {% endif %}
            {% else %}
                <form method="POST" action="/start-search" onsubmit="return confirm('Start a full search? This will run in the background and may take a long time.')">
                    <button class="btn" style="background:#27ae60; color:white;">&#9654; Start Search</button>
                </form>
            {% endif %}
            <form method="POST" action="/clear-db" onsubmit="return confirm('Delete ALL entries from the database? This cannot be undone.')">
                <button class="btn" style="background:#e74c3c; color:white;">&#x1F5D1; Clear Database</button>
            </form>
            <form method="POST" action="/publish" onsubmit="return confirm('Publish current data to the live GitHub Pages site?')">
                <button class="btn btn-dark" style="background:#8e44ad;">&#x1F310; Publish Live</button>
            </form>
            <a href="/export.csv" class="btn btn-dark" style="text-decoration:none;">&#x2B07; Export CSV</a>
            <a href="/history" class="btn btn-dark" style="text-decoration:none;">&#x1F4DC; Version History</a>
        </div>
    </div>

    {% if message %}
    <div style="padding:12px 16px; border-radius:6px; margin-bottom:15px;
                background:{{ '#d4efdf' if message_type == 'success' else '#fadbd8' }};
                color:{{ '#1e8449' if message_type == 'success' else '#c0392b' }}; font-size:14px;">
        {{ message }}
    </div>
    {% endif %}

    <div class="stats">
        <div class="stat-box">
            <h2>{{ total }}</h2>
            <p>Total Companies</p>
        </div>
        <div class="stat-box licensed">
            <h2>{{ licensed }}</h2>
            <p>PSPLA Licensed</p>
        </div>
        <div class="stat-box unlicensed">
            <h2>{{ unlicensed }}</h2>
            <p>Not Licensed</p>
        </div>
        <div class="stat-box expired">
            <h2>{{ expired }}</h2>
            <p>Expired License</p>
        </div>
        <div class="stat-box unknown">
            <h2>{{ unknown }}</h2>
            <p>Unverified</p>
        </div>
    </div>

    <div class="filters">
        <input type="text" id="searchBox" placeholder="Search company name..." onkeyup="filterTable()">
        <select id="regionFilter" onchange="filterTable()">
            <option value="">All Regions</option>
            {% for region in regions %}
            <option value="{{ region }}">{{ region }}</option>
            {% endfor %}
        </select>
        <select id="statusFilter" onchange="filterTable()">
            <option value="">All Statuses</option>
            <option value="licensed">Licensed</option>
            <option value="unlicensed">Not Licensed</option>
            <option value="expired">Expired</option>
            <option value="unknown">Unknown</option>
        </select>
        <button class="btn btn-dark" onclick="window.location.reload()">Refresh</button>
    </div>

    <table id="companyTable">
        <thead>
            <tr>
                <th>Company (Website)</th>
                <th>Region</th>
                <th>Contact</th>
                <th>PSPLA Status</th>
                <th>PSPLA Registered Name</th>
                <th>License #</th>
                <th>Expiry</th>
                <th>Companies Office</th>
                <th>Details</th>
            </tr>
        </thead>
        <tbody>
            {% for c in companies %}
            {% set lic = c.pspla_licensed|string|lower %}
            {% set status_key = 'licensed' if lic == 'true' else ('expired' if c.pspla_license_status and c.pspla_license_status|lower == 'expired' else ('unlicensed' if lic == 'false' else 'unknown')) %}
            <tr class="company-row"
                data-name="{{ (c.company_name or '') | lower }}"
                data-region="{{ (c.region or '') | lower }}"
                data-status="{{ status_key }}"
                data-id="{{ loop.index }}">
                <td class="company-cell">
                    {% if c.website %}<a href="{{ c.website }}" target="_blank">{{ c.company_name or '-' }}</a>{% else %}{{ c.company_name or '-' }}{% endif %}
                </td>
                <td>{{ c.region or '-' }}</td>
                <td>
                    {{ c.phone or '' }}
                    {% if c.email %}<div class="detail-block">{{ c.email }}</div>{% endif %}
                </td>
                <td>
                    {% if lic == 'true' %}
                        <span class="badge badge-licensed">LICENSED</span>
                    {% elif c.pspla_license_status and c.pspla_license_status|lower == 'expired' %}
                        <span class="badge badge-expired">EXPIRED</span>
                    {% elif lic == 'false' and c.individual_license %}
                        <span class="badge badge-expired">INDIVIDUAL ONLY</span>
                    {% elif lic == 'false' %}
                        <span class="badge badge-unlicensed">NOT LICENSED</span>
                    {% else %}
                        <span class="badge badge-unknown">UNKNOWN</span>
                    {% endif %}
                </td>
                <td>
                    {{ c.pspla_name or '-' }}
                    {% if c.pspla_address %}<div class="detail-block">{{ c.pspla_address }}</div>{% endif %}
                </td>
                <td>{{ c.pspla_license_number or '-' }}</td>
                <td>{{ c.pspla_license_expiry or '-' }}</td>
                <td>
                    {{ c.companies_office_name or '-' }}
                    {% if c.companies_office_address %}<div class="detail-block">{{ c.companies_office_address }}</div>{% endif %}
                </td>
                <td>
                    <button class="expand-btn" onclick="toggleDetail({{ loop.index }})">▼ more</button>
                </td>
            </tr>
            <tr class="detail-row" id="detail-{{ loop.index }}">
                <td colspan="9">
                    {% if c.match_reason %}
                    <div style="background:#eaf4fb; border-left:4px solid #2980b9; padding:10px 14px; margin-bottom:10px; border-radius:4px; font-size:13px;">
                        <strong style="color:#2471a3;">Why this classification?</strong><br>
                        {{ c.match_reason }}
                    </div>
                    {% endif %}
                    <div class="detail-grid">
                        <div class="detail-item"><label>Website Address</label><span>{{ c.address or '-' }}</span></div>
                        <div class="detail-item"><label>License Type</label><span>{{ c.license_type or '-' }}</span></div>
                        <div class="detail-item"><label>Directors Found</label><span>{{ c.director_name or '-' }}</span></div>
                        <div class="detail-item"><label>Individual License</label><span>{{ c.individual_license or '-' }}</span></div>
                        <div class="detail-item"><label>Match Method</label><span>{{ c.match_method or '-' }}</span></div>
                        <div class="detail-item"><label>License Status</label><span>{{ c.pspla_license_status or '-' }}</span></div>
                        <div class="detail-item"><label>Last Checked</label><span>{{ (c.last_checked or '')[:10] }}</span></div>
                        <div class="detail-item"><label>Found Via</label><span>{{ c.notes or '-' }}</span></div>
                    </div>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <script>
        function filterTable() {
            const search = document.getElementById('searchBox').value.toLowerCase();
            const region = document.getElementById('regionFilter').value.toLowerCase();
            const status = document.getElementById('statusFilter').value.toLowerCase();
            const rows = document.querySelectorAll('.company-row');
            rows.forEach(row => {
                const nameMatch = !search || row.dataset.name.includes(search);
                const regionMatch = !region || row.dataset.region.includes(region);
                const statusMatch = !status || row.dataset.status === status;
                const visible = nameMatch && regionMatch && statusMatch;
                row.style.display = visible ? '' : 'none';
                const detailRow = document.getElementById('detail-' + row.dataset.id);
                if (detailRow && !visible) detailRow.style.display = 'none';
            });
        }

        function toggleDetail(id) {
            const row = document.getElementById('detail-' + id);
            const btn = event.target;
            if (row.style.display === 'table-row') {
                row.style.display = 'none';
                btn.textContent = '▼ more';
            } else {
                row.style.display = 'table-row';
                btn.textContent = '▲ less';
            }
        }
    </script>
</body>
</html>
"""


def get_companies():
    url = f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=company_name.asc"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        if isinstance(data, list):
            return data
        else:
            print(f"Supabase error: {data}")
            return []
    except Exception as e:
        print(f"Error fetching companies: {e}")
        return []


@app.route("/")
def index():
    message = request.args.get("message", "")
    message_type = request.args.get("type", "success")

    companies = get_companies()

    total = len(companies)
    def is_licensed(c):
        v = c.get("pspla_licensed")
        return v is True or v == "true"

    def is_unlicensed(c):
        v = c.get("pspla_licensed")
        return v is False or v == "false"

    licensed = sum(1 for c in companies if is_licensed(c))
    expired = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() == "expired")
    unlicensed = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() != "expired")
    unknown = total - licensed - unlicensed - expired

    regions = sorted(set(c.get("region", "") for c in companies if c.get("region")))

    return render_template_string(
        HTML_TEMPLATE,
        companies=companies,
        total=total,
        licensed=licensed,
        unlicensed=unlicensed,
        expired=expired,
        unknown=unknown,
        regions=regions,
        message=message,
        message_type=message_type,
        search_running=os.path.exists(RUNNING_FLAG),
        search_paused=os.path.exists(PAUSE_FLAG)
    )


@app.route("/debug")
def debug():
    companies = get_companies()
    output = ""
    for c in companies[:5]:
        val = c.get("pspla_licensed")
        output += f"{c.get('company_name')}: pspla_licensed={val!r} type={type(val).__name__}<br>"
    return output


HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Version History</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f4f4f4; }
        h1 { color: #2c3e50; }
        .back { color: #2980b9; text-decoration: none; font-size: 14px; }
        .back:hover { text-decoration: underline; }
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px;
                overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-top: 20px; }
        th { background: #2c3e50; color: white; padding: 10px 14px; text-align: left; }
        td { padding: 10px 14px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: middle; }
        tr:hover td { background: #f9f9f9; }
        .hash { font-family: monospace; color: #888; font-size: 12px; }
        .current { background: #d4efdf !important; font-weight: bold; }
        .btn-rollback { background: #e74c3c; color: white; border: none; padding: 6px 12px;
                        border-radius: 4px; cursor: pointer; font-size: 12px; }
        .btn-rollback:hover { background: #c0392b; }
        .btn-current { background: #27ae60; color: white; border: none; padding: 6px 12px;
                       border-radius: 4px; font-size: 12px; cursor: default; }
        .warning { background: #fff3cd; border: 1px solid #ffc107; padding: 12px 16px;
                   border-radius: 6px; margin-top: 15px; font-size: 13px; color: #856404; }
    </style>
</head>
<body>
    <a href="/" class="back">&larr; Back to Dashboard</a>
    <h1>Version History</h1>
    <div class="warning">
        <strong>Rollback</strong> resets the code to that version. Any uncommitted changes will be lost.
        The database is not affected — only the code changes.
    </div>
    <table>
        <thead>
            <tr><th>Commit</th><th>Date</th><th>Message</th><th>Action</th></tr>
        </thead>
        <tbody>
            {% for commit in commits %}
            <tr {% if loop.first %}class="current"{% endif %}>
                <td class="hash">{{ commit.hash }}</td>
                <td>{{ commit.date }}</td>
                <td>{{ commit.message }}</td>
                <td>
                    {% if loop.first %}
                        <button class="btn-current" disabled>Current</button>
                    {% else %}
                        <form method="POST" action="/rollback/{{ commit.hash }}"
                              onsubmit="return confirm('Roll back to: {{ commit.message }}?')">
                            <button class="btn-rollback" type="submit">Rollback</button>
                        </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""


@app.route("/history")
def history():
    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%h|%ad|%s", "--date=short", "-20"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append({"hash": parts[0], "date": parts[1], "message": parts[2]})
    except Exception as e:
        commits = []
        print(f"Git log error: {e}")
    return render_template_string(HISTORY_TEMPLATE, commits=commits)


@app.route("/rollback/<commit_hash>", methods=["POST"])
def rollback(commit_hash):
    # Safety check: only allow valid short hashes (7 hex chars)
    if not all(c in "0123456789abcdef" for c in commit_hash) or len(commit_hash) != 7:
        return "Invalid commit hash", 400
    try:
        subprocess.run(
            ["git", "reset", "--hard", commit_hash],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
        )
    except Exception as e:
        return f"Rollback error: {e}", 500
    return redirect(url_for("history"))


@app.route("/start-search", methods=["POST"])
def start_search():
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searcher.py")
        subprocess.Popen(
            ["python", script],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        )
        msg = "Search started — a new terminal window has opened showing progress."
        return redirect(url_for("index", message=msg, type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start search: {e}", type="error"))


@app.route("/clear-db", methods=["POST"])
def clear_db():
    try:
        del_url = f"{SUPABASE_URL}/rest/v1/Companies?id=not.is.null"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        response = requests.delete(del_url, headers=headers)
        if response.status_code in [200, 204]:
            msg = "Database cleared — all entries deleted."
            return redirect(url_for("index", message=msg, type="success"))
        else:
            return redirect(url_for("index", message=f"Delete failed: {response.text[:200]}", type="error"))
    except Exception as e:
        return redirect(url_for("index", message=f"Error: {e}", type="error"))


@app.route("/export.csv")
def export_csv():
    companies = get_companies()
    fields = [
        "company_name", "website", "region", "phone", "email", "address",
        "pspla_licensed", "pspla_name", "pspla_address", "pspla_license_number",
        "pspla_license_status", "pspla_license_expiry", "license_type",
        "match_method", "match_reason", "individual_license", "director_name",
        "companies_office_name", "companies_office_address", "last_checked", "notes"
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for c in companies:
        writer.writerow({f: c.get(f, "") or "" for f in fields})
    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pspla_companies.csv"}
    )


@app.route("/publish", methods=["POST"])
def publish():
    if not GITHUB_PAT:
        return redirect(url_for("index", message="GITHUB_PAT not set in .env — cannot trigger publish.", type="error"))
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/publish.yml/dispatches"
        resp = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {GITHUB_PAT}",
                "Accept": "application/vnd.github+json"
            },
            json={"ref": "main"}
        )
        if resp.status_code == 204:
            msg = "Publish triggered — GitHub Pages will update in about 1-2 minutes."
            return redirect(url_for("index", message=msg, type="success"))
        else:
            return redirect(url_for("index", message=f"GitHub API error: {resp.status_code} {resp.text[:200]}", type="error"))
    except Exception as e:
        return redirect(url_for("index", message=f"Publish error: {e}", type="error"))


@app.route("/pause-search", methods=["POST"])
def pause_search():
    open(PAUSE_FLAG, "w").close()
    return redirect(url_for("index", message="Search paused — it will stop after the current company.", type="success"))


@app.route("/resume-search", methods=["POST"])
def resume_search():
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    return redirect(url_for("index", message="Search resumed.", type="success"))


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    print("Also accessible on your local network — find your IP with: ipconfig")
    app.run(host="0.0.0.0", port=5000, debug=False)
