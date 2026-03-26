#!/usr/bin/env python3
"""Monthly Actuate Patriot Numbers report — emailed on the 1st of each month."""

import os
import sys
import json
import smtplib
import requests
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

ACTUATE_API_TOKEN = os.getenv("ACTUATE_API_TOKEN", "")
ACTUATE_BASE_URL = os.getenv("ACTUATE_BASE_URL", "https://admin.actuateui.net")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
RECIPIENTS = ["accounts@alarmwatch.co.nz", "wade@alarmwatch.co.nz"]


def _ts_to_str(ts):
    """Convert Unix timestamp to readable NZ date string."""
    if not ts:
        return "-"
    try:
        from datetime import timezone as _tz
        import zoneinfo
        nz = zoneinfo.ZoneInfo("Pacific/Auckland")
        dt = datetime.fromtimestamp(float(ts), tz=_tz.utc).astimezone(nz)
        return dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.strftime("%d %b %Y, %H:%M UTC")
        except Exception:
            return str(ts)


def fetch_report():
    """Fetch all site data from Actuate API."""
    headers = {"Authorization": f"Token {ACTUATE_API_TOKEN}"}

    # Get all customers
    r = requests.get(f"{ACTUATE_BASE_URL}/api/customer/", headers=headers, timeout=30)
    if not r.ok:
        print(f"  ERROR: Customer API returned {r.status_code}: {r.text[:200]}")
        return []
    try:
        rj = r.json()
    except Exception:
        print(f"  ERROR: Customer API returned non-JSON: {r.text[:200]}")
        return []
    customers = rj.get("results", rj) if isinstance(rj, dict) else rj
    if not isinstance(customers, list):
        print(f"  ERROR: Expected list of customers, got: {type(customers)}")
        return []
    cust_map = {c["id"]: c.get("name", "?") for c in customers}

    # Get all cameras
    r2 = requests.get(f"{ACTUATE_BASE_URL}/api/camera/", headers=headers, timeout=30)
    try:
        r2j = r2.json()
    except Exception:
        print(f"  ERROR: Camera API returned non-JSON: {r2.text[:200]}")
        r2j = []
    cameras = r2j.get("results", r2j) if isinstance(r2j, dict) else r2j
    if not isinstance(cameras, list):
        cameras = []
    site_cameras = {}
    for c in cameras:
        cust = c.get("customer")
        cid = cust.get("id") if isinstance(cust, dict) else cust
        if cid not in site_cameras:
            site_cameras[cid] = []
        site_cameras[cid].append(c["id"])

    # For each site, get about (group, last_alert, deployed_date) + patriot from camera
    results = []
    for sid in cust_map:
        entry = {
            "site_id": sid,
            "site_name": cust_map.get(sid, "?"),
            "group": None,
            "patriot_client_no": None,
            "active": None,
            "armed": None,
            "motion_pct": None,
            "last_alert": None,
            "last_motion": None,
            "deployed_date": None,
            "camera_count": len(site_cameras.get(sid, [])),
            "cameras_active": 0,
            "cameras_inactive": 0,
        }

        # Get group and dates from about endpoint
        try:
            ra = requests.get(f"{ACTUATE_BASE_URL}/api/customer/{sid}/about/", headers=headers, timeout=10)
            if ra.ok:
                about = ra.json()
                pg = about.get("parent_group")
                if pg and isinstance(pg, dict):
                    entry["group"] = pg.get("name", "")
                entry["last_alert"] = about.get("last_alert")
                entry["last_motion"] = about.get("last_motion")
                entry["active"] = about.get("active")
                entry["armed"] = about.get("armed")
                mp = about.get("motion_percentage")
                entry["motion_pct"] = round(float(mp) * 100, 1) if mp is not None else None
                entry["deployed_date"] = about.get("deployed_date")
        except Exception:
            pass

        # Get active/inactive camera counts
        try:
            rc = requests.get(f"{ACTUATE_BASE_URL}/api/camera/site/",
                params={"customer__id": str(sid), "page": "1"}, headers=headers, timeout=10)
            if rc.ok:
                rcj = rc.json()
                site_cams = rcj.get("results", rcj) if isinstance(rcj, dict) else rcj
                if isinstance(site_cams, list):
                    entry["cameras_active"] = sum(1 for c in site_cams if c.get("active"))
                    entry["cameras_inactive"] = sum(1 for c in site_cams if not c.get("active"))
                    entry["camera_count"] = len(site_cams)
        except Exception:
            pass

        # Get patriot from first camera's general_info
        cam_ids = site_cameras.get(sid, [])
        if cam_ids:
            try:
                rg = requests.get(f"{ACTUATE_BASE_URL}/api/camera/{cam_ids[0]}/general_info/", headers=headers, timeout=10)
                if rg.ok:
                    info = rg.json()
                    for stream in info.get("streams", []):
                        for pa in stream.get("patriot_alerts", []):
                            entry["patriot_client_no"] = pa.get("patriot_client_no")
                            break
                        if entry["patriot_client_no"]:
                            break
            except Exception:
                pass

        results.append(entry)
        print(f"  [{len(results)}/{len(cust_map)}] {entry['site_name']}: {entry['patriot_client_no'] or '-'}")

    # Sort by group then site name
    results.sort(key=lambda x: (x.get("group") or "zzz", x.get("site_name") or ""))
    return results


def build_html(results):
    """Build HTML email body."""
    now_nz = _ts_to_str(datetime.now(timezone.utc).timestamp())
    with_patriot = sum(1 for r in results if r.get("patriot_client_no"))

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;margin:0;padding:20px;">
<div style="max-width:900px;margin:0 auto;">
<h2 style="color:#1a252f;margin-bottom:4px;">Actuate List by Bureau</h2>
<p style="color:#888;font-size:12px;margin-top:0;">Generated: {now_nz} — {with_patriot} of {len(results)} sites have Patriot configured</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd;">
<thead>
<tr style="background:#1a252f;color:white;">
<th style="padding:8px;text-align:left;">Group</th>
<th style="padding:8px;text-align:left;">Site ID</th>
<th style="padding:8px;text-align:left;">Site Name</th>
<th style="padding:8px;text-align:center;">Active</th>
<th style="padding:8px;text-align:center;">Armed</th>
<th style="padding:8px;text-align:center;">Cams (A/I)</th>
<th style="padding:8px;text-align:left;">Patriot Client No</th>
<th style="padding:8px;text-align:center;">Motion %</th>
<th style="padding:8px;text-align:left;">Last Alert</th>
<th style="padding:8px;text-align:left;">Last Motion</th>
<th style="padding:8px;text-align:left;">Deployed</th>
</tr>
</thead>
<tbody>"""

    now_ts = datetime.now(timezone.utc).timestamp()
    sixty_days_ago = now_ts - (60 * 86400)
    # "This month or prior month" = anything in the last ~62 days from 1st of last month
    from datetime import date
    today = date.today()
    if today.month == 1:
        recent_cutoff = datetime(today.year - 1, 12, 1, tzinfo=timezone.utc).timestamp()
    else:
        recent_cutoff = datetime(today.year, today.month - 1, 1, tzinfo=timezone.utc).timestamp()

    last_group = ""
    for i, r in enumerate(results):
        group = r.get("group") or "-"
        show_group = group != last_group
        last_group = group
        has_patriot = r.get("patriot_client_no")
        bg = "#f8f8f8" if i % 2 == 0 else "#ffffff"
        border_top = "border-top:2px solid #1a252f;" if show_group else ""
        grey = "color:#ccc;" if not has_patriot else ""

        # Last alert colour: red if older than 60 days
        last_alert_ts = r.get("last_alert")
        alert_style = "font-size:11px;"
        if last_alert_ts and float(last_alert_ts) < sixty_days_ago:
            alert_style += "color:#e74c3c;font-weight:600;"

        # Deployed colour: green if this month or last month
        deployed_ts = r.get("deployed_date")
        deploy_style = "font-size:11px;"
        if deployed_ts and float(deployed_ts) >= recent_cutoff:
            deploy_style += "color:#27ae60;font-weight:600;"

        html += f"""<tr style="background:{bg};{border_top}{grey}">
<td style="padding:6px 8px;font-weight:{'600' if show_group else 'normal'};color:#2980b9;">{group if show_group else ''}</td>
<td style="padding:6px 8px;">{r['site_id']}</td>
<td style="padding:6px 8px;">{r['site_name']}</td>
<td style="padding:6px 8px;text-align:center;">{'<span style="color:#27ae60;">&#x2705;</span>' if r.get('active') else '<span style="color:#e74c3c;">&#x274C;</span>' if r.get('active') is not None else '-'}</td>
<td style="padding:6px 8px;text-align:center;">{'<span style="color:#27ae60;">&#x2705;</span>' if r.get('armed') else '<span style="color:#e74c3c;">&#x274C;</span>' if r.get('armed') is not None else '-'}</td>
<td style="padding:6px 8px;text-align:center;">{r['camera_count']} <span style="font-size:10px;">(<span style="color:#27ae60;">{r.get('cameras_active', 0)}</span>/<span style="color:#e74c3c;">{r.get('cameras_inactive', 0)}</span>)</span></td>
<td style="padding:6px 8px;font-weight:600;{'color:#c0392b;' if has_patriot else ''}">{r.get('patriot_client_no') or '-'}</td>
<td style="padding:6px 8px;text-align:center;font-size:11px;">{str(r.get('motion_pct', '-')) + '%' if r.get('motion_pct') is not None else '-'}</td>
<td style="padding:6px 8px;{alert_style}">{_ts_to_str(last_alert_ts)}</td>
<td style="padding:6px 8px;font-size:11px;{"color:#e74c3c;font-weight:600;" if r.get("last_motion") and float(r["last_motion"]) < sixty_days_ago else ""}">{_ts_to_str(r.get('last_motion'))}</td>
<td style="padding:6px 8px;{deploy_style}">{_ts_to_str(deployed_ts)}</td>
</tr>"""

    html += """</tbody></table>
<p style="font-size:10px;color:#aaa;margin-top:12px;">This report is automatically generated on the 1st of each month by PSPLA Checker.</p>
</div>
</body>
</html>"""
    return html


def send_email(html_body):
    """Send the report email."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Actuate List by Bureau"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText("This email requires an HTML-capable email client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, RECIPIENTS, msg.as_string())

    print(f"  Email sent to: {', '.join(RECIPIENTS)}")


if __name__ == "__main__":
    if not ACTUATE_API_TOKEN:
        print("ERROR: ACTUATE_API_TOKEN not set.")
        sys.exit(1)
    if not SMTP_USER or not SMTP_PASS:
        print("ERROR: SMTP_USER/SMTP_PASS not set.")
        sys.exit(1)

    print("=" * 50)
    print("  Actuate Patriot Numbers Report")
    print("=" * 50)

    print("\n  Fetching site data...")
    results = fetch_report()
    print(f"\n  {len(results)} sites fetched.")

    print("\n  Building email...")
    html = build_html(results)

    print("\n  Sending email...")
    send_email(html)

    print("\n  Done!")
