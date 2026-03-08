import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

STATIC_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f4f4f4; }
        h1 { color: #2c3e50; margin-bottom: 5px; }
        .subtitle { color: #666; margin-bottom: 5px; }
        .updated { color: #999; font-size: 12px; margin-bottom: 25px; }
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
        .notice { background: #eaf4fb; border: 1px solid #aed6f1; padding: 10px 16px; border-radius: 6px;
                  font-size: 13px; color: #2471a3; margin-bottom: 20px; }
    </style>
</head>
<body>
    <h1>PSPLA Security Camera Company Checker</h1>
    <p class="subtitle">NZ companies found installing security cameras — checked against PSPLA licensing register.</p>
    <p class="updated">Last updated: {updated}</p>

    <div class="notice">
        This is a read-only public view. Data is updated daily.
        <strong>Note:</strong> This tool is not official — always verify licensing directly at
        <a href="https://forms.justice.govt.nz/search/PSPLA/" target="_blank">forms.justice.govt.nz</a>.
    </div>

    <div class="stats">
        <div class="stat-box"><h2>{total}</h2><p>Total Companies</p></div>
        <div class="stat-box licensed"><h2>{licensed}</h2><p>PSPLA Licensed</p></div>
        <div class="stat-box unlicensed"><h2>{unlicensed}</h2><p>Not Licensed</p></div>
        <div class="stat-box expired"><h2>{expired}</h2><p>Expired License</p></div>
        <div class="stat-box unknown"><h2>{unknown}</h2><p>Unverified</p></div>
    </div>

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
            {rows}
        </tbody>
    </table>

    <script>
        function filterTable() {{
            const search = document.getElementById('searchBox').value.toLowerCase();
            const region = document.getElementById('regionFilter').value.toLowerCase();
            const status = document.getElementById('statusFilter').value.toLowerCase();
            const rows = document.querySelectorAll('.company-row');
            rows.forEach(row => {{
                const nameMatch = !search || row.dataset.name.includes(search);
                const regionMatch = !region || row.dataset.region.includes(region);
                const statusMatch = !status || row.dataset.status === status;
                const visible = nameMatch && regionMatch && statusMatch;
                row.style.display = visible ? '' : 'none';
                const detailRow = document.getElementById('detail-' + row.dataset.id);
                if (detailRow && !visible) detailRow.style.display = 'none';
            }});
        }}

        function toggleDetail(id) {{
            const row = document.getElementById('detail-' + id);
            const btn = event.target;
            if (row.style.display === 'table-row') {{
                row.style.display = 'none';
                btn.textContent = '▼ more';
            }} else {{
                row.style.display = 'table-row';
                btn.textContent = '▲ less';
            }}
        }}
    </script>
</body>
</html>"""


def get_companies():
    url = f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=company_name.asc"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        response = requests.get(url, headers=headers)
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
            badge = '<span class="badge badge-licensed">LICENSED</span>'
        elif lic == "false" and status_val == "expired":
            status_key = "expired"
            badge = '<span class="badge badge-expired">EXPIRED</span>'
        elif lic == "false" and c.get("individual_license"):
            status_key = "unlicensed"
            badge = '<span class="badge badge-expired">INDIVIDUAL ONLY</span>'
        elif lic == "false":
            status_key = "unlicensed"
            badge = '<span class="badge badge-unlicensed">NOT LICENSED</span>'
        else:
            status_key = "unknown"
            badge = '<span class="badge badge-unknown">UNKNOWN</span>'

        name = escape(c.get("company_name") or "-")
        website = escape(c.get("website") or "")
        company_link = f'<a href="{website}" target="_blank">{name}</a>' if website else name

        phone = escape(c.get("phone") or "")
        email = escape(c.get("email") or "")
        contact = phone
        if email:
            contact += f'<div class="detail-block">{email}</div>'

        pspla_name = escape(c.get("pspla_name") or "-")
        pspla_address = escape(c.get("pspla_address") or "")
        pspla_cell = pspla_name
        if pspla_address:
            pspla_cell += f'<div class="detail-block">{pspla_address}</div>'

        co_name = escape(c.get("companies_office_name") or "-")
        co_address = escape(c.get("companies_office_address") or "")
        co_cell = co_name
        if co_address:
            co_cell += f'<div class="detail-block">{co_address}</div>'

        match_reason = escape(c.get("match_reason") or "")
        reason_block = ""
        if match_reason:
            reason_block = f'''<div style="background:#eaf4fb; border-left:4px solid #2980b9; padding:10px 14px;
                margin-bottom:10px; border-radius:4px; font-size:13px;">
                <strong style="color:#2471a3;">Why this classification?</strong><br>{match_reason}</div>'''

        row = f"""
            <tr class="company-row"
                data-name="{escape((c.get('company_name') or '').lower())}"
                data-region="{escape((c.get('region') or '').lower())}"
                data-status="{status_key}"
                data-id="{i}">
                <td class="company-cell">{company_link}</td>
                <td>{escape(c.get('region') or '-')}</td>
                <td>{contact}</td>
                <td>{badge}</td>
                <td>{pspla_cell}</td>
                <td>{escape(c.get('pspla_license_number') or '-')}</td>
                <td>{escape(c.get('pspla_license_expiry') or '-')}</td>
                <td>{co_cell}</td>
                <td><button class="expand-btn" onclick="toggleDetail({i})">▼ more</button></td>
            </tr>
            <tr class="detail-row" id="detail-{i}">
                <td colspan="9">
                    {reason_block}
                    <div class="detail-grid">
                        <div class="detail-item"><label>Website Address</label><span>{escape(c.get('address') or '-')}</span></div>
                        <div class="detail-item"><label>License Type</label><span>{escape(c.get('license_type') or '-')}</span></div>
                        <div class="detail-item"><label>Directors Found</label><span>{escape(c.get('director_name') or '-')}</span></div>
                        <div class="detail-item"><label>Individual License</label><span>{escape(c.get('individual_license') or '-')}</span></div>
                        <div class="detail-item"><label>Match Method</label><span>{escape(c.get('match_method') or '-')}</span></div>
                        <div class="detail-item"><label>License Status</label><span>{escape(c.get('pspla_license_status') or '-')}</span></div>
                        <div class="detail-item"><label>Last Checked</label><span>{escape((c.get('last_checked') or '')[:10])}</span></div>
                        <div class="detail-item"><label>Found Via</label><span>{escape(c.get('notes') or '-')}</span></div>
                    </div>
                </td>
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

    total = len(companies)
    licensed = sum(1 for c in companies if is_licensed(c))
    expired = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() == "expired")
    unlicensed = sum(1 for c in companies if is_unlicensed(c) and (c.get("pspla_license_status") or "").lower() != "expired")
    unknown = total - licensed - unlicensed - expired

    regions = sorted(set(c.get("region", "") for c in companies if c.get("region")))
    region_options = "\n".join(f'<option value="{r.lower()}">{r}</option>' for r in regions)

    updated = datetime.utcnow().strftime("%d %B %Y %H:%M UTC")
    rows = build_rows(companies)

    html = STATIC_TEMPLATE.format(
        updated=updated,
        total=total,
        licensed=licensed,
        unlicensed=unlicensed,
        expired=expired,
        unknown=unknown,
        region_options=region_options,
        rows=rows
    )

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Generated docs/index.html")


if __name__ == "__main__":
    generate()
