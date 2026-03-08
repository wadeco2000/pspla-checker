import os
import requests
from flask import Flask, render_template_string
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f4f4f4; }
        h1 { color: #2c3e50; }
        .stats { display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }
        .stat-box { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); min-width: 150px; text-align: center; }
        .stat-box h2 { margin: 0; font-size: 2em; }
        .stat-box p { margin: 5px 0 0; color: #666; }
        .unlicensed h2 { color: #e74c3c; }
        .licensed h2 { color: #27ae60; }
        .unknown h2 { color: #f39c12; }
        .filters { margin-bottom: 20px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .filters select, .filters input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        th { background: #2c3e50; color: white; padding: 12px; text-align: left; }
        td { padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 13px; }
        tr:hover { background: #f9f9f9; }
        .badge { padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .badge-licensed { background: #d4efdf; color: #1e8449; }
        .badge-unlicensed { background: #fadbd8; color: #c0392b; }
        .badge-unknown { background: #fdebd0; color: #d35400; }
        a { color: #2980b9; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .refresh { background: #2c3e50; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .refresh:hover { background: #34495e; }
    </style>
</head>
<body>
    <h1>PSPLA Security Camera Company Checker</h1>
    <p style="color:#666">NZ companies found installing security cameras, checked against PSPLA licensing register.</p>

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
        <div class="stat-box unknown">
            <h2>{{ unknown }}</h2>
            <p>Unknown / Unverified</p>
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
            <option value="unknown">Unknown</option>
        </select>
        <button class="refresh" onclick="window.location.reload()">Refresh</button>
    </div>

    <table id="companyTable">
        <thead>
            <tr>
                <th>Company Name</th>
                <th>Region</th>
                <th>Phone</th>
                <th>Website</th>
                <th>PSPLA Status</th>
                <th>PSPLA Name</th>
                <th>License Type</th>
                <th>Match Method</th>
                <th>Last Checked</th>
            </tr>
        </thead>
        <tbody>
            {% for c in companies %}
            <tr class="company-row"
                data-name="{{ (c.company_name or '') | lower }}"
                data-region="{{ (c.region or '') | lower }}"
                data-status="{{ 'licensed' if c.pspla_licensed == true else 'unlicensed' if c.pspla_licensed == false else 'unknown' }}">
                <td>{{ c.company_name or '-' }}</td>
                <td>{{ c.region or '-' }}</td>
                <td>{{ c.phone or '-' }}</td>
                <td>{% if c.website %}<a href="{{ c.website }}" target="_blank">Visit</a>{% else %}-{% endif %}</td>
                <td>
                    {% if c.pspla_licensed == true %}
                        <span class="badge badge-licensed">LICENSED</span>
                    {% elif c.pspla_licensed == false and c.pspla_license_status %}
                        <span class="badge badge-unlicensed">{{ c.pspla_license_status | upper }}</span>
                    {% elif c.pspla_licensed == false %}
                        <span class="badge badge-unlicensed">NOT LICENSED</span>
                    {% else %}
                        <span class="badge badge-unknown">UNKNOWN</span>
                    {% endif %}
                </td>
                <td>{{ c.pspla_name or '-' }}</td>
                <td>{{ c.license_type or '-' }}</td>
                <td>{{ c.match_method or '-' }}</td>
                <td>{{ (c.last_checked or '')[:10] }}</td>
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
                row.style.display = (nameMatch && regionMatch && statusMatch) ? '' : 'none';
            });
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
        print(f"Supabase status: {response.status_code}")
        print(f"Supabase response: {response.text[:300]}")
        data = response.json()
        if isinstance(data, list):
            return data
        else:
            print(f"Unexpected response format: {data}")
            return []
    except Exception as e:
        print(f"Error fetching companies: {e}")
        return []


@app.route("/")
def index():
    companies = get_companies()

    total = len(companies)
    licensed = sum(1 for c in companies if c.get("pspla_licensed") is True)
    unlicensed = sum(1 for c in companies if c.get("pspla_licensed") is False)
    unknown = total - licensed - unlicensed

    regions = sorted(set(c.get("region", "") for c in companies if c.get("region")))

    return render_template_string(
        HTML_TEMPLATE,
        companies=companies,
        total=total,
        licensed=licensed,
        unlicensed=unlicensed,
        unknown=unknown,
        regions=regions
    )


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(debug=False)
