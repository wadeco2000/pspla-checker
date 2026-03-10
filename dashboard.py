import os
import csv
import io
import json
import sys
import threading
import time as _time
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
from flask import Flask, render_template_string, redirect, url_for, request, Response
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
SERPAPI_KEY  = os.getenv("SERPAPI_KEY")
GITHUB_PAT = os.getenv("GITHUB_PAT")
EXPORT_PASSWORD = os.getenv("EXPORT_PASSWORD") or os.getenv("PAGES_PASSWORD", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "wadeco2000/pspla-checker")
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
STATUS_FILE = os.path.join(BASE_DIR, "search_status.json")
HISTORY_FILE = os.path.join(BASE_DIR, "search_history.json")
SCHEDULE_FLAG = os.path.join(BASE_DIR, "schedule_enabled.flag")
TERMS_FILE = os.path.join(BASE_DIR, "search_terms.json")
PARTIAL_CONFIG_FILE = os.path.join(BASE_DIR, "partial_config.json")
RECHECK_CONFIG_FILE = os.path.join(BASE_DIR, "recheck_config.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "search_progress.json")
PID_FILE = os.path.join(BASE_DIR, "search_pid.txt")
LOG_FILE = os.path.join(BASE_DIR, "search_log.txt")
START_FILE = os.path.join(BASE_DIR, "search_start.json")
BACKUP_LOG_FILE = os.path.join(BASE_DIR, "backup_log.txt")
RECHECK_LOG_FILE = os.path.join(BASE_DIR, "recheck_log.txt")

# ── Recheck log capture ────────────────────────────────────────────────────────
# Tees sys.stdout to recheck_log.txt during individual recheck endpoint calls
# so all print() output from searcher.py is visible in the dashboard terminal.
_recheck_log_lock = threading.Lock()

class _TeeWriter:
    """Write to both the real stdout and an open file handle."""
    def __init__(self, real, f):
        self._real = real
        self._f = f
    def write(self, data):
        if self._real is not None:
            try:
                self._real.write(data)
            except Exception:
                pass
        try:
            self._f.write(data)
            self._f.flush()
        except Exception:
            pass
    def flush(self):
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass
        try:
            self._f.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._real, name)

@contextmanager
def _recheck_log_capture():
    """Context manager: redirect stdout → recheck_log.txt for the duration."""
    with _recheck_log_lock:
        try:
            f = open(RECHECK_LOG_FILE, "w", encoding="utf-8", buffering=1)
            old = sys.stdout
            sys.stdout = _TeeWriter(old, f)
            yield
        finally:
            sys.stdout = old
            try:
                f.close()
            except Exception:
                pass

NZ_REGIONS = [
    # Major cities
    "Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga",
    "Dunedin", "Palmerston North", "Napier", "New Plymouth", "Whangarei",
    "Nelson", "Invercargill", "Gisborne", "Whanganui", "Rotorua",
    "Hastings", "Blenheim", "Timaru", "Pukekohe", "Taupo",
    # Auckland suburbs / districts
    "North Shore", "Henderson", "Manukau", "Papakura",
    "Howick", "Onehunga", "Manurewa", "Botany",
    "Pakuranga", "Waitakere", "Orewa", "Silverdale",
    "Takapuna", "Albany", "Glenfield", "Kumeu",
    # Northland
    "Kerikeri", "Kaitaia", "Dargaville",
    # Wellington region
    "Lower Hutt", "Upper Hutt", "Porirua", "Paraparaumu",
    # Waikato
    "Thames", "Te Awamutu", "Tokoroa",
    # Bay of Plenty
    "Whakatane", "Katikati", "Te Puke",
    # Tauranga suburbs
    "Mount Maunganui", "Papamoa",
    # Hawke's Bay
    "Waipukurau", "Wairoa",
    # Taranaki
    "Hawera", "Stratford",
    # Manawatu
    "Levin", "Feilding",
    # Tasman/Nelson
    "Motueka", "Richmond",
    # Marlborough
    "Picton",
    # West Coast
    "Greymouth", "Westport",
    # Canterbury / Christchurch suburbs
    "Rangiora", "Ashburton", "Rolleston", "Hornby", "Papanui",
    # Otago
    "Queenstown", "Wanaka", "Oamaru", "Alexandra",
    # Southland
    "Gore",
]


_DEFAULT_TERMS = {
    "google": [
        "security camera installer", "CCTV installer", "IP camera installation",
        "security camera installation company", "CCTV installation company",
        "security alarm installation", "alarm system installer",
        "IT security camera install", "network camera installation",
        "surveillance camera installation", "security system installer",
        "intruder alarm installer", "CCTV security alarm",
        "electrical security camera installation", "smart home security camera",
    ],
    "facebook": [
        "security camera installation", "CCTV installation",
        "security camera installer", "security alarm installation",
        "CCTV installer", "security camera company",
    ],
}


def _load_terms():
    try:
        if os.path.exists(TERMS_FILE):
            with open(TERMS_FILE) as f:
                data = json.load(f)
            # Return defaults for any missing key
            return {
                "google": data.get("google") or _DEFAULT_TERMS["google"],
                "facebook": data.get("facebook") or _DEFAULT_TERMS["facebook"],
            }
    except Exception:
        pass
    # Write defaults so the file exists for next time
    try:
        with open(TERMS_FILE, "w") as f:
            json.dump(_DEFAULT_TERMS, f, indent=2)
    except Exception:
        pass
    return dict(_DEFAULT_TERMS)

app = Flask(__name__)

# On startup, clear any stale flag files left over from a previous session that
# was killed mid-search.  If the app just started, no search can be running.
for _stale in [RUNNING_FLAG, PAUSE_FLAG, PID_FILE]:
    try:
        os.remove(_stale)
    except FileNotFoundError:
        pass

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" referrerpolicy="no-referrer" />
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f4f4f4; }
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
        .detail-row.open { display: table-row; }
        .detail-row td { background: #f8f9fa; padding: 12px; }
        .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
        .detail-item label { font-weight: bold; color: #555; font-size: 11px; display: block; margin-bottom: 2px; }
        .detail-item span { font-size: 13px; }
        .fb-tag { display:inline-block; background:#1877f2; color:white; border-radius:4px;
                  padding:2px 6px; font-size:11px; font-weight:bold; margin-left:6px;
                  vertical-align:middle; white-space:nowrap; text-decoration:none; }
        .li-tag { display:inline-block; background:#0a66c2; color:white; border-radius:4px;
                  padding:2px 6px; font-size:11px; font-weight:bold; margin-left:4px;
                  vertical-align:middle; white-space:nowrap; text-decoration:none; }
        .nzsa-logo { display:block; cursor:default; text-align:center; }
        .status-icon { margin-right:4px; }
        /* ── Navbar ── */
        .navbar { position:sticky; top:0; z-index:1000; background:linear-gradient(135deg,#1a2535 0%,#2c3e50 100%);
                  border-bottom:1px solid #3d5166; box-shadow:0 2px 16px rgba(0,0,0,0.35);
                  display:flex; align-items:stretch; padding:0 16px; height:54px; gap:0; }
        .navbar-brand { display:flex; align-items:center; gap:10px; padding-right:18px;
                        border-right:1px solid #3d5166; margin-right:4px; min-width:fit-content; text-decoration:none; }
        .brand-logo { height:30px; width:auto; }
        .brand-title { font-size:14px; font-weight:700; color:white; letter-spacing:0.3px; line-height:1.2; }
        .brand-sub { font-size:10px; color:#7f95b0; display:block; }
        .nav-menus { display:flex; align-items:stretch; flex:1; }
        .nav-item { position:relative; display:flex; align-items:center; }
        .nav-btn { background:none; border:none; color:#bdc3c7; font-size:13px; font-weight:500;
                   padding:0 15px; height:100%; cursor:pointer; display:flex; align-items:center;
                   gap:6px; transition:background 0.15s,color 0.15s; white-space:nowrap; }
        .nav-btn:hover, .nav-item.open .nav-btn { background:rgba(255,255,255,0.09); color:white; }
        .nav-chevron { font-size:9px; transition:transform 0.2s; opacity:0.6; }
        .nav-item.open .nav-chevron { transform:rotate(180deg); }
        .dropdown { display:none; position:absolute; top:100%; left:0; background:#1a2535;
                    border:1px solid #3d5166; border-top:none; border-radius:0 0 8px 8px;
                    box-shadow:0 10px 28px rgba(0,0,0,0.45); min-width:250px; z-index:1001;
                    padding:6px 0; animation:dropIn 0.15s ease; }
        .nav-item.open .dropdown { display:block; }
        @keyframes dropIn { from{opacity:0;transform:translateY(-6px)} to{opacity:1;transform:translateY(0)} }
        .dd-item { display:flex; align-items:center; gap:10px; padding:9px 16px; color:#bdc3c7;
                   font-size:13px; cursor:pointer; text-decoration:none; white-space:nowrap;
                   transition:background 0.1s,color 0.1s; border:none; background:none; width:100%; text-align:left; }
        .dd-item:hover { background:rgba(255,255,255,0.08); color:white; text-decoration:none; }
        .dd-item .dd-icon { width:18px; text-align:center; opacity:0.75; flex-shrink:0; }
        .dd-item .dd-sub { font-size:10px; color:#7f95b0; display:block; margin-top:1px; }
        .dd-item.danger { color:#e74c3c; }
        .dd-item.danger:hover { background:rgba(231,76,60,0.12); color:#ff6b6b; }
        .dd-item.highlight { color:#27ae60; }
        .dd-item.highlight:hover { background:rgba(39,174,96,0.1); color:#2ecc71; }
        .dd-fresh { margin-left:auto; font-size:10px; padding:2px 7px; border-radius:3px;
                    background:#d68910; color:white; border:none; cursor:pointer; white-space:nowrap; }
        .dd-fresh:hover { background:#b7770d; }
        .dd-divider { height:1px; background:#3d5166; margin:5px 0; }
        .dd-label { padding:6px 16px 3px; font-size:10px; color:#7f95b0; text-transform:uppercase;
                    letter-spacing:0.8px; font-weight:600; }
        .navbar-right { display:flex; align-items:center; gap:10px; padding-left:14px;
                        border-left:1px solid #3d5166; margin-left:4px; }
        .credits-bar { display:flex; flex-direction:column; gap:2px; font-size:11px; }
        .credits-bar span { color:#7f95b0; white-space:nowrap; line-height:1.3; }
        .credits-bar b { color:#bdc3c7; }
        .version-tag { font-size:10px; color:#4a6278; white-space:nowrap; font-family:monospace; margin-top:1px; }
        .running-pill { display:flex; align-items:center; gap:7px; background:rgba(39,174,96,0.12);
                        border:1px solid rgba(39,174,96,0.3); border-radius:20px; padding:3px 10px 3px 7px; }
        .pulse-dot { width:7px; height:7px; border-radius:50%; background:#27ae60; flex-shrink:0;
                     animation:pulse-glow 1.5s infinite; }
        @keyframes pulse-glow { 0%{box-shadow:0 0 0 0 rgba(39,174,96,0.5)}
                                 70%{box-shadow:0 0 0 7px rgba(39,174,96,0)}
                                 100%{box-shadow:0 0 0 0 rgba(39,174,96,0)} }
        .nav-action-btn { padding:4px 11px; border:none; border-radius:4px; cursor:pointer;
                          font-size:12px; font-weight:600; display:flex; align-items:center; gap:5px; }
        /* ── Page content wrapper ── */
        .page-content { padding:20px; }
        /* ── Panels ── */
        .panel-wrap { margin-bottom:16px; }
    </style>
</head>
<body>
<nav class="navbar">
  <!-- Brand -->
  <a class="navbar-brand" href="/">
    <img class="brand-logo" src="data:image/png;base64,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/GMzLdsAADsjaVRYdFhNTDpjb20uYWRvYmUueG1wAAAAAAA8P3hwYWNrZXQgYmVnaW49Iu+7vyIgaWQ9Ilc1TTBNcENlaGlIenJlU3pOVGN6a2M5ZCI/Pgo8eDp4bXBtZXRhIHhtbG5zOng9ImFkb2JlOm5zOm1ldGEvIiB4OnhtcHRrPSJBZG9iZSBYTVAgQ29yZSA1LjYtYzAxNCA3OS4xNTY3OTcsIDIwMTQvMDgvMjAtMDk6NTM6MDIgICAgICAgICI+CiAgIDxyZGY6UkRGIHhtbG5zOnJkZj0iaHR0cDovL3d3dy53My5vcmcvMTk5OS8wMi8yMi1yZGYtc3ludGF4LW5zIyI+CiAgICAgIDxyZGY6RGVzY3JpcHRpb24gcmRmOmFib3V0PSIiCiAgICAgICAgICAgIHhtbG5zOnhtcD0iaHR0cDovL25zLmFkb2JlLmNvbS94YXAvMS4wLyIKICAgICAgICAgICAgeG1sbnM6ZGM9Imh0dHA6Ly9wdXJsLm9yZy9kYy9lbGVtZW50cy8xLjEvIgogICAgICAgICAgICB4bWxuczpwaG90b3Nob3A9Imh0dHA6Ly9ucy5hZG9iZS5jb20vcGhvdG9zaG9wLzEuMC8iCiAgICAgICAgICAgIHhtbG5zOnhtcE1NPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvbW0vIgogICAgICAgICAgICB4bWxuczpzdEV2dD0iaHR0cDovL25zLmFkb2JlLmNvbS94YXAvMS4wL3NUeXBlL1Jlc291cmNlRXZlbnQjIgogICAgICAgICAgICB4bWxuczp0aWZmPSJodHRwOi8vbnMuYWRvYmUuY29tL3RpZmYvMS4wLyIKICAgICAgICAgICAgeG1sbnM6ZXhpZj0iaHR0cDovL25zLmFkb2JlLmNvbS9leGlmLzEuMC8iPgogICAgICAgICA8eG1wOkNyZWF0b3JUb29sPkFkb2JlIFBob3Rvc2hvcCBDQyAyMDE0IChNYWNpbnRvc2gpPC94bXA6Q3JlYXRvclRvb2w+CiAgICAgICAgIDx4bXA6Q3JlYXRlRGF0ZT4yMDE0LTEyLTAzVDE2OjE4OjU0KzEzOjAwPC94bXA6Q3JlYXRlRGF0ZT4KICAgICAgICAgPHhtcDpNb2RpZnlEYXRlPjIwMTQtMTItMDNUMTY6Mzk6NDIrMTM6MDA8L3htcDpNb2RpZnlEYXRlPgogICAgICAgICA8eG1wOk1ldGFkYXRhRGF0ZT4yMDE0LTEyLTAzVDE2OjM5OjQyKzEzOjAwPC94bXA6TWV0YWRhdGFEYXRlPgogICAgICAgICA8ZGM6Zm9ybWF0PmltYWdlL3BuZzwvZGM6Zm9ybWF0PgogICAgICAgICA8cGhvdG9zaG9wOkNvbG9yTW9kZT4zPC9waG90b3Nob3A6Q29sb3JNb2RlPgogICAgICAgICA8cGhvdG9zaG9wOklDQ1Byb2ZpbGU+c1JHQiBJRUM2MTk2Ni0yLjE8L3Bob3Rvc2hvcDpJQ0NQcm9maWxlPgogICAgICAgICA8eG1wTU06SW5zdGFuY2VJRD54bXAuaWlkOjMyOWMzNjI1LTZmMzYtNDlhYi04YWViLWRjMTA3MDI5NzJjODwveG1wTU06SW5zdGFuY2VJRD4KICAgICAgICAgPHhtcE1NOkRvY3VtZW50SUQ+YWRvYmU6ZG9jaWQ6cGhvdG9zaG9wOjY4NjhiZWI2LWJiMjktMTE3Ny1iOGZjLWVmNjRiOTQxNjM5NTwveG1wTU06RG9jdW1lbnRJRD4KICAgICAgICAgPHhtcE1NOk9yaWdpbmFsRG9jdW1lbnRJRD54bXAuZGlkOjhhNjUwNjQxLTQyYzMtNDkzMy05MTQ3LTQ1NWEyNWY0NzJiYjwveG1wTU06T3JpZ2luYWxEb2N1bWVudElEPgogICAgICAgICA8eG1wTU06SGlzdG9yeT4KICAgICAgICAgICAgPHJkZjpTZXE+CiAgICAgICAgICAgICAgIDxyZGY6bGkgcmRmOnBhcnNlVHlwZT0iUmVzb3VyY2UiPgogICAgICAgICAgICAgICAgICA8c3RFdnQ6YWN0aW9uPmNyZWF0ZWQ8L3N0RXZ0OmFjdGlvbj4KICAgICAgICAgICAgICAgICAgPHN0RXZ0Omluc3RhbmNlSUQ+eG1wLmlpZDo4YTY1MDY0MS00MmMzLTQ5MzMtOTE0Ny00NTVhMjVmNDcyYmI8L3N0RXZ0Omluc3RhbmNlSUQ+CiAgICAgICAgICAgICAgICAgIDxzdEV2dDp3aGVuPjIwMTQtMTItMDNUMTY6MTg6NTQrMTM6MDA8L3N0RXZ0OndoZW4+CiAgICAgICAgICAgICAgICAgIDxzdEV2dDpzb2Z0d2FyZUFnZW50PkFkb2JlIFBob3Rvc2hvcCBDQyAyMDE0IChNYWNpbnRvc2gpPC9zdEV2dDpzb2Z0d2FyZUFnZW50PgogICAgICAgICAgICAgICA8L3JkZjpsaT4KICAgICAgICAgICAgICAgPHJkZjpsaSByZGY6cGFyc2VUeXBlPSJSZXNvdXJjZSI+CiAgICAgICAgICAgICAgICAgIDxzdEV2dDphY3Rpb24+Y29udmVydGVkPC9zdEV2dDphY3Rpb24+CiAgICAgICAgICAgICAgICAgIDxzdEV2dDpwYXJhbWV0ZXJzPmZyb20gYXBwbGljYXRpb24vdm5kLmFkb2JlLnBob3Rvc2hvcCB0byBpbWFnZS9wbmc8L3N0RXZ0OnBhcmFtZXRlcnM+CiAgICAgICAgICAgICAgIDwvcmRmOmxpPgogICAgICAgICAgICAgICA8cmRmOmxpIHJkZjpwYXJzZVR5cGU9IlJlc291cmNlIj4KICAgICAgICAgICAgICAgICAgPHN0RXZ0OmFjdGlvbj5zYXZlZDwvc3RFdnQ6YWN0aW9uPgogICAgICAgICAgICAgICAgICA8c3RFdnQ6aW5zdGFuY2VJRD54bXAuaWlkOjMyOWMzNjI1LTZmMzYtNDlhYi04YWViLWRjMTA3MDI5NzJjODwvc3RFdnQ6aW5zdGFuY2VJRD4KICAgICAgICAgICAgICAgICAgPHN0RXZ0OndoZW4+MjAxNC0xMi0wM1QxNjozOTo0MisxMzowMDwvc3RFdnQ6d2hlbj4KICAgICAgICAgICAgICAgICAgPHN0RXZ0OnNvZnR3YXJlQWdlbnQ+QWRvYmUgUGhvdG9zaG9wIENDIDIwMTQgKE1hY2ludG9zaCk8L3N0RXZ0OnNvZnR3YXJlQWdlbnQ+CiAgICAgICAgICAgICAgICAgIDxzdEV2dDpjaGFuZ2VkPi88L3N0RXZ0OmNoYW5nZWQ+CiAgICAgICAgICAgICAgIDwvcmRmOmxpPgogICAgICAgICAgICA8L3JkZjpTZXE+CiAgICAgICAgIDwveG1wTU06SGlzdG9yeT4KICAgICAgICAgPHRpZmY6T3JpZW50YXRpb24+MTwvdGlmZjpPcmllbnRhdGlvbj4KICAgICAgICAgPHRpZmY6WFJlc29sdXRpb24+NzIwMDAwLzEwMDAwPC90aWZmOlhSZXNvbHV0aW9uPgogICAgICAgICA8dGlmZjpZUmVzb2x1dGlvbj43MjAwMDAvMTAwMDA8L3RpZmY6WVJlc29sdXRpb24+CiAgICAgICAgIDx0aWZmOlJlc29sdXRpb25Vbml0PjI8L3RpZmY6UmVzb2x1dGlvblVuaXQ+CiAgICAgICAgIDxleGlmOkNvbG9yU3BhY2U+MTwvZXhpZjpDb2xvclNwYWNlPgogICAgICAgICA8ZXhpZjpQaXhlbFhEaW1lbnNpb24+MTIwPC9leGlmOlBpeGVsWERpbWVuc2lvbj4KICAgICAgICAgPGV4aWY6UGl4ZWxZRGltZW5zaW9uPjY0PC9leGlmOlBpeGVsWURpbWVuc2lvbj4KICAgICAgPC9yZGY6RGVzY3JpcHRpb24+CiAgIDwvcmRmOlJERj4KPC94OnhtcG1ldGE+CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAogICAgICAgICAgICAgICAgICAgICAgICAgICAgCjw/eHBhY2tldCBlbmQ9InciPz4aStXKAAAAIGNIUk0AAHolAACAgwAA+f8AAIDpAAB1MAAA6mAAADqYAAAXb5JfxUYAABv5SURBVHja7H13WJRX2v59pjEzSJuhCoIKSBFp0kUsRMWWGNNsiTGa7MZN27R1NyZ+KW562cRkk7jZaIwaE6NGsNNEitJROiiolKEMZWBmmHq+P3DGwaAO8wK/fNf1O1xz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" alt="Alarm Watch" style="height:36px;width:auto;">
    <div>
      <span class="brand-title">PSPLA Checker</span>
      <span class="brand-sub">by Alarm Watch</span>
    </div>
  </a>

  <!-- Menus -->
  <div class="nav-menus">

    <!-- Searches -->
    <div class="nav-item" id="menu-searches">
      <button class="nav-btn" onclick="toggleMenu('menu-searches')">
        <i class="fa-solid fa-magnifying-glass"></i> Searches
        <i class="fa-solid fa-chevron-down nav-chevron"></i>
      </button>
      <div class="dropdown">
        <div class="dd-label">Run a search</div>

        <form method="POST" action="/start-search" onsubmit="return searchFormCheck(this, 'Full Search')">
          <button type="submit" class="dd-item highlight">
            <i class="fa-solid fa-play dd-icon"></i>
            <span>Full Search<span class="dd-sub">All regions × all terms + Facebook pass</span></span>
          </button>
        </form>

        <form method="POST" action="/start-weekly-search" onsubmit="return searchFormCheck(this, 'Weekly Scan')">
          <button type="submit" class="dd-item">
            <i class="fa-solid fa-calendar-week dd-icon" style="color:#16a085;"></i>
            <span>Weekly Scan<span class="dd-sub">Light scan — recent changes only</span></span>
          </button>
        </form>

        <div class="dd-divider"></div>

        <!-- Facebook Search row with Fresh -->
        <div style="display:flex; align-items:center;">
          <form method="POST" action="/start-facebook-search" onsubmit="return searchFormCheck(this, 'Facebook Search')" style="flex:1;">
            <button type="submit" class="dd-item">
              <i class="fa-brands fa-facebook-f dd-icon" style="color:#1877f2;"></i>
              <span>Facebook Search<small id="fb-progress-badge" style="display:none;color:#e67e22;margin-left:6px;font-size:10px;"></small><span class="dd-sub">Search FB for NZ security companies</span></span>
            </button>
          </form>
          <form method="POST" action="/start-facebook-search" id="fb-fresh-form" style="display:none;" onsubmit="return searchFormCheck(this, 'Facebook Search (Fresh)');">
            <input type="hidden" name="fresh" value="1">
            <button type="submit" class="dd-fresh" title="Start fresh — clear saved progress">&#8635; Fresh</button>
          </form>
        </div>

        <!-- Directory Import row with Fresh -->
        <div style="display:flex; align-items:center;">
          <form method="POST" action="/start-directory-import" onsubmit="return searchFormCheck(this, 'Directory Import')" style="flex:1;">
            <button type="submit" class="dd-item">
              <i class="fa-solid fa-address-book dd-icon" style="color:#c0392b;"></i>
              <span>Directory Import<small id="dir-progress-badge" style="display:none;color:#e67e22;margin-left:6px;font-size:10px;"></small><span class="dd-sub">NZSA + LinkedIn member lists</span></span>
            </button>
          </form>
          <form method="POST" action="/start-directory-import" id="dir-fresh-form" style="display:none;" onsubmit="return searchFormCheck(this, 'Directory Import (Fresh)');">
            <input type="hidden" name="fresh" value="1">
            <button type="submit" class="dd-fresh" title="Start fresh — clear saved progress">&#8635; Fresh</button>
          </form>
        </div>

        <div class="dd-divider"></div>

        <button type="button" class="dd-item" onclick="togglePanel('panel-partial'); closeMenus();">
          <i class="fa-solid fa-crosshairs dd-icon" style="color:#8e44ad;"></i>
          <span>Partial Search<span class="dd-sub">Target specific regions or terms</span></span>
        </button>

        <button type="button" class="dd-item" onclick="togglePanel('panel-bulk'); closeMenus();">
          <i class="fa-solid fa-rotate dd-icon" style="color:#e67e22;"></i>
          <span>Bulk Recheck<span class="dd-sub">Re-run checks on existing companies</span></span>
        </button>

        <button type="button" class="dd-item" onclick="togglePanel('panel-terms'); closeMenus();">
          <i class="fa-solid fa-list-check dd-icon" style="color:#7f8c8d;"></i>
          <span>Search Terms<span class="dd-sub">Edit Google / Facebook search terms</span></span>
        </button>

        <button type="button" class="dd-item" onclick="togglePanel('panel-schedule'); closeMenus();">
          <i class="fa-solid fa-calendar-days dd-icon" style="color:{{ '#27ae60' if schedule_enabled else '#95a5a6' }};"></i>
          <span>Scheduled Searches
            <span class="dd-sub">Auto-runs — currently <strong style="color:{{ '#27ae60' if schedule_enabled else '#e74c3c' }};">{{ 'Enabled' if schedule_enabled else 'Disabled' }}</strong></span>
          </span>
        </button>
      </div>
    </div>

    <!-- Database -->
    <div class="nav-item" id="menu-database">
      <button class="nav-btn" onclick="toggleMenu('menu-database')">
        <i class="fa-solid fa-database"></i> Database
        <i class="fa-solid fa-chevron-down nav-chevron"></i>
      </button>
      <div class="dropdown">
        <div class="dd-label">Data management</div>

        <form method="POST" action="/dedupe-db" onsubmit="return confirm('Find and merge duplicate companies?\n\nGroups by: matching name, same domain, or same Facebook URL.\nKeeps the record with the most data.\nAll data from duplicates is merged in — nothing is lost.\nAn audit entry is written for every merge.')">
          <button type="submit" class="dd-item">
            <i class="fa-solid fa-filter dd-icon" style="color:#8e44ad;"></i>
            <span>Dedupe DB<span class="dd-sub">Auto-merge duplicates (name / domain / FB URL)</span></span>
          </button>
        </form>

        <a href="/review-duplicates" class="dd-item">
          <i class="fa-solid fa-eye dd-icon" style="color:#e67e22;"></i>
          <span>Review Near-Matches<span class="dd-sub">Manually review possible duplicates</span></span>
        </a>

        <a href="/suspect-records" class="dd-item">
          <i class="fa-solid fa-triangle-exclamation dd-icon" style="color:#e74c3c;"></i>
          <span>Suspect Records<span class="dd-sub">Review low-quality or possibly wrong entries</span></span>
        </a>

        <div class="dd-divider"></div>

        <form method="POST" action="/publish" onsubmit="return confirm('Publish current data to the live GitHub Pages site?')">
          <button type="submit" class="dd-item">
            <i class="fa-solid fa-globe dd-icon" style="color:#8e44ad;"></i>
            <span>Publish Live<span class="dd-sub">Push to GitHub Pages public site</span></span>
          </button>
        </form>

        <a href="https://wadeco2000.github.io/pspla-checker/" target="_blank" class="dd-item">
          <i class="fa-solid fa-arrow-up-right-from-square dd-icon" style="color:#2980b9;"></i>
          <span>View Live Site<span class="dd-sub">Open public GitHub Pages site</span></span>
        </a>

        <button type="button" class="dd-item" onclick="document.getElementById('export-modal').style.display='flex'; closeMenus();">
          <i class="fa-solid fa-file-csv dd-icon" style="color:#27ae60;"></i>
          <span>Export CSV<span class="dd-sub">Download all companies as CSV</span></span>
        </button>

        <form method="POST" action="/backup-db">
          <button type="submit" class="dd-item">
            <i class="fa-solid fa-cloud-arrow-up dd-icon" style="color:#8e44ad;"></i>
            <span>Backup Now<span class="dd-sub">Save CSV to local folder + Dropbox</span></span>
          </button>
        </form>

        <div class="dd-divider"></div>

        <button type="button" class="dd-item danger" onclick="document.getElementById('clear-db-modal').style.display='flex'; closeMenus();">
          <i class="fa-solid fa-trash-can dd-icon"></i>
          <span>Clear DB<span class="dd-sub">Delete all company records</span></span>
        </button>
      </div>
    </div>

    <!-- Logs & History -->
    <div class="nav-item" id="menu-logs">
      <button class="nav-btn" onclick="toggleMenu('menu-logs')">
        <i class="fa-solid fa-clock-rotate-left"></i> History &amp; Logs
        <i class="fa-solid fa-chevron-down nav-chevron"></i>
      </button>
      <div class="dropdown">
        <div class="dd-label">Activity &amp; decisions</div>
        <a href="/search-history" class="dd-item">
          <i class="fa-solid fa-rectangle-list dd-icon" style="color:#3498db;"></i>
          <span>Search History<span class="dd-sub">All search runs and outcomes</span></span>
        </a>
        <a href="/audit-log" class="dd-item">
          <i class="fa-solid fa-book dd-icon" style="color:#6c3483;"></i>
          <span>Audit Log<span class="dd-sub">Every database change and AI decision</span></span>
        </a>
        <a href="/llm-log" class="dd-item">
          <i class="fa-solid fa-robot dd-icon" style="color:#27ae60;"></i>
          <span>LLM Log<span class="dd-sub">Every AI prompt and response</span></span>
        </a>
        <div class="dd-divider"></div>
        <a href="/history" class="dd-item">
          <i class="fa-solid fa-code-branch dd-icon" style="color:#7f8c8d;"></i>
          <span>Version History<span class="dd-sub">Git commits — rollback if needed</span></span>
        </a>
        <a href="/duplicates" class="dd-item">
          <i class="fa-solid fa-triangle-exclamation dd-icon" style="color:#e74c3c;"></i>
          <span>Duplicates Report<span class="dd-sub">Companies flagged as possible duplicates</span></span>
        </a>
      </div>
    </div>

  </div><!-- /nav-menus -->

  <!-- Right: credits + running state -->
  <div class="navbar-right">
    <div class="credits-bar" id="api-credits-bar">
      <span id="credit-serp"><i class="fa-solid fa-magnifying-glass"></i> SerpAPI: <b>loading…</b></span>
      <span id="credit-tokens"><i class="fa-solid fa-robot"></i> Claude: <b>–</b></span>
      <span class="version-tag"><i class="fa-solid fa-code-branch"></i> {{ git_version }}</span>
    </div>

    <span id="btns-running" style="display:{{ 'contents' if search_running else 'none' }};">
      <div class="running-pill">
        <div class="pulse-dot"></div>
        <span style="font-size:12px; color:#2ecc71; font-weight:600; white-space:nowrap;">Running</span>
      </div>
      <form method="POST" action="/resume-search" id="btn-resume" style="display:{{ 'inline' if search_paused else 'none' }}; margin:0;">
        <button class="nav-action-btn" style="background:#27ae60; color:white;"><i class="fa-solid fa-play"></i> Resume</button>
      </form>
      <form method="POST" action="/pause-search" id="btn-pause" style="display:{{ 'none' if search_paused else 'inline' }}; margin:0;">
        <button class="nav-action-btn" style="background:#e67e22; color:white;"><i class="fa-solid fa-pause"></i> Pause</button>
      </form>
      <form method="POST" action="/stop-search" onsubmit="return confirm('Stop the running search?')" style="margin:0;">
        <button class="nav-action-btn" style="background:#c0392b; color:white;"><i class="fa-solid fa-stop"></i> Stop</button>
      </form>
    </span>
    <span id="btns-idle" style="display:none;"></span>
  </div>
</nav>
<div class="page-content">

    {% set _s = init_status %}
    {% set _pct = ((_s.region_idx - 1) / _s.total_regions * 100) | round | int if _s.region_idx and _s.total_regions else 0 %}
    {% set _phase = 'Facebook' if _s.phase == 'facebook' else 'Google' %}
    {% set _paused_txt = ' — PAUSED' if search_paused else '' %}
    {% set _label = _phase ~ ' search: region ' ~ _s.region_idx ~ ' of ' ~ _s.total_regions ~ ' — ' ~ (_s.region or '') ~ _paused_txt if _s.region_idx else _phase ~ ' search starting...' ~ _paused_txt %}
    {% set _term_txt = 'Term ' ~ _s.term_idx ~ ' of ' ~ _s.total_terms ~ ': ' ~ (_s.term or '') if _s.term_idx else '' %}
    {% set _bar_color = '#1877f2' if _s.phase == 'facebook' else '#27ae60' %}
    <div id="llm-warning-banner" style="display:none; margin-top:10px; background:#fff3cd; border:1px solid #ffc107;
         border-radius:6px; padding:8px 14px; font-size:12px; color:#856404;"></div>

    <div id="progress-wrap" style="display:{{ 'block' if search_running else 'none' }}; margin-top:14px; background:white; border-radius:8px;
         box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <span id="progress-label" style="font-size:13px; font-weight:bold; color:#2c3e50;">{{ _label if search_running else '' }}</span>
            <span id="progress-counts" style="font-size:12px; color:#888;">{{ (_s.total_found or 0)|string ~ ' found, ' ~ (_s.total_new or 0)|string ~ ' new' if search_running else '' }}</span>
        </div>
        <div style="background:#ecf0f1; border-radius:4px; height:10px; overflow:hidden;">
            <div id="progress-bar" style="height:100%; background:{{ _bar_color }}; border-radius:4px;
                 transition:width 0.4s ease; width:{{ _pct }}%;"></div>
        </div>
        <div id="progress-term" style="font-size:12px; color:#888; margin-top:5px;">{{ _term_txt if search_running else '' }}</div>
        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px;">
            <span style="font-size:11px; color:#aaa;"><i class="fa-solid fa-terminal"></i> Search log</span>
            <div style="display:flex; gap:8px; align-items:center;">
                <button onclick="openTerminal()"
                    style="background:none; border:1px solid #555; color:#ccc; font-size:11px; cursor:pointer; padding:2px 8px; border-radius:4px;">
                    Open Terminal
                </button>
                <button onclick="toggleLog()" id="log-toggle-btn"
                    style="background:none; border:none; color:#2980b9; font-size:11px; cursor:pointer; padding:0;">
                    Hide log
                </button>
            </div>
        </div>
        <div id="log-panel" style="margin-top:4px;">
            <pre id="log-output"
                style="background:#1e1e1e; color:#d4d4d4; font-size:11px; font-family:monospace;
                       padding:10px 14px; border-radius:6px; max-height:260px; overflow-y:auto;
                       margin:0; white-space:pre-wrap; word-break:break-all;">{{ init_log_lines | join('\n') | e if init_log_lines else '(no log yet)' }}</pre>
        </div>
    </div>

    <script>
    // ── Navbar dropdown logic ──────────────────────────────────────────
    function toggleMenu(id) {
        var el = document.getElementById(id);
        var wasOpen = el.classList.contains('open');
        closeMenus();
        if (!wasOpen) el.classList.add('open');
    }
    function closeMenus() {
        document.querySelectorAll('.nav-item.open').forEach(function(el) {
            el.classList.remove('open');
        });
    }
    // Close dropdowns when clicking outside
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.nav-item')) closeMenus();
    });

    // ── Panel toggle ───────────────────────────────────────────────────
    function togglePanel(id) {
        var el = document.getElementById(id);
        if (!el) return;
        var opening = el.style.display === 'none';
        // Close all panels first
        ['panel-terms','panel-partial','panel-bulk','panel-schedule'].forEach(function(pid) {
            var p = document.getElementById(pid);
            if (p) p.style.display = 'none';
        });
        if (opening) {
            el.style.display = '';
            el.scrollIntoView({ behavior:'smooth', block:'nearest' });
        }
    }

    // Log panel state — must be declared before poll() runs
    var _logOpen = true;
    var _lastLogLine = -1;

    function toggleLog() {
        _logOpen = !_logOpen;
        document.getElementById('log-panel').style.display = _logOpen ? '' : 'none';
        document.getElementById('log-toggle-btn').textContent = _logOpen ? 'Hide log' : 'Show log';
    }

    function openTerminal() {
        fetch('/open-terminal', {method:'POST'}).catch(function() {});
    }

    // Standalone log updater — runs every 2s independently
    setInterval(function() {
        var el = document.getElementById('log-output');
        if (!el || !_logOpen) return;
        fetch('/search-log')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var lines = d.lines || [];
                if (lines.length !== _lastLogLine) {
                    _lastLogLine = lines.length;
                    el.textContent = lines.length ? lines.join('\\n') : '(no output yet)';
                    el.scrollTop = el.scrollHeight;
                }
            })
            .catch(function(e) {
                var el2 = document.getElementById('log-output');
                if (el2) el2.textContent = '[log fetch error: ' + e + ']';
            });
    }, 2000);

    (function() {
        var wrap = document.getElementById('progress-wrap');
        var label = document.getElementById('progress-label');
        var bar = document.getElementById('progress-bar');
        var counts = document.getElementById('progress-counts');
        var termEl = document.getElementById('progress-term');

        var btnsRunning = document.getElementById('btns-running');
        var btnsIdle = document.getElementById('btns-idle');
        var btnPause = document.getElementById('btn-pause');
        var btnResume = document.getElementById('btn-resume');

        function poll() {
            fetch('/search-status')
                .then(function(r) { return r.json(); })
                .then(function(s) {
                    if (!s.running) {
                        wrap.style.display = 'none';
                        if (btnsRunning) btnsRunning.style.display = 'none';
                        if (btnsIdle) btnsIdle.style.display = 'contents';
                        return;
                    }
                    wrap.style.display = 'block';
                    if (btnsRunning) btnsRunning.style.display = 'contents';
                    if (btnsIdle) btnsIdle.style.display = 'none';
                    if (btnPause) btnPause.style.display = s.paused ? 'none' : 'inline';
                    if (btnResume) btnResume.style.display = s.paused ? 'inline' : 'none';
                    var phase = s.phase === 'facebook' ? 'Facebook' : 'Google';
                    var paused = s.paused ? ' - PAUSED' : '';
                    if (s.region_idx != null && s.total_regions != null) {
                        label.textContent = phase + ' search: region ' + s.region_idx + ' of ' + s.total_regions + ' - ' + (s.region || '') + paused;
                        var pct = Math.round((s.region_idx - 1) / s.total_regions * 100);
                        bar.style.width = pct + '%';
                        termEl.textContent = 'Term ' + s.term_idx + ' of ' + s.total_terms + ': ' + (s.term || '');
                    } else {
                        label.textContent = phase + ' search starting...' + paused;
                        bar.style.width = '0%';
                        termEl.textContent = '';
                    }
                    bar.style.background = s.phase === 'facebook' ? '#1877f2' : '#27ae60';
                    counts.textContent = (s.total_found || 0) + ' found, ' + (s.total_new || 0) + ' new';
                    // LLM warning banner
                    var llmBanner = document.getElementById('llm-warning-banner');
                    if (llmBanner) {
                        if (s.llm_warning) {
                            llmBanner.textContent = '⚠ ' + s.llm_warning;
                            llmBanner.style.display = 'block';
                        } else {
                            llmBanner.style.display = 'none';
                        }
                    }
                    // Update token counter from live status
                    updateTokenWidget(s.tokens);
                })
                .catch(function() {});
        }
        poll();
        setInterval(poll, 3000);
    })();

    // ── Search progress badges ────────────────────────────────────────────────
    function loadSearchProgress() {
        fetch('/search-progress')
            .then(function(r) { return r.json(); })
            .then(function(p) {
                // Facebook progress badge
                var fbBadge = document.getElementById('fb-progress-badge');
                var fbFresh = document.getElementById('fb-fresh-form');
                if (fbBadge && p.facebook) {
                    var done = p.facebook.done;
                    var total = p.facebook.total;
                    var nwDone = p.facebook.nationwide_done ? ' + NW' : '';
                    fbBadge.textContent = done + '/' + (total - 1) + ' regions' + nwDone + ' saved';
                    fbBadge.style.display = 'inline';
                    if (fbFresh) fbFresh.style.display = 'inline';
                } else if (fbBadge) {
                    fbBadge.style.display = 'none';
                    if (fbFresh) fbFresh.style.display = 'none';
                }
                // Directory progress badge
                var dirBadge = document.getElementById('dir-progress-badge');
                var dirFresh = document.getElementById('dir-fresh-form');
                if (dirBadge && p.directory) {
                    var d = p.directory;
                    var parts = [];
                    if (d.nzsa_done) parts.push('NZSA done');
                    else if (d.nzsa_last_idx >= 0) parts.push('NZSA @' + (d.nzsa_last_idx + 1));
                    if (d.linkedin_done) parts.push('LinkedIn done');
                    else if (d.linkedin_queries_done > 0) parts.push('LI ' + d.linkedin_queries_done + '/' + d.linkedin_total + ' queries');
                    dirBadge.textContent = parts.join(', ') + ' saved';
                    dirBadge.style.display = 'inline';
                    if (dirFresh) dirFresh.style.display = 'inline';
                } else if (dirBadge) {
                    dirBadge.style.display = 'none';
                    if (dirFresh) dirFresh.style.display = 'none';
                }
                // Partial progress badge
                var partialBadge = document.getElementById('partial-progress-badge');
                var partialFreshBtn = document.getElementById('partial-fresh-btn');
                if (partialBadge && p.partial) {
                    var pp = p.partial;
                    var pparts = [];
                    if (pp.google_done) pparts.push('Google done');
                    else if (pp.completed_regions > 0) pparts.push(pp.completed_regions + ' regions done');
                    if (pp.fb_done) pparts.push('FB done');
                    partialBadge.textContent = pparts.join(', ') + ' saved';
                    partialBadge.style.display = 'inline';
                    if (partialFreshBtn) partialFreshBtn.style.display = 'inline';
                } else if (partialBadge) {
                    partialBadge.style.display = 'none';
                    if (partialFreshBtn) partialFreshBtn.style.display = 'none';
                }
            })
            .catch(function() {});
    }
    loadSearchProgress();
    setInterval(loadSearchProgress, 15000);

    // ── API Credits widget ───────────────────────────────────────────────────
    function updateTokenWidget(tokens) {
        var el = document.getElementById('credit-tokens');
        if (!el || !tokens) return;
        var inp = (tokens.input || 0).toLocaleString();
        var out = (tokens.output || 0).toLocaleString();
        var cost = tokens.estimated_cost_usd != null ? ' (~$' + tokens.estimated_cost_usd.toFixed(3) + ' USD)' : '';
        var total = (tokens.input || 0) + (tokens.output || 0);
        if (total === 0) {
            el.innerHTML = '<i class="fa-solid fa-robot"></i> Claude: <span style="font-weight:bold;color:#aaa;">no usage this session</span>';
        } else {
            el.innerHTML = '<i class="fa-solid fa-robot"></i> Claude: <span style="font-weight:bold;color:#a29bfe;">'
                + total.toLocaleString() + ' tokens' + cost + '</span>'
                + ' <span style="color:#888;font-size:11px;">(' + inp + ' in / ' + out + ' out)</span>';
        }
    }

    function loadApiCredits() {
        fetch('/api-credits')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                // SerpAPI
                var serpEl = document.getElementById('credit-serp');
                if (serpEl) {
                    if (d.serp_error) {
                        serpEl.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> SerpAPI: <span style="color:#e74c3c;font-weight:bold;">' + d.serp_error + '</span>';
                    } else {
                        var left  = d.serp_searches_left != null ? d.serp_searches_left.toLocaleString() : '?';
                        var month = d.serp_searches_month != null ? d.serp_searches_month.toLocaleString() : '?';
                        var used  = d.serp_this_month != null ? d.serp_this_month.toLocaleString() : '?';
                        var pct   = d.serp_searches_month ? Math.round((1 - d.serp_searches_left / d.serp_searches_month) * 100) : null;
                        var color = d.serp_searches_left < 100 ? '#e74c3c' : d.serp_searches_left < 500 ? '#e67e22' : '#2ecc71';
                        serpEl.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> SerpAPI: <span style="font-weight:bold;color:' + color + ';">'
                            + left + ' searches left</span>'
                            + ' <span style="color:#888;font-size:11px;">(' + used + ' used of ' + month + ' this month'
                            + (pct != null ? ', ' + pct + '%' : '') + ')</span>';
                    }
                }
                // Claude tokens
                updateTokenWidget(d.tokens);
            })
            .catch(function() {
                var serpEl = document.getElementById('credit-serp');
                if (serpEl) serpEl.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> SerpAPI: <span style="color:#888;">unavailable</span>';
            });
    }
    loadApiCredits();
    // Refresh SerpAPI balance every 5 minutes (token count updates via poll())
    setInterval(loadApiCredits, 300000);

    </script>

    {% if message %}
    <div id="flash-msg" style="padding:12px 16px; border-radius:6px; margin-bottom:15px;
                background:{{ '#d4efdf' if message_type == 'success' else '#fadbd8' }};
                color:{{ '#1e8449' if message_type == 'success' else '#c0392b' }}; font-size:14px;
                transition: opacity 0.5s ease;">
        {{ message }}
    </div>
    <script>
        setTimeout(function() {
            var el = document.getElementById('flash-msg');
            if (el) { el.style.opacity = '0'; setTimeout(function(){ el.style.display='none'; }, 500); }
        }, 5000);
    </script>
    {% endif %}

    <script>
    var _terms = {google: [], facebook: []};
    var _activeTab = 'google';
    function renderTermsList(type) {
        var el = document.getElementById('terms-list-' + type);
        if (!el) return;
        if (!_terms[type].length) {
            el.innerHTML = '<span style="color:#aaa;">No terms yet.</span>';
            return;
        }
        var html = '';
        for (var i = 0; i < _terms[type].length; i++) {
            html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 0;border-bottom:1px solid #f5f5f5;">'
                  + '<span>' + _terms[type][i] + '</span>'
                  + '<button onclick="removeTerm(\\'' + type + '\\',' + i + ')" style="background:none;border:none;color:#e74c3c;cursor:pointer;font-size:13px;padding:0 4px;" title="Remove">x</button>'
                  + '</div>';
        }
        el.innerHTML = html;
    }
    function renderPartialTerms() {
        var el = document.getElementById('partial-term-list');
        if (!el) return;
        var html = '';
        for (var i = 0; i < _terms.google.length; i++) {
            html += '<label style="display:block;padding:1px 2px;cursor:pointer;">'
                  + '<input type="checkbox" class="partial-term-cb" value="' + _terms.google[i] + '" checked style="margin-right:4px;">'
                  + _terms.google[i] + '</label>';
        }
        el.innerHTML = html;
    }
    function loadTerms() {
        fetch('/search-terms').then(function(r) { return r.json(); }).then(function(data) {
            _terms.google = data.google || [];
            _terms.facebook = data.facebook || [];
            renderTermsList('google');
            renderTermsList('facebook');
            renderPartialTerms();
        });
    }
    function saveTerms(type) {
        var payload = {};
        payload[type] = _terms[type];
        fetch('/save-terms', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    }
    function addTerm(type) {
        var input = document.getElementById('new-term-' + type);
        var val = input.value.trim();
        if (!val || _terms[type].indexOf(val) !== -1) return;
        _terms[type].push(val);
        input.value = '';
        renderTermsList(type);
        renderPartialTerms();
        saveTerms(type);
    }
    function removeTerm(type, idx) {
        _terms[type].splice(idx, 1);
        renderTermsList(type);
        renderPartialTerms();
        saveTerms(type);
    }
    loadTerms();
    </script>

    <!-- Panels (toggled from navbar) -->

    <!-- Search Terms Panel -->
    <div id="panel-terms" class="panel-wrap" style="display:none;">
        <div style="background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px; max-width:520px;">
            <script>
            function showTermsTab(tab) {
                if (typeof _activeTab !== 'undefined') _activeTab = tab;
                document.getElementById('terms-google').style.display = tab === 'google' ? '' : 'none';
                document.getElementById('terms-facebook').style.display = tab === 'facebook' ? '' : 'none';
                document.getElementById('tab-btn-google').style.background = tab === 'google' ? '#2c3e50' : 'white';
                document.getElementById('tab-btn-google').style.color = tab === 'google' ? 'white' : '#555';
                document.getElementById('tab-btn-facebook').style.background = tab === 'facebook' ? '#1877f2' : 'white';
                document.getElementById('tab-btn-facebook').style.color = tab === 'facebook' ? 'white' : '#555';
            }
            </script>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                <strong style="color:#2c3e50; font-size:14px;"><i class="fa-solid fa-list-check"></i> Search Terms</strong>
                <div style="display:flex; gap:4px;">
                    <button onclick="showTermsTab('google')" id="tab-btn-google"
                        style="padding:3px 10px; font-size:11px; border:1px solid #2c3e50; border-radius:4px;
                               background:#2c3e50; color:white; cursor:pointer;">Google</button>
                    <button onclick="showTermsTab('facebook')" id="tab-btn-facebook"
                        style="padding:3px 10px; font-size:11px; border:1px solid #ddd; border-radius:4px;
                               background:white; color:#555; cursor:pointer;"><i class="fa-brands fa-facebook-f" style="color:#1877f2;"></i> Facebook</button>
                </div>
            </div>
            <div id="terms-google">
                <div id="terms-list-google" style="max-height:160px; overflow-y:auto; font-size:12px; margin-bottom:8px;">
                {% if not init_terms.google %}<span style="color:#aaa;">No terms yet.</span>{% endif %}
                {% for t in init_terms.google %}<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 0;border-bottom:1px solid #f5f5f5;"><span>{{ t }}</span><button onclick="removeTerm('google',{{ loop.index0 }})" style="background:none;border:none;color:#e74c3c;cursor:pointer;font-size:13px;padding:0 4px;" title="Remove">&times;</button></div>{% endfor %}
                </div>
                <div style="display:flex; gap:5px;">
                    <input id="new-term-google" type="text" placeholder="Add Google term..."
                        style="flex:1; padding:5px 8px; border:1px solid #ddd; border-radius:4px; font-size:12px;"
                        onkeydown="if(event.key==='Enter') addTerm('google')">
                    <button onclick="addTerm('google')"
                        style="padding:5px 10px; background:#27ae60; color:white; border:none; border-radius:4px; cursor:pointer; font-size:12px;">Add</button>
                </div>
            </div>
            <div id="terms-facebook" style="display:none;">
                <div id="terms-list-facebook" style="max-height:160px; overflow-y:auto; font-size:12px; margin-bottom:8px;">
                {% if not init_terms.facebook %}<span style="color:#aaa;">No terms yet.</span>{% endif %}
                {% for t in init_terms.facebook %}<div style="display:flex;justify-content:space-between;align-items:center;padding:2px 0;border-bottom:1px solid #f5f5f5;"><span>{{ t }}</span><button onclick="removeTerm('facebook',{{ loop.index0 }})" style="background:none;border:none;color:#e74c3c;cursor:pointer;font-size:13px;padding:0 4px;" title="Remove">&times;</button></div>{% endfor %}
                </div>
                <div style="display:flex; gap:5px;">
                    <input id="new-term-facebook" type="text" placeholder="Add Facebook term..."
                        style="flex:1; padding:5px 8px; border:1px solid #ddd; border-radius:4px; font-size:12px;"
                        onkeydown="if(event.key==='Enter') addTerm('facebook')">
                    <button onclick="addTerm('facebook')"
                        style="padding:5px 10px; background:#1877f2; color:white; border:none; border-radius:4px; cursor:pointer; font-size:12px;">Add</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Partial Search Panel -->
    <div id="panel-partial" class="panel-wrap" style="display:none;">
        <div style="background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px; max-width:760px;">
            <strong style="color:#2c3e50; font-size:14px;"><i class="fa-solid fa-crosshairs"></i> Partial Search</strong>
            <div style="display:flex; gap:12px; margin-top:10px; flex-wrap:wrap;">

                <!-- Region selector -->
                <div style="flex:1; min-width:160px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="font-size:12px; font-weight:bold; color:#555;"><i class="fa-solid fa-location-dot"></i> Regions</label>
                        <span style="font-size:11px;">
                            <a href="#" onclick="document.querySelectorAll('.partial-region-cb').forEach(function(cb){cb.checked=true;}); return false;" style="color:#2980b9;">All</a> /
                            <a href="#" onclick="document.querySelectorAll('.partial-region-cb').forEach(function(cb){cb.checked=false;}); return false;" style="color:#2980b9;">None</a>
                        </span>
                    </div>
                    <div id="partial-region-list"
                        style="max-height:190px; overflow-y:auto; border:1px solid #ddd; border-radius:4px; padding:5px; font-size:12px;">
                    {% for r in nz_regions %}<label style="display:block;padding:1px 2px;cursor:pointer;"><input type="checkbox" class="partial-region-cb" value="{{ r }}" checked style="margin-right:4px;">{{ r }}</label>{% endfor %}
                    </div>
                </div>

                <!-- Terms selector -->
                <div style="flex:1; min-width:200px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="font-size:12px; font-weight:bold; color:#555;"><i class="fa-solid fa-magnifying-glass"></i> Terms</label>
                        <span style="font-size:11px;">
                            <a href="#" onclick="document.querySelectorAll('.partial-term-cb').forEach(function(cb){cb.checked=true;}); return false;" style="color:#2980b9;">All</a> /
                            <a href="#" onclick="document.querySelectorAll('.partial-term-cb').forEach(function(cb){cb.checked=false;}); return false;" style="color:#2980b9;">None</a>
                        </span>
                    </div>
                    <div id="partial-term-list"
                        style="max-height:120px; overflow-y:auto; border:1px solid #ddd; border-radius:4px; padding:5px; font-size:12px;">
                    {% for t in init_terms.google %}<label style="display:block;padding:1px 2px;cursor:pointer;"><input type="checkbox" class="partial-term-cb" value="{{ t }}" checked style="margin-right:4px;">{{ t }}</label>{% endfor %}
                    </div>
                    <label style="font-size:11px; color:#888; display:block; margin-top:8px; margin-bottom:2px;">One-time extra terms (one per line):</label>
                    <textarea id="partial-extra-terms" rows="3"
                        style="width:100%; font-size:12px; padding:5px 7px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box; resize:vertical;"
                        placeholder="e.g. home CCTV setup"></textarea>
                </div>

            </div>
            <div style="display:flex; align-items:center; gap:12px; margin-top:10px; flex-wrap:wrap;">
                <label style="font-size:12px; color:#555; cursor:pointer;">
                    <input type="checkbox" id="partial-facebook" style="margin-right:4px;">
                    <i class="fa-brands fa-facebook-f" style="color:#1877f2;"></i> Facebook (regional)
                </label>
                <label style="font-size:12px; color:#555; cursor:pointer;">
                    <input type="checkbox" id="partial-facebook-nz" style="margin-right:4px;">
                    <i class="fa-brands fa-facebook-f" style="color:#1877f2;"></i> Facebook (NZ-wide, no town)
                </label>
                <script>
                function runPartialSearch(fresh) {
                    var regions = Array.from(document.querySelectorAll('.partial-region-cb:checked')).map(function(cb){ return cb.value; });
                    var terms = Array.from(document.querySelectorAll('.partial-term-cb:checked')).map(function(cb){ return cb.value; });
                    var extraRaw = document.getElementById('partial-extra-terms').value.trim();
                    var extraTerms = extraRaw ? extraRaw.split('\\n').map(function(t){ return t.trim(); }).filter(Boolean) : [];
                    var allTerms = terms.concat(extraTerms);
                    var includeFb = document.getElementById('partial-facebook').checked;
                    var includeFbNw = document.getElementById('partial-facebook-nz').checked;
                    if (!regions.length) { alert('Please select at least one region.'); return; }
                    if (!allTerms.length && !includeFb && !includeFbNw) { alert('Please select at least one term or enable Facebook search.'); return; }
                    var statusEl = document.getElementById('partial-status');
                    statusEl.style.color = '#888';
                    statusEl.textContent = 'Checking...';
                    checkRunning('Partial Search', function() {
                        statusEl.textContent = 'Starting...';

                        fetch('/start-partial-search', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({regions: regions, google_terms: allTerms, include_facebook: includeFb, include_nationwide: includeFbNw, fresh: !!fresh})
                        }).then(function(r){ return r.json(); }).then(function(d) {
                            if (d.ok) {
                                statusEl.style.color = '#27ae60';
                                statusEl.textContent = 'Search started! Scroll up to see the log.';
                                var wrap = document.getElementById('progress-wrap');
                                if (wrap) {
                                    wrap.style.display = 'block';
                                    var logPanel = document.getElementById('log-panel');
                                    if (logPanel) logPanel.style.display = '';
                                    var logBtn = document.getElementById('log-toggle-btn');
                                    if (logBtn) logBtn.textContent = 'Hide log';
                                    wrap.scrollIntoView({behavior: 'smooth', block: 'start'});
                                }
                                loadSearchProgress();
                                setTimeout(function(){ statusEl.textContent = ''; }, 8000);
                            } else {
                                statusEl.style.color = '#e74c3c';
                                statusEl.textContent = d.error || 'Error starting search.';
                            }
                        }).catch(function(){ statusEl.style.color = '#e74c3c'; statusEl.textContent = 'Request failed.'; });
                    }, function() { statusEl.textContent = ''; });
                }
                </script>
                <button onclick="runPartialSearch(false)"
                    style="padding:7px 18px; background:#8e44ad; color:white; border:none; border-radius:5px;
                           cursor:pointer; font-size:13px; font-weight:bold;">
                    <i class="fa-solid fa-play"></i> Run Partial Search
                </button>
                <button onclick="if(confirm('Start partial search fresh (clear all saved progress)?')) runPartialSearch(true);"
                    id="partial-fresh-btn"
                    style="display:none; padding:5px 10px; background:#d68910; color:white; border:none; border-radius:5px;
                           cursor:pointer; font-size:11px;" title="Start Fresh">
                    &#8635; Fresh
                </button>
                <small id="partial-progress-badge" style="display:none; color:#e67e22; font-size:11px; white-space:nowrap;"></small>
                <span id="partial-status" style="font-size:12px; color:#888;"></span>
            </div>
        </div>
    </div>

    <!-- Bulk Recheck Panel -->
    <div id="panel-bulk" class="panel-wrap" style="display:none;">
    <div id="bulkRecheckPanel" style="background:#1e1e2e; border:1px solid #333; border-radius:6px; padding:14px 18px; margin-bottom:14px;">
      <div style="display:flex; align-items:center; justify-content:space-between; cursor:pointer;" onclick="toggleBulkPanel()">
        <strong style="color:#e0e0e0; font-size:13px;"><i class="fa-solid fa-rotate"></i> Bulk Recheck</strong>
        <span id="bulkPanelToggle" style="color:#aaa; font-size:11px;">&#9660; expand</span>
      </div>
      <div id="bulkPanelBody" style="display:none; margin-top:12px;">
        <div style="margin-bottom:10px; font-size:12px; color:#aaa;">Re-run selected checks against existing companies in the database.</div>

        <!-- Check type selection -->
        <div style="margin-bottom:10px;">
          <strong style="color:#ccc; font-size:12px; display:block; margin-bottom:6px;">Checks to run:</strong>
          <div style="display:flex; flex-wrap:wrap; gap:10px;">
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-facebook" value="facebook"> <i class="fa-brands fa-facebook-f" style="color:#1877f2"></i> Facebook
            </label>
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-google" value="google"> <i class="fa-brands fa-google" style="color:#ea4335"></i> Google
            </label>
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-linkedin" value="linkedin"> <i class="fa-brands fa-linkedin-in" style="color:#0a66c2"></i> LinkedIn
            </label>
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-nzsa" value="nzsa"> <span style="color:#c0392b; font-weight:bold; font-size:11px;">NZSA</span>
            </label>
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-co" value="companies_office"> <i class="fa-solid fa-landmark"></i> Companies Office
            </label>
            <label style="color:#ddd; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;">
              <input type="checkbox" id="rc-pspla" value="pspla"> <i class="fa-solid fa-shield-halved"></i> PSPLA
            </label>
            <label style="color:#c39bd3; font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer;" title="Claude Sonnet reviews all associations and clears any that are clearly wrong">
              <input type="checkbox" id="rc-llm-sense" value="llm_sense"> <i class="fa-solid fa-brain"></i> AI Sense Check
            </label>
          </div>
        </div>

        <!-- Scope selection -->
        <div style="margin-bottom:12px;">
          <strong style="color:#ccc; font-size:12px; display:block; margin-bottom:6px;">Apply to:</strong>
          <div style="display:flex; gap:12px; align-items:center;">
            <label style="color:#ddd; font-size:12px; cursor:pointer;">
              <input type="radio" name="rcScope" id="rc-scope-all" value="all" checked onchange="updateBulkScope()"> All companies
            </label>
            <label style="color:#ddd; font-size:12px; cursor:pointer;">
              <input type="radio" name="rcScope" id="rc-scope-selected" value="selected" onchange="updateBulkScope()"> Selected companies
            </label>
            <span id="rcSelectedCount" style="color:#e67e22; font-size:12px; display:none;"></span>
            <button onclick="toggleRowSelection()" id="rcSelectToggle" style="display:none; padding:3px 10px; font-size:11px; background:#333; color:#ddd; border:1px solid #555; border-radius:3px; cursor:pointer;">Show checkboxes</button>
          </div>
        </div>

        <button onclick="startBulkRecheck()" id="rcStartBtn" style="padding:6px 18px; background:#27ae60; color:white; border:none; border-radius:4px; font-size:13px; cursor:pointer; font-weight:bold;">
          <i class="fa-solid fa-rotate"></i> Run Recheck
        </button>
        <span id="rcStatus" style="margin-left:12px; font-size:12px; color:#aaa;"></span>
      </div>
    </div>
    </div>

    <!-- Scheduled Searches panel -->
    <div id="panel-schedule" class="panel-wrap" style="display:none;">
      <div style="background:white; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:18px 22px; max-width:480px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
          <strong style="color:#2c3e50; font-size:15px;"><i class="fa-solid fa-calendar-days" style="margin-right:6px;"></i> Scheduled Searches</strong>
          <form method="POST" action="/toggle-schedule" style="margin:0;">
            <button class="btn" style="padding:5px 14px; font-size:12px; font-weight:bold;
                background:{{ '#27ae60' if schedule_enabled else '#95a5a6' }}; color:white; border:none; border-radius:4px; cursor:pointer;">
              <i class="fa-solid fa-{{ 'circle-check' if schedule_enabled else 'circle-xmark' }}"></i>
              {{ 'Enabled' if schedule_enabled else 'Disabled' }}
            </button>
          </form>
        </div>
        <p style="color:#666; font-size:12px; margin:0 0 12px;">
          When enabled, searches run automatically on the schedule below.
          The dashboard process must be running on your PC (tray icon visible) — the browser tab does not need to be open.
        </p>
        <table style="width:100%; font-size:13px; border-collapse:collapse;">
          <thead>
            <tr style="border-bottom:2px solid #eee;">
              <th style="text-align:left; padding:5px 0; color:#888; font-weight:normal; font-size:11px;">Search</th>
              <th style="text-align:left; padding:5px 0; color:#888; font-weight:normal; font-size:11px;">Schedule (NZ time)</th>
            </tr>
          </thead>
          <tbody>
            <tr style="border-bottom:1px solid #f5f5f5;">
              <td style="padding:7px 0; color:#2c3e50;"><i class="fa-solid fa-magnifying-glass" style="width:16px; color:#8e44ad;"></i> Full Search</td>
              <td style="padding:7px 0; color:#555;">1st of month, 2:00am</td>
            </tr>
            <tr style="border-bottom:1px solid #f5f5f5;">
              <td style="padding:7px 0; color:#2c3e50;"><i class="fa-solid fa-calendar-week" style="width:16px; color:#16a085;"></i> Weekly Scan</td>
              <td style="padding:7px 0; color:#555;">8th, 15th, 22nd, 2:00am</td>
            </tr>
            <tr style="border-bottom:1px solid #f5f5f5;">
              <td style="padding:7px 0; color:#2c3e50;"><i class="fa-brands fa-facebook-f" style="width:16px; color:#1877f2;"></i> Facebook Search</td>
              <td style="padding:7px 0; color:#555;">Tue &amp; Fri, 3:00am</td>
            </tr>
            <tr>
              <td style="padding:7px 0; color:#2c3e50;"><i class="fa-solid fa-address-book" style="width:16px; color:#c0392b;"></i> Directory Import</td>
              <td style="padding:7px 0; color:#555;">15th of month, 4:00am</td>
            </tr>
          </tbody>
        </table>
        <p style="color:#e67e22; font-size:11px; margin:12px 0 0;">
          <i class="fa-solid fa-triangle-exclamation"></i>
          If a search is already running when a scheduled job fires, it will be skipped automatically.
        </p>
      </div>
    </div>

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
        <select id="facebookFilter" onchange="filterTable()">
            <option value="">All (Facebook)</option>
            <option value="yes">Has Facebook</option>
            <option value="no">No Facebook</option>
        </select>
        <select id="linkedinFilter" onchange="filterTable()">
            <option value="">All (LinkedIn)</option>
            <option value="yes">Has LinkedIn</option>
            <option value="no">No LinkedIn</option>
        </select>
        <select id="nzsaFilter" onchange="filterTable()">
            <option value="">All (NZSA)</option>
            <option value="yes">NZSA Member</option>
            <option value="no">Not NZSA</option>
        </select>
        <select id="serviceFilter" onchange="filterTable()">
            <option value="">All Services (Website)</option>
            <option value="alarm_systems">Alarm Systems</option>
            <option value="cctv">CCTV / Cameras</option>
            <option value="monitoring">Alarm Monitoring</option>
        </select>
        <select id="fbServiceFilter" onchange="filterTable()">
            <option value="">All Services (Facebook)</option>
            <option value="fb_alarm_systems">Alarm Systems</option>
            <option value="fb_cctv">CCTV / Cameras</option>
            <option value="fb_monitoring">Alarm Monitoring</option>
        </select>
        <select id="sortSelect" onchange="sortTable()" style="margin-left:8px;">
            <option value="name-asc">Sort: Name (A–Z)</option>
            <option value="name-desc">Sort: Name (Z–A)</option>
            <option value="date-desc">Sort: Newest First</option>
            <option value="date-asc">Sort: Oldest First</option>
        </select>
        <button class="btn btn-dark" onclick="window.location.reload()">Refresh</button>
    </div>

    <table id="companyTable" style="table-layout:fixed; width:100%;">
        <colgroup>
            <col style="width:28px">        <!-- checkbox -->
            <col style="width:18%">         <!-- company -->
            <col style="width:10%">         <!-- region -->
            <col style="width:9%">          <!-- phone -->
            <col style="width:13%">         <!-- email -->
            <col style="width:28px">        <!-- fb -->
            <col style="width:28px">        <!-- linkedin -->
            <col style="width:46px">        <!-- nzsa -->
            <col style="width:11%">         <!-- pspla status -->
            <col style="width:16%">         <!-- pspla name -->
            <col style="width:13%">         <!-- companies office -->
            <col style="width:72px">        <!-- added -->
            <col style="width:54px">        <!-- expand -->
        </colgroup>
        <thead>
            <tr>
                <th style="width:28px; padding:4px;"><input type="checkbox" id="selectAllRows" onchange="toggleSelectAll(this)" title="Select all" style="display:none;"></th>
                <th><i class="fa-solid fa-building"></i> Company (Website)</th>
                <th><i class="fa-solid fa-location-dot"></i> Region</th>
                <th><i class="fa-solid fa-phone"></i> Phone</th>
                <th><i class="fa-solid fa-envelope"></i> Email</th>
                <th style="text-align:center"><i class="fa-brands fa-facebook-f" style="color:#1877f2"></i></th>
                <th style="text-align:center"><i class="fa-brands fa-linkedin-in" style="color:#0a66c2"></i></th>
                <th style="text-align:center;padding:4px 2px;" title="NZSA Member"><img src="data:image/jpeg;base64,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" style="height:20px;width:auto;display:block;margin:0 auto;" alt="NZSA"></th>
                <th><i class="fa-solid fa-shield-halved"></i> PSPLA Status</th>
                <th><i class="fa-solid fa-id-card"></i> PSPLA Name</th>
                <th><i class="fa-solid fa-landmark"></i> Companies Office</th>
                <th><i class="fa-regular fa-calendar-plus"></i> Added</th>
                <th></th>
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
                data-facebook="{{ 'yes' if (c.facebook_url or (c.source_url and 'facebook.com' in c.source_url)) else 'no' }}"
                data-linkedin="{{ 'yes' if c.linkedin_url else 'no' }}"
                data-nzsa="{{ 'yes' if c.nzsa_member == 'true' else 'no' }}"
                data-alarm-systems="{{ 'yes' if c.has_alarm_systems else 'no' }}"
                data-cctv="{{ 'yes' if c.has_cctv_cameras else 'no' }}"
                data-monitoring="{{ 'yes' if c.has_alarm_monitoring else 'no' }}"
                data-fb-alarm-systems="{{ 'yes' if c.fb_alarm_systems else 'no' }}"
                data-fb-cctv="{{ 'yes' if c.fb_cctv_cameras else 'no' }}"
                data-fb-monitoring="{{ 'yes' if c.fb_alarm_monitoring else 'no' }}"
                data-date="{{ c.date_added or '' }}"
                data-id="{{ loop.index }}"
                data-company-id="{{ c.id }}">
                <td style="width:24px; padding:4px; text-align:center;"><input type="checkbox" class="row-select" value="{{ c.id }}" style="display:none;" onchange="updateSelectedCount()"></td>
                <td class="company-cell" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{{ c.company_name or '' }}">
                    {% if c.website %}<a href="{{ c.website }}" target="_blank">{{ c.company_name or '-' }}</a>{% else %}{{ c.company_name or '-' }}{% endif %}
                </td>
                <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{{ c.region or '' }}">{{ c.region or '-' }}</td>
                <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="{{ c.phone or '' }}">{{ c.phone or '-' }}</td>
                <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="{{ c.email or '' }}">{% if c.email %}<a href="mailto:{{ c.email }}">{{ c.email }}</a>{% else %}-{% endif %}</td>
                <td style="text-align:center">
                    {% if c.facebook_url %}
                        <a href="{{ c.facebook_url }}" target="_blank" class="fb-tag" title="{{ c.facebook_url }}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 512" width="7" height="11" fill="white" style="vertical-align:middle"><path d="M279.14 288l14.22-92.66h-88.91v-60.13c0-25.35 12.42-50.06 52.24-50.06h40.42V6.26S260.43 0 225.36 0c-73.22 0-121.08 44.38-121.08 124.72v70.62H22.89V288h81.39v224h100.17V288z"/></svg></a>
                    {% elif c.source_url and 'facebook.com' in c.source_url %}
                        <a href="{{ c.source_url }}" target="_blank" class="fb-tag" style="opacity:0.6" title="{{ c.source_url }}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 512" width="7" height="11" fill="white" style="vertical-align:middle"><path d="M279.14 288l14.22-92.66h-88.91v-60.13c0-25.35 12.42-50.06 52.24-50.06h40.42V6.26S260.43 0 225.36 0c-73.22 0-121.08 44.38-121.08 124.72v70.62H22.89V288h81.39v224h100.17V288z"/></svg></a>
                    {% else %}-{% endif %}
                </td>
                <td style="text-align:center">
                    {% if c.linkedin_url %}
                        <a href="{{ c.linkedin_url }}" target="_blank" class="li-tag" title="{{ c.linkedin_url }}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512" width="8" height="11" fill="white" style="vertical-align:middle"><path d="M100.28 448H7.4V148.9h92.88zM53.79 108.1C24.09 108.1 0 83.5 0 53.8a53.79 53.79 0 0 1 107.58 0c0 29.7-24.1 54.3-53.79 54.3zM447.9 448h-92.68V302.4c0-34.7-.7-79.2-48.29-79.2-48.29 0-55.69 37.7-55.69 76.7V448h-92.78V148.9h89.08v40.8h1.3c12.4-23.5 42.69-48.3 87.88-48.3 94 0 111.28 61.9 111.28 142.3V448z"/></svg></a>
                    {% else %}-{% endif %}
                </td>
                <td style="text-align:center">
                    {% if c.nzsa_member == 'true' %}
                        <span class="nzsa-logo" title="NZSA Member{% if c.nzsa_accredited == 'true' %} — Accredited{% endif %}{% if c.nzsa_grade %}: {{ c.nzsa_grade }}{% endif %}"><img src="data:image/jpeg;base64,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" style="height:20px;width:auto;display:block;margin:0 auto;" alt="NZSA"></span>
                    {% else %}-{% endif %}
                </td>
                <td>
                    {% if lic == 'true' and c.individual_license and (not c.pspla_license_status or c.pspla_license_status|lower != 'active') and c.pspla_name %}
                        <span class="badge badge-expired"><i class="fa-solid fa-user-check status-icon"></i>EXP + INDIVIDUAL</span>
                    {% elif lic == 'true' and c.individual_license and (not c.pspla_license_status or c.pspla_license_status|lower != 'active') %}
                        <span class="badge badge-expired"><i class="fa-solid fa-user-check status-icon"></i>INDIVIDUAL ONLY</span>
                    {% elif lic == 'true' %}
                        <span class="badge badge-licensed"><i class="fa-solid fa-circle-check status-icon"></i>LICENSED</span>
                    {% elif c.pspla_license_status and c.pspla_license_status|lower == 'expired' %}
                        <span class="badge badge-expired"><i class="fa-solid fa-triangle-exclamation status-icon"></i>EXPIRED</span>
                    {% elif lic == 'false' and c.individual_license and c.pspla_name %}
                        <span class="badge badge-expired"><i class="fa-solid fa-user-check status-icon"></i>EXP + INDIVIDUAL</span>
                    {% elif lic == 'false' and c.individual_license %}
                        <span class="badge badge-expired"><i class="fa-solid fa-user-check status-icon"></i>INDIVIDUAL ONLY</span>
                    {% elif lic == 'false' %}
                        <span class="badge badge-unlicensed"><i class="fa-solid fa-circle-xmark status-icon"></i>NOT LICENSED</span>
                    {% else %}
                        <span class="badge badge-unknown"><i class="fa-solid fa-circle-question status-icon"></i>UNKNOWN</span>
                    {% endif %}
                </td>
                <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{{ c.pspla_name or '' }}{% if c.pspla_address %} — {{ c.pspla_address }}{% endif %}">
                    {{ c.pspla_name or '-' }}
                </td>
                <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="{{ c.companies_office_name or '' }}{% if c.companies_office_address %} — {{ c.companies_office_address }}{% endif %}">
                    {{ c.companies_office_name or '-' }}
                </td>
                <td style="white-space:nowrap; font-size:12px; color:#666;">{{ (c.date_added or '')[:10] or '-' }}</td>
                <td>
                    <button class="expand-btn" onclick="var r=document.getElementById('detail-{{ loop.index }}');if(r){var o=r.classList.toggle('open');this.textContent=o?'\u25b2 less':'\u25bc more';}">&#x25BC; more</button>
                </td>
            </tr>
            <tr class="detail-row" id="detail-{{ loop.index }}">
                <td colspan="13">
                    {% if c.match_reason %}
                    <div style="background:#eaf4fb; border-left:4px solid #2980b9; padding:10px 14px; margin-bottom:10px; border-radius:4px; font-size:13px;">
                        <strong style="color:#2471a3;">Why this classification?</strong><br>
                        {{ c.match_reason }}
                    </div>
                    {% endif %}
                    <div style="padding:4px 0;">

                        <!-- TOP ROW: PSPLA + Companies Office -->
                        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px;">

                            <!-- PSPLA Licence Card -->
                            <div style="background:#eaf3fb; border:1px solid #aed4f0; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; color:#1a5276; margin-bottom:8px; display:flex; align-items:center; gap:6px;">
                                    <i class="fa-solid fa-shield-halved" style="color:#2980b9;"></i> PSPLA Licence
                                </div>
                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:4px 16px; font-size:12px;">
                                    {% if c.pspla_name %}<div style="grid-column:1/-1;"><span style="color:#888;">Name:</span> <strong>{{ c.pspla_name }}</strong></div>{% endif %}
                                    {% if c.pspla_license_status %}<div><span style="color:#888;">Status:</span> {{ c.pspla_license_status }}</div>{% endif %}
                                    {% if c.pspla_license_number %}<div><span style="color:#888;">Number:</span> <a href="https://forms.justice.govt.nz/search/PSPLA/" target="_blank" onclick="copyAndOpen(event,'{{ c.pspla_license_number }}')">{{ c.pspla_license_number }}</a></div>{% endif %}
                                    {% if c.pspla_license_expiry %}<div><span style="color:#888;">Expires:</span> {{ c.pspla_license_expiry }}</div>{% endif %}
                                    {% if c.pspla_license_classes %}<div><span style="color:#888;">Classes:</span> {{ c.pspla_license_classes }}</div>{% endif %}
                                    {% if c.pspla_license_start %}<div><span style="color:#888;">Start:</span> {{ c.pspla_license_start }}</div>{% endif %}
                                    {% if c.pspla_permit_type %}<div><span style="color:#888;">Permit:</span> {{ c.pspla_permit_type }}</div>{% endif %}
                                    {% if c.license_type %}<div><span style="color:#888;">Type:</span> {{ c.license_type }}</div>{% endif %}
                                    {% if c.match_method %}<div><span style="color:#888;">Match:</span> {{ c.match_method }}</div>{% endif %}
                                    {% if c.individual_license %}<div style="grid-column:1/-1;"><span style="color:#888;">Individual:</span> {{ c.individual_license }}</div>{% endif %}
                                </div>
                                <div style="margin-top:8px; padding-top:8px; border-top:1px solid #aed4f0; display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                    <span id="pspla-recheck-result-{{ c.id }}" style="font-size:11px; color:#555;"></span>
                                    <input id="pspla-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 6px; font-size:11px; border:1px solid #aed4f0; border-radius:3px; flex:1; min-width:120px; max-width:180px;">
                                    <button onclick="recheckPspla({{ c.id }})" id="pspla-btn-{{ c.id }}"
                                            data-directors="{{ (c.director_name or '') | e }}"
                                            data-region="{{ (c.region or '') | e }}"
                                            data-coname="{{ (c.companies_office_name or '') | e }}"
                                            style="padding:2px 10px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                            <!-- Companies Office Card -->
                            <div style="background:#f8f9fa; border:1px solid #dee2e6; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; color:#2c3e50; margin-bottom:8px; display:flex; align-items:center; gap:6px;">
                                    🏢 Companies Office
                                </div>
                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:4px 16px; font-size:12px;">
                                    {% if c.companies_office_name %}<div style="grid-column:1/-1;"><span style="color:#888;">Name:</span> <strong>{{ c.companies_office_name }}</strong>{% if c.co_status %} <em style="color:#888;">({{ c.co_status }})</em>{% endif %}</div>{% endif %}
                                    {% if c.nzbn %}<div><span style="color:#888;">NZBN:</span> {{ c.nzbn }}</div>{% endif %}
                                    {% if c.co_incorporated %}<div><span style="color:#888;">Incorporated:</span> {{ c.co_incorporated }}</div>{% endif %}
                                    {% if c.co_website %}<div style="grid-column:1/-1;"><span style="color:#888;">CO Website:</span> <a href="{{ c.co_website }}" target="_blank" style="color:#3498db;">{{ c.co_website }}</a></div>{% endif %}
                                    {% if c.director_name %}<div style="grid-column:1/-1;"><span style="color:#888;">Directors:</span> {{ c.director_name }}</div>{% endif %}
                                    {% if c.companies_office_address %}<div style="grid-column:1/-1;"><span style="color:#888;">Address:</span> {{ c.companies_office_address }}</div>{% endif %}
                                </div>
                                <div style="margin-top:8px; padding-top:8px; border-top:1px solid #dee2e6; display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                    <span id="co-recheck-result-{{ c.id }}" style="font-size:11px; color:#555;"></span>
                                    <input id="co-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 6px; font-size:11px; border:1px solid #ddd; border-radius:3px; flex:1; min-width:120px; max-width:180px;">
                                    <button onclick="recheckCompaniesOffice({{ c.id }})" id="co-btn-{{ c.id }}"
                                            style="padding:2px 10px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                        </div>

                        <!-- SECOND ROW: Facebook + NZSA + Google + LinkedIn -->
                        <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:12px;">

                            <!-- Facebook Card -->
                            <div style="background:#f0f4ff; border:1px solid #c3cef5; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; color:#1877f2; margin-bottom:8px; display:flex; align-items:center; gap:5px;">
                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 512" width="8" height="12" fill="#1877f2"><path d="M279.14 288l14.22-92.66h-88.91v-60.13c0-25.35 12.42-50.06 52.24-50.06h40.42V6.26S260.43 0 225.36 0c-73.22 0-121.08 44.38-121.08 124.72v70.62H22.89V288h81.39v224h100.17V288z"/></svg>
                                    Facebook
                                </div>
                                <div style="font-size:12px; margin-bottom:6px;">
                                    <div id="fb-result-{{ c.id }}" style="margin-bottom:4px; word-break:break-all;">
                                        {% if c.facebook_url %}<a href="{{ c.facebook_url }}" target="_blank">{{ c.facebook_url }}</a>
                                        {% elif c.source_url and 'facebook.com' in c.source_url %}<a href="{{ c.source_url }}" target="_blank">{{ c.source_url }}</a>
                                        {% else %}<em style="color:#aaa">Not found</em>{% endif %}
                                    </div>
                                    {% if c.fb_followers or c.fb_phone or c.fb_email or c.fb_address or c.fb_category or c.fb_rating %}
                                    <div style="border-top:1px solid #c3cef5; padding-top:6px; margin-top:4px; display:flex; flex-direction:column; gap:3px; color:#444;">
                                        {% if c.fb_followers %}<div>👥 {{ c.fb_followers }} followers</div>{% endif %}
                                        {% if c.fb_category %}<div>🏷️ {{ c.fb_category }}</div>{% endif %}
                                        {% if c.fb_rating %}<div>⭐ {{ c.fb_rating }}</div>{% endif %}
                                        {% if c.fb_phone %}<div>📞 {{ c.fb_phone }}</div>{% endif %}
                                        {% if c.fb_email %}<div>✉️ <a href="mailto:{{ c.fb_email }}">{{ c.fb_email }}</a></div>{% endif %}
                                        {% if c.fb_address %}<div>📍 {{ c.fb_address }}</div>{% endif %}
                                        {% if c.fb_description %}<div style="color:#777; font-style:italic; margin-top:2px; font-size:11px;">{{ c.fb_description[:120] }}{% if c.fb_description|length > 120 %}…{% endif %}</div>{% endif %}
                                    </div>
                                    {% endif %}
                                </div>
                                <div style="padding-top:8px; border-top:1px solid #c3cef5; display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                                    <input id="fb-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 5px; font-size:11px; border:1px solid #c3cef5; border-radius:3px; flex:1; min-width:80px;">
                                    <button onclick="lookupFacebook({{ c.id }})" id="fb-btn-{{ c.id }}"
                                            style="padding:2px 8px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                            <!-- NZSA Card -->
                            <div style="background:#fff5f5; border:1px solid #f5c6cb; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; color:#c0392b; margin-bottom:8px; display:flex; align-items:center; gap:5px;">
                                    <span style="background:#c0392b; color:white; font-size:10px; padding:1px 5px; border-radius:3px; font-weight:bold;">NZSA</span>
                                    Membership
                                </div>
                                <div id="nzsa-result-{{ c.id }}" style="font-size:12px; margin-bottom:6px;">
                                    {% if c.nzsa_member == 'true' %}
                                        <strong style="color:#27ae60;">Member</strong>{% if c.nzsa_member_name %} — {{ c.nzsa_member_name }}{% endif %}
                                        {% if c.nzsa_accredited == 'true' %}<br><em style="color:#888; font-size:11px;">Accredited{% if c.nzsa_grade %}: {{ c.nzsa_grade }}{% endif %}</em>{% endif %}
                                        {% if c.nzsa_contact_name %}<br>👤 {{ c.nzsa_contact_name }}{% endif %}
                                        {% if c.nzsa_phone %}<br>📞 {{ c.nzsa_phone }}{% endif %}
                                        {% if c.nzsa_email %}<br>✉️ <a href="mailto:{{ c.nzsa_email }}">{{ c.nzsa_email }}</a>{% endif %}
                                        {% if c.nzsa_overview %}<br><span style="color:#777; font-style:italic; font-size:11px;">{{ c.nzsa_overview[:120] }}{% if c.nzsa_overview|length > 120 %}…{% endif %}</span>{% endif %}
                                    {% else %}
                                        <em style="color:#aaa">Not a member</em>
                                    {% endif %}
                                </div>
                                <div style="padding-top:8px; border-top:1px solid #f5c6cb; display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                                    <input id="nzsa-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 5px; font-size:11px; border:1px solid #f5c6cb; border-radius:3px; flex:1; min-width:80px;">
                                    <button onclick="recheckNzsa({{ c.id }})" id="nzsa-btn-{{ c.id }}"
                                            style="padding:2px 8px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                            <!-- Google Card -->
                            <div style="background:#fff8f0; border:1px solid #fce4c3; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; margin-bottom:8px; display:flex; align-items:center; gap:4px;">
                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 488 512" width="11" height="12" style="vertical-align:middle;"><path fill="#4285F4" d="M488 261.8C488 403.3 391.1 504 248 504 110.8 504 0 393.2 0 256S110.8 8 248 8c66.8 0 123 24.5 166.3 64.9l-67.5 64.9C258.5 52.6 94.3 116.6 94.3 256c0 86.5 69.1 156.6 153.7 156.6 98.2 0 135-70.4 140.8-106.9H248v-85.3h236.1c2.3 12.7 3.9 24.9 3.9 41.4z"/></svg>
                                    <span style="color:#4285F4;">G</span><span style="color:#ea4335;">o</span><span style="color:#fbbc04;">o</span><span style="color:#4285F4;">g</span><span style="color:#34a853;">l</span><span style="color:#ea4335;">e</span>
                                </div>
                                <div id="google-recheck-result-{{ c.id }}" style="font-size:12px; margin-bottom:6px;">
                                    {% if c.google_rating or c.google_phone or c.google_address %}
                                        {% if c.google_rating %}<div>⭐ {{ c.google_rating }}{% set rev = c.google_reviews | int(default=0) %}{% if rev > 0 %} <span style="color:#888;">({{ rev }} reviews)</span>{% endif %} &nbsp;<a href="https://www.google.com/search?q={{ (c.company_name or '') | urlencode }}" target="_blank" style="font-size:10px; color:#4285F4;">View on Google ↗</a></div>{% endif %}
                                        {% if c.google_phone %}<div>📞 {{ c.google_phone }}</div>{% endif %}
                                        {% if c.google_email %}<div>✉️ <a href="mailto:{{ c.google_email }}">{{ c.google_email }}</a></div>{% endif %}
                                        {% if c.google_address %}<div>📍 {{ c.google_address }}</div>{% endif %}
                                    {% else %}
                                        <em style="color:#aaa">Not found</em>
                                    {% endif %}
                                </div>
                                <div style="padding-top:8px; border-top:1px solid #fce4c3; display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                                    <input id="google-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 5px; font-size:11px; border:1px solid #fce4c3; border-radius:3px; flex:1; min-width:80px;">
                                    <button onclick="recheckGoogleProfile({{ c.id }})" id="google-btn-{{ c.id }}"
                                            data-region="{{ (c.region or '') | e }}"
                                            style="padding:2px 8px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                            <!-- LinkedIn Card -->
                            <div style="background:#f0f5ff; border:1px solid #b3c8e8; border-radius:8px; padding:12px;">
                                <div style="font-size:12px; font-weight:bold; color:#0a66c2; margin-bottom:8px; display:flex; align-items:center; gap:5px;">
                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512" width="10" height="12" fill="#0a66c2"><path d="M100.28 448H7.4V148.9h92.88zM53.79 108.1C24.09 108.1 0 83.5 0 53.8a53.79 53.79 0 0 1 107.58 0c0 29.7-24.1 54.3-53.79 54.3zM447.9 448h-92.68V302.4c0-34.7-.7-79.2-48.29-79.2-48.29 0-55.69 37.7-55.69 76.7V448h-92.78V148.9h89.08v40.8h1.3c12.4-23.5 42.69-48.3 87.88-48.3 94 0 111.28 61.9 111.28 142.3V448z"/></svg>
                                    LinkedIn
                                </div>
                                <div id="li-result-{{ c.id }}" style="font-size:12px; margin-bottom:6px; word-break:break-all;">
                                    {% if c.linkedin_url %}<a href="{{ c.linkedin_url }}" target="_blank">{{ c.linkedin_url }}</a>
                                    {% else %}<em style="color:#aaa">Not found</em>{% endif %}
                                    {% if c.linkedin_followers or c.linkedin_industry or c.linkedin_location or c.linkedin_size or c.linkedin_website %}
                                    <div style="border-top:1px solid #b3c8e8; padding-top:5px; margin-top:4px; display:flex; flex-direction:column; gap:2px; color:#444;">
                                        {% if c.linkedin_followers %}<div>👥 {{ c.linkedin_followers }} followers</div>{% endif %}
                                        {% if c.linkedin_industry %}<div>🏭 {{ c.linkedin_industry }}</div>{% endif %}
                                        {% if c.linkedin_location %}<div>📍 {{ c.linkedin_location }}</div>{% endif %}
                                        {% if c.linkedin_size %}<div>👤 {{ c.linkedin_size }}</div>{% endif %}
                                        {% if c.linkedin_website %}<div>🌐 <a href="{{ c.linkedin_website }}" target="_blank">{{ c.linkedin_website }}</a></div>{% endif %}
                                    </div>
                                    {% endif %}
                                    {% if c.linkedin_description %}<div style="color:#777; font-style:italic; font-size:11px; margin-top:4px;">{{ c.linkedin_description[:150] }}{% if c.linkedin_description|length > 150 %}…{% endif %}</div>{% endif %}
                                </div>
                                <div style="padding-top:8px; border-top:1px solid #b3c8e8; display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                                    <input id="li-term-{{ c.id }}" type="text" value="{{ (c.company_name or '') | replace('"', '&quot;') }}"
                                           style="padding:2px 5px; font-size:11px; border:1px solid #b3c8e8; border-radius:3px; flex:1; min-width:80px;">
                                    <button onclick="lookupLinkedIn({{ c.id }})" id="li-btn-{{ c.id }}"
                                            style="padding:2px 8px; font-size:11px; background:#555; color:white; border:none; border-radius:3px; cursor:pointer; white-space:nowrap;">
                                        Re-check
                                    </button>
                                </div>
                            </div>

                        </div>

                        <!-- METADATA ROW -->
                        <div style="display:flex; flex-wrap:wrap; gap:16px; font-size:11px; color:#888; padding:8px 4px; border-top:1px solid #eee; border-bottom:1px solid #eee; margin-bottom:12px;">
                            {% if c.website_url %}<span><strong style="color:#555;">Website:</strong> <a href="{{ c.website_url }}" target="_blank" style="color:#2980b9;">{{ c.website_url }}</a></span>{% endif %}
                            <span><strong style="color:#555;">Found via:</strong> {{ c.notes or '-' }}</span>
                            <span><strong style="color:#555;">Date added:</strong> {{ (c.date_added or '')[:10] or '-' }}</span>
                            <span><strong style="color:#555;">Last checked:</strong> {{ (c.last_checked or '')[:10] or '-' }}</span>
                            <span style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;" id="services-row-{{ c.id }}">
                                <strong style="color:#555;">Website Services:</strong>
                                {% if c.has_alarm_systems %}<span class="svc-tag svc-alarm" style="background:#1a6e3c; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">Alarm Systems</span>{% endif %}
                                {% if c.has_cctv_cameras %}<span class="svc-tag svc-cctv" style="background:#1a4b8a; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">CCTV / Cameras</span>{% endif %}
                                {% if c.has_alarm_monitoring %}<span class="svc-tag svc-mon" style="background:#7a3a99; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">Alarm Monitoring</span>{% endif %}
                                {% if not c.has_alarm_systems and not c.has_cctv_cameras and not c.has_alarm_monitoring %}<span style="color:#bbb; font-size:10px;">none detected</span>{% endif %}
                                <button onclick="recheckServices({{ c.id }}, this)"
                                        data-website="{{ (c.website or '') | e }}"
                                        style="padding:1px 8px; font-size:10px; background:#555; color:white; border:none; border-radius:4px; cursor:pointer; white-space:nowrap;">Re-check</button>
                            </span>
                            <span style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                <strong style="color:#1877f2;">Facebook Services:</strong>
                                {% if c.fb_alarm_systems %}<span style="background:#1a6e3c; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">Alarm Systems</span>{% endif %}
                                {% if c.fb_cctv_cameras %}<span style="background:#1a4b8a; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">CCTV / Cameras</span>{% endif %}
                                {% if c.fb_alarm_monitoring %}<span style="background:#7a3a99; color:white; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600;">Alarm Monitoring</span>{% endif %}
                                {% if not c.fb_alarm_systems and not c.fb_cctv_cameras and not c.fb_alarm_monitoring %}<span style="color:#bbb; font-size:10px;">none detected</span>{% endif %}
                            </span>
                        </div>

                        <!-- FULL RE-CHECK BANNER -->
                        <div style="background:#2c3e50; border-radius:8px; padding:10px 16px; margin-bottom:12px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                            <span style="color:white; font-size:12px; font-weight:bold;">🔄 Full Re-check</span>
                            <span id="full-recheck-result-{{ c.id }}" style="font-size:12px; color:#ecf0f1; flex:1;"></span>
                            <small style="color:#95a5a6; order:3;">Runs CO + Facebook + Google + PSPLA + NZSA</small>
                            <button onclick="fullRecheck({{ c.id }})" id="full-recheck-btn-{{ c.id }}"
                                    data-name="{{ (c.company_name or '') | e }}"
                                    data-website="{{ (c.website_url or '') | e }}"
                                    data-region="{{ (c.region or '') | e }}"
                                    style="padding:5px 16px; font-size:12px; background:#27ae60; color:white; border:none; border-radius:4px; cursor:pointer; font-weight:bold; order:2; white-space:nowrap;">
                                Re-check all sources
                            </button>
                        </div>

                        <!-- AI LLM SENSE CHECK -->
                        <div style="background:#4a235a; border-radius:8px; padding:10px 16px; margin-bottom:12px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                            <span style="color:white; font-size:12px; font-weight:bold;">🤖 AI Sense Check</span>
                            <span id="llm-sense-result-{{ c.id }}" style="font-size:12px; color:#ecf0f1; flex:1;"></span>
                            <small style="color:#c39bd3; order:3;">Claude reviews all associations and removes obvious errors</small>
                            <button onclick="recheckLlmSense({{ c.id }})" id="llm-sense-btn-{{ c.id }}"
                                    style="padding:5px 16px; font-size:12px; background:#8e44ad; color:white; border:none; border-radius:4px; cursor:pointer; font-weight:bold; order:2; white-space:nowrap;">
                                AI Sense Check
                            </button>
                        </div>

                        <!-- AI DECISIONS -->
                        <div style="border-top:1px solid #ddd; padding-top:10px; margin-bottom:8px;">
                            <label style="font-weight:bold; color:#555; font-size:11px; display:block; margin-bottom:6px;">AI Matching Decisions</label>
                            <div id="ai-decisions-{{ c.id }}" style="font-size:11px;">
                                <button onclick="loadAIDecisions('{{ c.id }}', this.dataset.name)"
                                        data-name="{{ (c.company_name or '') | e }}"
                                        style="padding:2px 10px; font-size:11px; background:#2980b9; color:white; border:none; border-radius:3px; cursor:pointer;">
                                    Load AI reasoning
                                </button>
                            </div>
                        </div>

                        <!-- EDIT / DELETE / CORRECTION -->
                        <div style="border-top:1px solid #ddd; padding-top:10px; margin-top:4px;">
                            <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:8px;">
                                <button onclick="toggleEditForm({{ c.id }})"
                                        style="padding:3px 12px; font-size:12px; background:#8e44ad; color:white; border:none; border-radius:3px; cursor:pointer;">
                                    ✎ Edit record
                                </button>
                                <button onclick="toggleCorrectionForm({{ c.id }})"
                                        style="padding:3px 12px; font-size:12px; background:#16a085; color:white; border:none; border-radius:3px; cursor:pointer;">
                                    📝 Corrections / notes
                                </button>
                                <button data-cid="{{ c.id }}" data-cname="{{ (c.company_name or '') | e }}"
                                        onclick="deleteCompany(this.dataset.cid, this.dataset.cname)"
                                        style="padding:3px 12px; font-size:12px; background:#c0392b; color:white; border:none; border-radius:3px; cursor:pointer;">
                                    ✕ Delete this record
                                </button>
                            </div>

                            <div id="edit-form-{{ c.id }}" style="display:none; background:#f9f0ff; border:1px solid #c39bd3; border-radius:5px; padding:10px; margin-bottom:8px;">
                                <strong style="font-size:12px; color:#6c3483;">Edit Record Fields</strong>
                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:7px;">
                                    <label style="font-size:11px; color:#555;">Company Name<br>
                                        <input id="edit-name-{{ c.id }}" value="{{ (c.company_name or '') | e }}" style="width:100%; padding:3px 5px; font-size:12px; border:1px solid #ccc; border-radius:3px; box-sizing:border-box;">
                                    </label>
                                    <label style="font-size:11px; color:#555;">Website<br>
                                        <input id="edit-website-{{ c.id }}" value="{{ (c.website or '') | e }}" style="width:100%; padding:3px 5px; font-size:12px; border:1px solid #ccc; border-radius:3px; box-sizing:border-box;">
                                    </label>
                                    <label style="font-size:11px; color:#555;">Email<br>
                                        <input id="edit-email-{{ c.id }}" value="{{ (c.email or '') | e }}" style="width:100%; padding:3px 5px; font-size:12px; border:1px solid #ccc; border-radius:3px; box-sizing:border-box;">
                                    </label>
                                    <label style="font-size:11px; color:#555;">Phone<br>
                                        <input id="edit-phone-{{ c.id }}" value="{{ (c.phone or '') | e }}" style="width:100%; padding:3px 5px; font-size:12px; border:1px solid #ccc; border-radius:3px; box-sizing:border-box;">
                                    </label>
                                    <label style="font-size:11px; color:#555;">Region<br>
                                        <input id="edit-region-{{ c.id }}" value="{{ (c.region or '') | e }}" style="width:100%; padding:3px 5px; font-size:12px; border:1px solid #ccc; border-radius:3px; box-sizing:border-box;">
                                    </label>
                                </div>
                                <div style="margin-top:8px; display:flex; align-items:center; gap:8px;">
                                    <button onclick="saveEdit({{ c.id }})" style="padding:3px 12px; font-size:12px; background:#8e44ad; color:white; border:none; border-radius:3px; cursor:pointer;">Save changes</button>
                                    <button onclick="document.getElementById('edit-form-{{ c.id }}').style.display='none'" style="padding:3px 10px; font-size:12px; background:#95a5a6; color:white; border:none; border-radius:3px; cursor:pointer;">Cancel</button>
                                    <span id="edit-status-{{ c.id }}" style="font-size:11px;"></span>
                                </div>
                            </div>

                            <div id="correction-form-{{ c.id }}" style="display:none; background:#eafaf1; border:1px solid #a9dfbf; border-radius:5px; padding:10px;">
                                <strong style="font-size:12px; color:#1e8449;">Corrections &amp; notes for improving the system</strong>
                                <p style="font-size:11px; color:#555; margin:4px 0 6px;">Describe what the system got wrong — wrong website, wrong email, wrong PSPLA match etc. This is saved to a file I can read next session to improve the logic.</p>
                                <textarea id="correction-text-{{ c.id }}" style="width:100%; height:70px; font-size:12px; padding:5px; border:1px solid #a9dfbf; border-radius:3px; box-sizing:border-box; resize:vertical;" placeholder="e.g. Wrong website — picked up a council PDF instead of the real site sis-ltd.co.nz. Correct email is service@sis-ltd.co.nz"></textarea>
                                <div style="margin-top:6px; display:flex; align-items:center; gap:8px;">
                                    <button onclick="saveCorrection({{ c.id }}, '{{ (c.company_name or '') | e }}')" style="padding:3px 12px; font-size:12px; background:#16a085; color:white; border:none; border-radius:3px; cursor:pointer;">Save note</button>
                                    <button onclick="document.getElementById('correction-form-{{ c.id }}').style.display='none'" style="padding:3px 10px; font-size:12px; background:#95a5a6; color:white; border:none; border-radius:3px; cursor:pointer;">Cancel</button>
                                    <span id="correction-status-{{ c.id }}" style="font-size:11px;"></span>
                                </div>
                            </div>
                        </div>

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
            const facebook = document.getElementById('facebookFilter').value;
            const linkedin = document.getElementById('linkedinFilter').value;
            const nzsa = document.getElementById('nzsaFilter').value;
            const service = document.getElementById('serviceFilter').value;
            const fbService = document.getElementById('fbServiceFilter').value;
            const rows = document.querySelectorAll('.company-row');
            rows.forEach(row => {
                const nameMatch = !search || row.dataset.name.includes(search);
                const regionMatch = !region || row.dataset.region.includes(region);
                const statusMatch = !status || row.dataset.status === status;
                const facebookMatch = !facebook || row.dataset.facebook === facebook;
                const linkedinMatch = !linkedin || row.dataset.linkedin === linkedin;
                const nzsaMatch = !nzsa || row.dataset.nzsa === nzsa;
                let serviceMatch = true;
                if (service === 'alarm_systems') serviceMatch = row.dataset.alarmSystems === 'yes';
                else if (service === 'cctv') serviceMatch = row.dataset.cctv === 'yes';
                else if (service === 'monitoring') serviceMatch = row.dataset.monitoring === 'yes';
                let fbServiceMatch = true;
                if (fbService === 'fb_alarm_systems') fbServiceMatch = row.dataset.fbAlarmSystems === 'yes';
                else if (fbService === 'fb_cctv') fbServiceMatch = row.dataset.fbCctv === 'yes';
                else if (fbService === 'fb_monitoring') fbServiceMatch = row.dataset.fbMonitoring === 'yes';
                const visible = nameMatch && regionMatch && statusMatch && facebookMatch && linkedinMatch && nzsaMatch && serviceMatch && fbServiceMatch;
                row.style.display = visible ? '' : 'none';
                const detailRow = document.getElementById('detail-' + row.dataset.id);
                if (detailRow && !visible) detailRow.classList.remove('open');
            });
        }

        function sortTable() {
            const sel = document.getElementById('sortSelect').value;
            const tbody = document.querySelector('#companyTable tbody');
            const rows = Array.from(document.querySelectorAll('.company-row'));
            rows.sort(function(a, b) {
                if (sel === 'name-asc' || sel === 'name-desc') {
                    const cmp = a.dataset.name.localeCompare(b.dataset.name);
                    return sel === 'name-asc' ? cmp : -cmp;
                } else {
                    const da = a.dataset.date || '';
                    const db = b.dataset.date || '';
                    const cmp = da < db ? -1 : da > db ? 1 : 0;
                    return sel === 'date-desc' ? -cmp : cmp;
                }
            });
            rows.forEach(function(row) {
                const detailRow = document.getElementById('detail-' + row.dataset.id);
                tbody.appendChild(row);
                if (detailRow) tbody.appendChild(detailRow);
            });
        }

        // ── Bulk Recheck ─────────────────────────────────────────────────────────────
        function toggleBulkPanel() {
            var body = document.getElementById('bulkPanelBody');
            var toggle = document.getElementById('bulkPanelToggle');
            var open = body.style.display === 'none';
            body.style.display = open ? 'block' : 'none';
            toggle.textContent = open ? '\u25b2 collapse' : '\u25bc expand';
        }

        function updateBulkScope() {
            var sel = document.querySelector('input[name="rcScope"]:checked').value;
            var countEl = document.getElementById('rcSelectedCount');
            var toggleBtn = document.getElementById('rcSelectToggle');
            if (sel === 'selected') {
                countEl.style.display = '';
                toggleBtn.style.display = '';
                updateSelectedCount();
            } else {
                countEl.style.display = 'none';
                toggleBtn.style.display = 'none';
            }
        }

        var _rowSelectVisible = false;
        function toggleRowSelection() {
            _rowSelectVisible = !_rowSelectVisible;
            document.querySelectorAll('.row-select').forEach(function(cb) {
                cb.style.display = _rowSelectVisible ? '' : 'none';
            });
            var hdr = document.getElementById('selectAllRows');
            if (hdr) hdr.style.display = _rowSelectVisible ? '' : 'none';
            document.getElementById('rcSelectToggle').textContent = _rowSelectVisible ? 'Hide checkboxes' : 'Show checkboxes';
            updateSelectedCount();
        }

        function toggleSelectAll(masterCb) {
            document.querySelectorAll('.row-select').forEach(function(cb) {
                cb.checked = masterCb.checked;
            });
            updateSelectedCount();
        }

        function updateSelectedCount() {
            var checked = document.querySelectorAll('.row-select:checked').length;
            var el = document.getElementById('rcSelectedCount');
            if (el) el.textContent = checked + ' selected';
        }

        function startBulkRecheck() {
            var checks = [];
            ['facebook','google','linkedin','nzsa','co','pspla','llm-sense'].forEach(function(id) {
                var cb = document.getElementById('rc-' + id);
                if (cb && cb.checked) checks.push(cb.value);
            });
            if (checks.length === 0) {
                alert('Please select at least one check to run.');
                return;
            }

            var scope = document.querySelector('input[name="rcScope"]:checked').value;
            var company_ids = 'all';
            if (scope === 'selected') {
                var ids = Array.from(document.querySelectorAll('.row-select:checked')).map(function(cb) {
                    return parseInt(cb.value);
                });
                if (ids.length === 0) {
                    alert('No companies selected. Please check some rows or switch to "All companies".');
                    return;
                }
                company_ids = ids;
            }

            var btn = document.getElementById('rcStartBtn');
            var status = document.getElementById('rcStatus');
            var resetBtn = function() {
                btn.innerHTML = '<i class="fa-solid fa-rotate"></i> Run Recheck';
                btn.disabled = false;
            };
            btn.disabled = true;
            btn.textContent = 'Checking...';
            status.textContent = '';

            checkRunning('Bulk Recheck', function() {
                btn.textContent = 'Starting...';
                fetch('/start-bulk-recheck', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({checks: checks, company_ids: company_ids})
                })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d.ok) {
                        status.textContent = d.message || 'Started!';
                        status.style.color = '#27ae60';
                        resetBtn();
                        var wrap = document.getElementById('progress-wrap');
                        if (wrap) {
                            wrap.style.display = 'block';
                            var logPanel = document.getElementById('log-panel');
                            if (logPanel) logPanel.style.display = '';
                            wrap.scrollIntoView({behavior: 'smooth', block: 'start'});
                        }
                        loadSearchProgress();
                        setTimeout(function(){ status.textContent = ''; }, 8000);
                    } else {
                        status.textContent = 'Error: ' + (d.error || 'unknown');
                        status.style.color = '#e74c3c';
                        resetBtn();
                    }
                })
                .catch(function(e) {
                    status.textContent = 'Request failed';
                    status.style.color = '#e74c3c';
                    resetBtn();
                });
            }, resetBtn);
        }
        // ── End Bulk Recheck ──────────────────────────────────────────────────────────

        function lookupFacebook(id) {
            var btn = document.getElementById('fb-btn-' + id);
            var result = document.getElementById('fb-result-' + id);
            var termInput = document.getElementById('fb-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...'; btn.style.background = '#555';
            _recheckTermStart('Facebook — ' + name);
            fetch('/find-facebook', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.found && d.url) {
                    result.innerHTML = '<a href="' + d.url + '" target="_blank">' + d.url + '</a>';
                    btnSaved(btn);
                } else if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else {
                    result.innerHTML = '<em style="color:#aaa">not found</em>';
                    btnSaved(btn, '#95a5a6', 'Not found');
                }
                _recheckTermStop();
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Search';
                btn.disabled = false;
                _recheckTermStop();
            });
        }

        function recheckNzsa(id) {
            var btn = document.getElementById('nzsa-btn-' + id);
            var result = document.getElementById('nzsa-result-' + id);
            var termInput = document.getElementById('nzsa-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...';
            _recheckTermStart('NZSA — ' + name);
            fetch('/recheck-nzsa', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else if (d.member) {
                    var txt = '<strong style="color:#27ae60;">Member</strong> — ' + d.member_name;
                    if (d.accredited) { txt += ' <em>(Accredited' + (d.grade ? ': ' + d.grade : '') + ')</em>'; }
                    if (d.contact_name || d.phone || d.email) {
                        txt += '<br><small style="color:#555;">';
                        if (d.contact_name) txt += '<strong>Contact:</strong> ' + d.contact_name;
                        if (d.phone) txt += ' &nbsp;&#128222; ' + d.phone;
                        if (d.email) txt += ' &nbsp;&#9993; <a href="mailto:' + d.email + '">' + d.email + '</a>';
                        txt += '</small>';
                    }
                    if (d.overview) { txt += '<br><small style="color:#777;font-style:italic;">' + d.overview.substring(0, 200) + (d.overview.length > 200 ? '…' : '') + '</small>'; }
                    result.innerHTML = txt;
                    btnSaved(btn);
                } else {
                    result.innerHTML = '<em style="color:#aaa">not found / not a member</em>';
                    btnSaved(btn, '#95a5a6', 'Not found');
                }
                _recheckTermStop();
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
                _recheckTermStop();
            });
        }

        function lookupLinkedIn(id) {
            var btn = document.getElementById('li-btn-' + id);
            var result = document.getElementById('li-result-' + id);
            var termInput = document.getElementById('li-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...'; btn.style.background = '#555';
            _recheckTermStart('LinkedIn — ' + name);
            fetch('/find-linkedin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.found && d.url) {
                    var html = '<a href="' + d.url + '" target="_blank">' + d.url + '</a>';
                    var details = '';
                    if (d.followers) details += '<div>👥 ' + d.followers + ' followers</div>';
                    if (d.industry) details += '<div>🏭 ' + d.industry + '</div>';
                    if (d.location) details += '<div>📍 ' + d.location + '</div>';
                    if (d.size) details += '<div>👤 ' + d.size + '</div>';
                    if (d.website) details += '<div>🌐 <a href="' + d.website + '" target="_blank">' + d.website + '</a></div>';
                    if (details) html += '<div style="border-top:1px solid #b3c8e8;padding-top:5px;margin-top:4px;display:flex;flex-direction:column;gap:2px;color:#444;">' + details + '</div>';
                    if (d.description) html += '<div style="color:#777;font-style:italic;font-size:11px;margin-top:4px;">' + d.description.substring(0, 150) + (d.description.length > 150 ? '…' : '') + '</div>';
                    result.innerHTML = html;
                    btnSaved(btn);
                } else if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else {
                    result.innerHTML = '<em style="color:#aaa">not found</em>';
                    btnSaved(btn, '#95a5a6', 'Not found');
                }
                _recheckTermStop();
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Search';
                btn.disabled = false;
                _recheckTermStop();
            });
        }

        function recheckPspla(id) {
            var btn = document.getElementById('pspla-btn-' + id);
            var result = document.getElementById('pspla-recheck-result-' + id);
            var termInput = document.getElementById('pspla-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...';
            var directors = btn.dataset.directors || '';
            var region = btn.dataset.region || '';
            var coname = btn.dataset.coname || '';
            _recheckTermStart('PSPLA — ' + name);
            fetch('/recheck-pspla', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name, directors: directors, region: region, co_name: coname})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else if (d.licensed && d.individual_license && d.pspla_name) {
                    result.innerHTML = '<strong style="color:#e67e22">Exp + Individual</strong> — company: ' + d.pspla_name + ' (' + (d.pspla_license_status || 'expired') + '), individual: ' + d.individual_license;
                    btnSaved(btn, '#e67e22');
                } else if (d.licensed && d.individual_license) {
                    result.innerHTML = '<strong style="color:#e67e22">Individual Only</strong> — ' + d.individual_license;
                    btnSaved(btn, '#e67e22');
                } else if (d.licensed) {
                    result.innerHTML = '<strong style="color:#27ae60">Licensed</strong> — ' + (d.pspla_name || '');
                    btnSaved(btn);
                } else {
                    result.innerHTML = '<em style="color:#e74c3c">Not licensed</em>';
                    btnSaved(btn, '#95a5a6');
                }
                _recheckTermStop();
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check';
                btn.disabled = false;
                _recheckTermStop();
            });
        }

        function btnSaved(btn, color, label) {
            color = color || '#27ae60';
            label = label || 'Re-check';
            btn.textContent = '✓ Saved';
            btn.style.background = color;
            btn.disabled = true;
            setTimeout(function() {
                btn.textContent = label;
                btn.style.background = '#555';
                btn.disabled = false;
            }, 2000);
        }

        // ── Recheck terminal panel ─────────────────────────────────────────────
        var _recheckTermTimer = null;
        function _recheckTermStart(label) {
            try {
                var panel = document.getElementById('recheck-terminal');
                var out   = document.getElementById('recheck-term-output');
                var lbl   = document.getElementById('recheck-term-label');
                var stat  = document.getElementById('recheck-term-status');
                if (!panel || !out || !lbl || !stat) return;
                lbl.textContent  = '\u25B6 ' + label;
                stat.textContent = 'running\u2026';
                out.textContent  = '(waiting for output\u2026)';
                panel.style.display = '';
                _recheckTermPoll();
                _recheckTermTimer = setInterval(_recheckTermPoll, 1000);
            } catch(e) {}
        }
        function _recheckTermStop() {
            try {
                clearInterval(_recheckTermTimer);
                _recheckTermTimer = null;
                setTimeout(function() {
                    _recheckTermPoll();
                    var stat = document.getElementById('recheck-term-status');
                    if (stat) stat.textContent = 'done';
                    var lbl = document.getElementById('recheck-term-label');
                    if (lbl) lbl.textContent = lbl.textContent.replace('\u25B6', '\u2713');
                }, 400);
            } catch(e) {}
        }
        function _recheckTermPoll() {
            fetch('/recheck-log')
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    var out = document.getElementById('recheck-term-output');
                    if (!out) return;
                    var lines = d.lines || [];
                    out.textContent = lines.length ? lines.join('\\n') : '(no output yet)';
                    out.scrollTop = out.scrollHeight;
                })
                .catch(function() {});
        }

        function recheckServices(id, btn) {
            var website = btn.dataset.website;
            if (!website) { alert('No website URL available.'); return; }
            btn.disabled = true;
            btn.textContent = 'Checking...';
            _recheckTermStart('Services — ' + website);
            fetch('/recheck-services', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, website: website})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    btn.textContent = 'Error'; btn.disabled = false;
                    return;
                }
                // Update the tags inline
                var row = document.getElementById('services-row-' + id);
                // Remove existing tags and "none" placeholder
                row.querySelectorAll('.svc-tag, .svc-none').forEach(function(el) { el.remove(); });
                var noneEl = row.querySelector('span[style*="bbb"]');
                if (noneEl) noneEl.remove();
                var insertBefore = btn;
                function addTag(label, color, cls) {
                    var t = document.createElement('span');
                    t.className = 'svc-tag ' + cls;
                    t.style.cssText = 'background:' + color + ';color:white;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;';
                    t.textContent = label;
                    row.insertBefore(t, insertBefore);
                }
                if (d.has_alarm_systems) addTag('Alarm Systems', '#1a6e3c', 'svc-alarm');
                if (d.has_cctv_cameras) addTag('CCTV / Cameras', '#1a4b8a', 'svc-cctv');
                if (d.has_alarm_monitoring) addTag('Alarm Monitoring', '#7a3a99', 'svc-mon');
                if (!d.has_alarm_systems && !d.has_cctv_cameras && !d.has_alarm_monitoring) {
                    var n = document.createElement('span');
                    n.style.cssText = 'color:#bbb;font-size:10px;';
                    n.textContent = 'none detected';
                    row.insertBefore(n, insertBefore);
                }
                btnSaved(btn, '#27ae60', 'Re-check');
                _recheckTermStop();
            })
            .catch(function() { btn.textContent = 'Re-check'; btn.disabled = false; _recheckTermStop(); });
        }

        function recheckCompaniesOffice(id) {
            var btn = document.getElementById('co-btn-' + id);
            var result = document.getElementById('co-recheck-result-' + id);
            var termInput = document.getElementById('co-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...';
            _recheckTermStart('Companies Office — ' + name);
            fetch('/recheck-companies-office', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else if (d.found) {
                    var txt = '<strong style="color:#27ae60;">Found</strong> — ' + (d.co_name || '');
                    if (d.co_status) txt += ' <em>(' + d.co_status + ')</em>';
                    if (d.nzbn) txt += ' &nbsp; NZBN: ' + d.nzbn;
                    if (d.co_incorporated) txt += '<br><small style="color:#555;">Incorporated: ' + d.co_incorporated + '</small>';
                    if (d.director_name) txt += '<br><small style="color:#555;">Director: ' + d.director_name + '</small>';
                    result.innerHTML = txt;
                    btnSaved(btn);
                } else {
                    result.innerHTML = '<em style="color:#aaa">Not found on Companies Register</em>';
                    btnSaved(btn, '#95a5a6', 'Not found');
                }
                _recheckTermStop();
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
                _recheckTermStop();
            });
        }

        function recheckGoogleProfile(id) {
            var btn = document.getElementById('google-btn-' + id);
            var result = document.getElementById('google-recheck-result-' + id);
            var termInput = document.getElementById('google-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            var region = btn.dataset.region || '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...';
            _recheckTermStart('Google Profile — ' + name);
            fetch('/recheck-google-profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name, region: region})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check'; btn.disabled = false;
                } else if (d.found) {
                    var txt = '<strong style="color:#27ae60;">Found</strong>';
                    if (d.google_rating) txt += ' &nbsp; &#9733; ' + d.google_rating + (d.google_reviews ? ' (' + d.google_reviews + ' reviews)' : '');
                    if (d.google_phone) txt += '<br><small style="color:#555;">&#128222; ' + d.google_phone + '</small>';
                    if (d.google_address) txt += '<br><small style="color:#555;">&#128205; ' + d.google_address + '</small>';
                    if (d.google_email) txt += '<br><small style="color:#555;">&#9993; ' + d.google_email + '</small>';
                    result.innerHTML = txt;
                    btnSaved(btn);
                } else {
                    result.innerHTML = '<em style="color:#aaa">No Google Business Profile found</em>';
                    btnSaved(btn, '#95a5a6', 'Not found');
                }
                _recheckTermStop();
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
                _recheckTermStop();
            });
        }

        function fullRecheck(id) {
            var btn = document.getElementById('full-recheck-btn-' + id);
            var result = document.getElementById('full-recheck-result-' + id);
            var name = btn.dataset.name || '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Running all checks...';
            result.innerHTML = '<em style="color:#888;">Running Companies Office → Facebook → Google → PSPLA → NZSA... this may take a minute.</em>';
            _recheckTermStart('Full Re-check — ' + name);
            fetch('/full-recheck', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, name: name})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'Re-check all'; btn.disabled = false;
                } else {
                    var s = d.summary || {};
                    var txt = '<strong style="color:#27ae60;">Complete</strong>';
                    if (s.pspla_licensed) {
                        txt += ' &nbsp; &#10003; PSPLA: <strong>' + (s.pspla_name || 'Licensed') + '</strong>';
                    } else {
                        txt += ' &nbsp; &#10007; PSPLA: <em style="color:#e74c3c">Not licensed</em>';
                    }
                    if (s.co) txt += '<br><small style="color:#555;">CO: ' + s.co + '</small>';
                    if (s.google_rating) txt += '<br><small style="color:#555;">Google: &#9733; ' + s.google_rating + '</small>';
                    if (s.nzsa_member) txt += '<br><small style="color:#555;">NZSA: Member</small>';
                    result.innerHTML = txt;
                    btnSaved(btn);
                }
                _recheckTermStop();
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed or timed out</em>';
                btn.textContent = 'Re-check all'; btn.disabled = false;
                _recheckTermStop();
            });
        }

        function recheckLlmSense(id) {
            var btn = document.getElementById('llm-sense-btn-' + id);
            var result = document.getElementById('llm-sense-result-' + id);
            if (!btn) return;
            btn.disabled = true;
            btn.textContent = 'Asking Claude...';
            result.innerHTML = '<em style="color:#c39bd3;">Sending all associations to Claude for review...</em>';
            _recheckTermStart('AI Sense Check — ID ' + id);
            fetch('/recheck-llm-sense', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) {
                    result.innerHTML = '<em style="color:#e74c3c">Error: ' + d.error + '</em>';
                    btn.textContent = 'AI Sense Check'; btn.disabled = false;
                } else if (d.cleared && d.cleared.length > 0) {
                    var txt = '<strong style="color:#e67e22;">Cleared ' + d.cleared.length + ' association(s):</strong><br>';
                    d.cleared.forEach(function(r) { txt += '<small style="color:#ecf0f1;">&bull; ' + r + '</small><br>'; });
                    result.innerHTML = txt;
                    btnSaved(btn, '#e67e22', 'Done');
                } else {
                    result.innerHTML = '<strong style="color:#27ae60;">&#10003; All associations look correct</strong>';
                    btnSaved(btn, '#27ae60', 'All OK');
                }
                _recheckTermStop();
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'AI Sense Check'; btn.disabled = false;
                _recheckTermStop();
            });
        }

        function loadAIDecisions(id, name) {
            var container = document.getElementById('ai-decisions-' + id);
            container.innerHTML = '<span style="color:#888">Loading...</span>';
            fetch('/company-ai-decisions?name=' + encodeURIComponent(name))
                .then(function(r) { return r.json(); })
                .then(function(rows) {
                    if (!rows || !rows.length) {
                        container.innerHTML = '<span style="color:#aaa; font-size:11px;">No AI decisions recorded for this company.</span>';
                        return;
                    }
                    var colors = {
                        'ACCEPTED': '#1a7a3a', 'CONFIRMED': '#1a7a3a',
                        'REJECTED': '#c0392b',
                        'Strategy 4': '#8e44ad',
                        'inconsistency': '#e67e22',
                        'cross-check': '#e67e22',
                    };
                    var html = '<div style="display:flex; flex-direction:column; gap:6px; margin-top:4px;">';
                    rows.forEach(function(row) {
                        var ts = row.timestamp ? new Date(row.timestamp).toLocaleString('en-NZ', {timeZone:'Pacific/Auckland'}) : '';
                        var changes = row.changes || '';
                        var notes = row.notes || '';
                        var triggered = row.triggered_by || '';
                        // Pick border colour based on keywords
                        var borderColor = '#2980b9';
                        Object.keys(colors).forEach(function(k) {
                            if (changes.indexOf(k) !== -1) borderColor = colors[k];
                        });
                        html += '<div style="border-left:3px solid ' + borderColor + '; padding:5px 8px; background:#f8f9fa; border-radius:0 4px 4px 0;">';
                        html += '<div style="color:#666; font-size:10px; margin-bottom:2px;">' + ts + ' &nbsp;·&nbsp; ' + triggered + '</div>';
                        html += '<div style="color:#222;">' + changes + '</div>';
                        if (notes) html += '<div style="color:#888; font-size:10px; margin-top:2px;">' + notes + '</div>';
                        html += '</div>';
                    });
                    html += '</div>';
                    container.innerHTML = html;
                })
                .catch(function(e) {
                    container.innerHTML = '<span style="color:#c0392b; font-size:11px;">Error loading AI decisions: ' + e + '</span>';
                });
        }

        function toggleEditForm(id) {
            var f = document.getElementById('edit-form-' + id);
            f.style.display = f.style.display === 'none' ? 'block' : 'none';
        }
        function toggleCorrectionForm(id) {
            var f = document.getElementById('correction-form-' + id);
            f.style.display = f.style.display === 'none' ? 'block' : 'none';
        }
        function saveEdit(id) {
            var status = document.getElementById('edit-status-' + id);
            var data = {
                id: id,
                company_name: document.getElementById('edit-name-' + id).value.trim(),
                website: document.getElementById('edit-website-' + id).value.trim(),
                email: document.getElementById('edit-email-' + id).value.trim(),
                phone: document.getElementById('edit-phone-' + id).value.trim(),
                region: document.getElementById('edit-region-' + id).value.trim()
            };
            status.style.color = '#888';
            status.textContent = 'Saving...';
            fetch('/update-company', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            }).then(function(r){ return r.json(); }).then(function(d) {
                if (d.ok) {
                    status.style.color = '#27ae60';
                    status.textContent = 'Saved!';
                    setTimeout(function(){ status.textContent = ''; }, 3000);
                } else {
                    status.style.color = '#e74c3c';
                    status.textContent = d.error || 'Error saving.';
                }
            }).catch(function(){ status.style.color='#e74c3c'; status.textContent='Request failed.'; });
        }
        var _recheckPending = {};

        function saveCorrection(id, companyName) {
            var status = document.getElementById('correction-status-' + id);
            var text = document.getElementById('correction-text-' + id).value.trim();
            if (!text) { status.style.color='#e74c3c'; status.textContent='Please enter a note first.'; return; }
            status.style.color = '#888';
            status.textContent = 'Saving and re-checking... this may take a moment.';
            fetch('/save-correction', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, company_name: companyName, correction: text})
            }).then(function(r){ return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    status.style.color = '#27ae60';
                    status.textContent = 'Correction saved. See review panel.';
                    setTimeout(function(){ status.textContent = ''; }, 5000);

                    var p = d.proposed;
                    _recheckPending = {id: id, company_name: companyName, proposed: p};

                    var resultDiv = document.getElementById('recheck-result');
                    if (p && p.pspla_name) {
                        resultDiv.innerHTML =
                            '<b>Proposed match:</b> ' + p.pspla_name + '<br>' +
                            '<b>Status:</b> ' + (p.pspla_license_status || '—') + '<br>' +
                            '<b>Licence #:</b> ' + (p.pspla_license_number || '—') + '<br>' +
                            '<b>Expiry:</b> ' + (p.pspla_license_expiry || '—') + '<br>' +
                            '<b>Found via:</b> ' + (p.match_method || '—') + '<br>' +
                            (p.match_reason ? '<b>Reason:</b> ' + p.match_reason : '');
                    } else {
                        resultDiv.innerHTML = '<b style="color:#e74c3c;">No PSPLA match found.</b><br>' +
                            'Approving will mark this company as <b>Not Licensed</b>.';
                    }
                    document.getElementById('recheck-lesson').textContent =
                        d.lesson_rule ? 'Lesson learned: ' + d.lesson_rule : '';
                    document.getElementById('recheck-modal').style.display = 'flex';
                } else {
                    status.style.color = '#e74c3c';
                    status.textContent = d.error || 'Error saving.';
                }
            }).catch(function(){ status.style.color='#e74c3c'; status.textContent='Request failed.'; });
        }

        function confirmRecheck(approved) {
            document.getElementById('recheck-modal').style.display = 'none';
            if (!_recheckPending.id) return;
            fetch('/confirm-recheck', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    id: _recheckPending.id,
                    company_name: _recheckPending.company_name,
                    approved: approved,
                    proposed: _recheckPending.proposed
                })
            }).then(function(r){ return r.json(); }).then(function(d){
                if (d.ok) {
                    var msg = approved ? 'Match approved and saved.' : 'Match rejected — marked as Not Licensed.';
                    alert(msg + ' Refresh the page to see the update.');
                }
                _recheckPending = {};
            });
        }


        function deleteCompany(id, name) {
            if (!confirm('Delete "' + name + '"?\\nThis cannot be undone.')) return;
            fetch('/delete-company', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    // Walk up from the delete button to find and remove both rows
                    var btn2 = document.querySelector('[data-cid="' + id + '"]');
                    if (btn2) {
                        var td = btn2.closest('td');
                        if (td) {
                            var detailTr = td.closest('tr');
                            var mainTr = detailTr ? detailTr.previousElementSibling : null;
                            if (detailTr) detailTr.remove();
                            if (mainTr) mainTr.remove();
                        }
                    }
                } else {
                    alert('Delete failed: ' + (d.error || 'unknown error'));
                }
            })
            .catch(function() { alert('Delete request failed.'); });
        }

        function copyAndOpen(e, licNum) {
            e.preventDefault();
            navigator.clipboard.writeText(licNum).catch(() => {});
            const link = e.currentTarget;
            const orig = link.title;
            link.title = 'Copied! Paste into the PSPLA search box.';
            setTimeout(() => { link.title = orig; }, 3000);
            window.open('https://forms.justice.govt.nz/search/PSPLA/', '_blank');
        }

        // ── Running Search Conflict System ───────────────────────────────────────────
        var _srConflictCallback = null;
        var _srCancelCallback = null;

        function checkRunning(actionLabel, callback, onCancel) {
            fetch('/search-running-info')
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (!d.running) { callback(); return; }
                    _srConflictCallback = callback;
                    _srCancelCallback = onCancel || null;
                    var mins = d.minutes;
                    var timeStr = mins < 1 ? 'less than a minute' : (mins === 1 ? '1 minute' : mins + ' minutes');
                    var msg = 'A <strong>' + d.type_label + '</strong> has been running for <strong>' + timeStr + '</strong>.<br>'
                            + 'Stop it and start <strong>' + actionLabel + '</strong> instead, or cancel?';
                    document.getElementById('sc-message').innerHTML = msg;
                    document.getElementById('search-conflict-modal').style.display = 'flex';
                })
                .catch(function() { callback(); });
        }

        function _srStopAndProceed() {
            var btn = document.getElementById('sc-stop-btn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Stopping...';
            fetch('/stop-search', {method: 'POST'})
                .then(function() {
                    setTimeout(function() {
                        document.getElementById('search-conflict-modal').style.display = 'none';
                        btn.disabled = false;
                        btn.innerHTML = '<i class="fa-solid fa-stop"></i> Stop &amp; Start New';
                        var cb = _srConflictCallback;
                        _srConflictCallback = null;
                        _srCancelCallback = null;
                        if (cb) cb();
                    }, 2000);
                })
                .catch(function() {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-stop"></i> Stop &amp; Start New';
                });
        }

        function _srDismiss() {
            document.getElementById('search-conflict-modal').style.display = 'none';
            _srConflictCallback = null;
            var cc = _srCancelCallback;
            _srCancelCallback = null;
            if (cc) cc();
        }

        function searchFormCheck(form, actionLabel) {
            checkRunning(actionLabel, function() { form.submit(); });
            return false;
        }
        // ── End Running Search Conflict System ───────────────────────────────────────
    </script>

<!-- Clear DB Modal -->
<div id="clear-db-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%;
     background:rgba(44,62,80,0.97); z-index:9999; align-items:center; justify-content:center;">
    <div style="background:white; padding:40px; border-radius:12px; text-align:center; max-width:420px; width:90%;">
        <i class="fa-solid fa-triangle-exclamation" style="font-size:48px; color:#e74c3c; margin-bottom:16px; display:block;"></i>
        <h2 style="margin:0 0 8px; color:#c0392b;">Delete All Data</h2>
        <p style="color:#555; font-size:15px; margin-bottom:6px;">
            You are about to permanently delete
            <strong style="color:#c0392b;">{{ total }} {{ 'entry' if total == 1 else 'entries' }}</strong>
            from the database.
        </p>
        <p style="color:#888; font-size:13px; margin-bottom:16px;">
            Search progress will also be reset so the next full search starts from scratch.
            <strong>This cannot be undone.</strong> A CSV backup will be saved locally and to Dropbox first.
        </p>
        <form method="POST" action="/clear-db">
            <input type="password" name="clear_password" placeholder="Enter password to confirm"
                style="width:100%; padding:10px 14px; border:1px solid #e74c3c; border-radius:6px;
                       font-size:15px; box-sizing:border-box; margin-bottom:12px;">
            <button type="submit"
                style="width:100%; padding:11px; background:#e74c3c; color:white; border:none;
                       border-radius:6px; font-size:15px; font-weight:bold; cursor:pointer;">
                <i class="fa-solid fa-trash-can"></i> Yes, Delete Everything
            </button>
        </form>
        <button onclick="document.getElementById('clear-db-modal').style.display='none';"
            style="margin-top:10px; background:none; border:none; color:#999; cursor:pointer; font-size:13px;">Cancel</button>
    </div>
</div>

<!-- Recheck Review Modal -->
<div id="recheck-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%;
     background:rgba(44,62,80,0.97); z-index:9999; align-items:center; justify-content:center;">
    <div style="background:white; padding:36px; border-radius:12px; max-width:480px; width:90%;">
        <h2 style="margin:0 0 6px; color:#2c3e50;"><i class="fa-solid fa-magnifying-glass"></i> Recheck Result</h2>
        <p style="color:#666; font-size:13px; margin-bottom:18px;">Your correction was saved. Here's what the system found — is this a better match?</p>
        <div id="recheck-result" style="background:#f8f9fa; border-radius:8px; padding:16px; margin-bottom:20px; font-size:14px; line-height:1.7;"></div>
        <div id="recheck-lesson" style="font-size:12px; color:#888; margin-bottom:20px; font-style:italic;"></div>
        <div style="display:flex; gap:10px;">
            <button onclick="confirmRecheck(true)"
                style="flex:1; padding:11px; background:#27ae60; color:white; border:none;
                       border-radius:6px; font-size:14px; font-weight:bold; cursor:pointer;">
                <i class="fa-solid fa-check"></i> Yes, use this match
            </button>
            <button onclick="confirmRecheck(false)"
                style="flex:1; padding:11px; background:#e74c3c; color:white; border:none;
                       border-radius:6px; font-size:14px; font-weight:bold; cursor:pointer;">
                <i class="fa-solid fa-xmark"></i> No, not a match
            </button>
        </div>
        <button onclick="document.getElementById('recheck-modal').style.display='none';"
            style="margin-top:10px; background:none; border:none; color:#999; cursor:pointer;
                   font-size:13px; width:100%;">Close without changing</button>
    </div>
</div>

<!-- Export CSV Modal -->
<div id="export-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%;
     background:rgba(44,62,80,0.97); z-index:9999; align-items:center; justify-content:center;">
    <div style="background:white; padding:40px; border-radius:12px; text-align:center; max-width:360px; width:90%;">
        <h2 style="margin:0 0 8px; color:#2c3e50;">Export CSV</h2>
        <p style="color:#666; font-size:14px; margin-bottom:24px;">Enter the password to download.</p>
        <form method="POST" action="/export.csv">
            <input type="password" name="export_password" placeholder="Password"
                style="width:100%; padding:10px 14px; border:1px solid #ddd; border-radius:6px;
                       font-size:15px; box-sizing:border-box; margin-bottom:12px;">
            <button type="submit" style="width:100%; padding:10px; background:#2c3e50; color:white;
                    border:none; border-radius:6px; font-size:15px; cursor:pointer;">Download</button>
        </form>
        <button onclick="document.getElementById('export-modal').style.display='none';"
            style="margin-top:10px; background:none; border:none; color:#999; cursor:pointer; font-size:13px;">Cancel</button>
    </div>
</div>

<!-- Running Search Conflict Modal -->
<div id="search-conflict-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%;
     background:rgba(44,62,80,0.97); z-index:9999; align-items:center; justify-content:center;">
    <div style="background:white; padding:36px; border-radius:12px; max-width:440px; width:90%; text-align:center;">
        <i class="fa-solid fa-circle-exclamation" style="font-size:42px; color:#e67e22; margin-bottom:14px; display:block;"></i>
        <h2 style="margin:0 0 8px; color:#2c3e50;">Search Already Running</h2>
        <p id="sc-message" style="color:#555; font-size:14px; margin-bottom:24px;"></p>
        <div style="display:flex; gap:10px; margin-bottom:10px;">
            <button id="sc-stop-btn" onclick="_srStopAndProceed()"
                style="flex:1; padding:11px; background:#e74c3c; color:white; border:none;
                       border-radius:6px; font-size:14px; font-weight:bold; cursor:pointer;">
                <i class="fa-solid fa-stop"></i> Stop &amp; Start New
            </button>
            <button onclick="_srDismiss()"
                style="flex:1; padding:11px; background:#95a5a6; color:white; border:none;
                       border-radius:6px; font-size:14px; font-weight:bold; cursor:pointer;">
                <i class="fa-solid fa-xmark"></i> Cancel
            </button>
        </div>
    </div>
</div>

<!-- Recheck Terminal — floating panel, shown during individual recheck calls -->
<div id="recheck-terminal" style="display:none; position:fixed; bottom:16px; right:16px; width:480px; max-width:calc(100vw - 32px);
     background:#1e1e1e; border-radius:8px; box-shadow:0 4px 20px rgba(0,0,0,0.5); z-index:8000;
     font-family:monospace; font-size:11px; color:#d4d4d4; overflow:hidden;">
    <div style="background:#2d2d2d; padding:7px 12px; display:flex; align-items:center; gap:8px; border-bottom:1px solid #444;">
        <i class="fa-solid fa-terminal" style="color:#6dbf6d; font-size:12px;"></i>
        <span id="recheck-term-label" style="flex:1; color:#ccc; font-size:11px;"></span>
        <span id="recheck-term-status" style="font-size:10px; color:#888;"></span>
        <button onclick="document.getElementById('recheck-terminal').style.display='none';"
            style="background:none; border:none; color:#888; cursor:pointer; font-size:13px; padding:0 2px; line-height:1;"
            title="Close">&#x2715;</button>
    </div>
    <pre id="recheck-term-output"
        style="margin:0; padding:10px 12px; max-height:300px; overflow-y:auto;
               white-space:pre-wrap; word-break:break-all; color:#d4d4d4;">(waiting for output...)</pre>
</div>

</div><!-- /page-content -->
</body>
</html>
"""


def _write_backup_log(trigger, record_count, local_result, dropbox_result):
    """Append a backup event to backup_log.txt and write to Supabase AuditLog."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] trigger={trigger} records={record_count} local={local_result} dropbox={dropbox_result}\n"
    try:
        with open(BACKUP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[backup_log] write failed: {e}")
    # Also write to Supabase AuditLog
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/AuditLog",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"timestamp": datetime.now(timezone.utc).isoformat(),
                  "action": "backup",
                  "company_name": None,
                  "changes": f"records={record_count} | local={local_result} | dropbox={dropbox_result}",
                  "triggered_by": trigger},
            timeout=10
        )
    except Exception as e:
        print(f"[backup_log] AuditLog write failed: {e}")


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

    search_alive = _search_process_alive()
    search_paused = search_alive and os.path.exists(PAUSE_FLAG)

    try:
        _gh = subprocess.run(["git", "log", "-1", "--format=%h %cd", "--date=format:%d %b %Y %H:%M"],
                             capture_output=True, text=True, cwd=BASE_DIR)
        git_version = _gh.stdout.strip() if _gh.returncode == 0 else "unknown"
    except Exception:
        git_version = "unknown"

    # Read live status for server-side progress bar pre-population
    init_status = {}
    if search_alive and os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                init_status = json.load(f)
        except Exception:
            pass


    # Pre-load search terms for immediate render
    init_terms = _load_terms()

    # Pre-load last log lines for immediate render
    init_log_lines = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                raw = f.readlines()
            init_log_lines = [l.rstrip() for l in raw[-200:]]
        except Exception:
            pass

    return render_template_string(
        HTML_TEMPLATE,
        companies=companies,
        total=total,
        licensed=licensed,
        unlicensed=unlicensed,
        expired=expired,
        unknown=unknown,
        regions=regions,
        nz_regions=NZ_REGIONS,
        message=message,
        message_type=message_type,
        search_running=search_alive,
        search_paused=search_paused,
        schedule_enabled=os.path.exists(SCHEDULE_FLAG),
        init_status=init_status,
        init_terms=init_terms,
        init_log_lines=init_log_lines,
        git_version=git_version,
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
        _launch("searcher.py")
        return redirect(url_for("index", message="Full search started.", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start search: {e}", type="error"))


@app.route("/start-weekly-search", methods=["POST"])
def start_weekly_search():
    try:
        _launch("run_weekly.py")
        return redirect(url_for("index", message="Weekly scan started.", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed: {e}", type="error"))


@app.route("/search-progress")
def search_progress_endpoint():
    from flask import jsonify
    from searcher import get_all_progress
    try:
        return jsonify(get_all_progress())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/start-facebook-search", methods=["POST"])
def start_facebook_search():
    try:
        fresh = request.form.get("fresh") == "1"
        _launch("run_facebook.py", ["--fresh"] if fresh else [])
        return redirect(url_for("index", message="Facebook search started.", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start Facebook search: {e}", type="error"))


@app.route("/start-directory-import", methods=["POST"])
def start_directory_import():
    try:
        fresh = request.form.get("fresh") == "1"
        _launch("run_directories.py", ["--fresh"] if fresh else [])
        return redirect(url_for("index", message="Directory import started (NZSA + LinkedIn).", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start directory import: {e}", type="error"))


@app.route("/dedupe-db", methods=["POST"])
def dedupe_db():
    """Merge duplicate companies into one record, preserving ALL data.
    Groups by: (1) normalised name, (2) same root_domain, (3) same FB URL slug.
    Keeper = record with most valuable data (licensed > filled fields > lowest id).
    Merges all non-null fields from duplicates into keeper — nothing is lost.
    Writes an audit entry for every merge.
    """
    if _search_process_alive():
        return redirect(url_for("index", message="Cannot dedupe while a search is running — stop it first.", type="error"))
    import re as _re
    try:
        from searcher import write_audit
    except Exception:
        write_audit = None

    try:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        patch_headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
        del_headers   = {**headers, "Content-Type": "application/json"}

        # Fetch ALL fields so we can merge without losing anything
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=id.asc&limit=10000",
            headers=headers, timeout=30)
        rows = resp.json()
        if not isinstance(rows, list):
            return redirect(url_for("index", message=f"Dedupe error: unexpected response from Supabase", type="error"))

        # ── Helpers ────────────────────────────────────────────────────
        def norm_name(name):
            """Normalise company name for grouping: lowercase, strip punctuation,
            remove common legal suffixes so 'ABC Security Ltd' == 'ABC Security'."""
            n = (name or "").lower().strip()
            n = _re.sub(r"[^\w\s]", " ", n)
            for suffix in [r"\bnew zealand\b", r"\bnz\b", r"\blimited\b", r"\bltd\b",
                           r"\bl t d\b", r"\bco\b", r"\bllc\b", r"\bpty\b", r"\binc\b"]:
                n = _re.sub(suffix + r"\s*$", "", n)
            return _re.sub(r"\s+", " ", n).strip()

        def norm_fb_slug(url):
            """Extract and normalise the Facebook page slug from a URL."""
            if not url:
                return None
            m = _re.search(r"facebook\.com/([^/?#&]+)", url.lower())
            slug = m.group(1).rstrip("/") if m else None
            return slug if slug and slug not in ("pages", "groups", "profile.php") else None

        # Fields to merge from duplicates into keeper (everything meaningful)
        MERGE_FIELDS = [
            "website_url", "phone", "email", "address",
            "pspla_licensed", "pspla_name", "pspla_address", "pspla_license_number",
            "pspla_license_status", "pspla_license_expiry", "pspla_license_classes",
            "pspla_license_start", "pspla_permit_type", "license_type",
            "match_method", "match_reason",
            "companies_office_name", "companies_office_address", "companies_office_number",
            "nzbn", "co_status", "co_incorporated", "co_website", "individual_license", "director_name",
            "facebook_url", "fb_followers", "fb_phone", "fb_email", "fb_address",
            "fb_description", "fb_category", "fb_rating",
            "fb_alarm_systems", "fb_cctv_cameras", "fb_alarm_monitoring",
            "linkedin_url", "linkedin_followers", "linkedin_description", "linkedin_industry",
            "linkedin_location", "linkedin_website", "linkedin_size",
            "nzsa_member", "nzsa_member_name", "nzsa_accredited", "nzsa_grade",
            "nzsa_contact_name", "nzsa_phone", "nzsa_email", "nzsa_overview",
            "has_alarm_systems", "has_cctv_cameras", "has_alarm_monitoring",
            "google_rating", "google_reviews", "google_phone", "google_address", "google_email",
            "root_domain", "source_url", "notes",
        ]
        # Boolean fields: use OR across group (True from any record wins)
        BOOL_OR_FIELDS = {
            "pspla_licensed", "individual_license",
            "nzsa_member", "nzsa_accredited",
            "has_alarm_systems", "has_cctv_cameras", "has_alarm_monitoring",
            "fb_alarm_systems", "fb_cctv_cameras", "fb_alarm_monitoring",
        }

        def keeper_score(r):
            score = 0
            v = r.get("pspla_licensed")
            if v is True or v == "true":   score += 10
            if r.get("pspla_name"):        score += 3
            if r.get("companies_office_name"): score += 2
            v2 = r.get("nzsa_member")
            if v2 is True or v2 == "true": score += 2
            if r.get("facebook_url"):      score += 1
            if r.get("linkedin_url"):      score += 1
            # Count non-empty fields as a tiebreaker
            for f in MERGE_FIELDS:
                val = r.get(f)
                if val is not None and val != "" and val is not False:
                    score += 0.1
            return score

        def is_empty(val):
            return val is None or val == "" or (isinstance(val, str) and val.lower() in ("none", "null"))

        def merge_group(group, match_type, match_key):
            """Merge a list of duplicate rows. Returns (keeper_id, patch_payload, dup_ids)."""
            group = sorted(group, key=lambda r: (-keeper_score(r), r["id"]))
            keeper = group[0]
            dups   = group[1:]

            patch = {}

            # Regions: merge unique values across group
            seen_regions = []
            seen_lower   = set()
            for r in group:
                for reg in (r.get("region") or "").split(","):
                    reg = reg.strip()
                    if reg and reg.lower() not in seen_lower:
                        seen_regions.append(reg)
                        seen_lower.add(reg.lower())
            merged_region = ", ".join(seen_regions)
            if merged_region != (keeper.get("region") or ""):
                patch["region"] = merged_region

            # date_added: keep the earliest across group
            dates = [r.get("date_added") for r in group if r.get("date_added")]
            if dates:
                earliest = min(dates)
                if earliest != keeper.get("date_added"):
                    patch["date_added"] = earliest

            # Notes: concatenate non-empty notes from dups into keeper
            keeper_notes = (keeper.get("notes") or "").strip()
            extra_notes  = []
            for d in dups:
                dn = (d.get("notes") or "").strip()
                if dn and dn != keeper_notes:
                    extra_notes.append(f"[merged from ID {d['id']}] {dn}")
            if extra_notes:
                combined = (keeper_notes + "\n" + "\n".join(extra_notes)).strip()
                patch["notes"] = combined

            # Per-field merge
            for field in MERGE_FIELDS:
                if field == "notes":
                    continue  # handled above
                if field in BOOL_OR_FIELDS:
                    # True wins — if any record in group has True, keeper gets True
                    for r in group:
                        v = r.get(field)
                        if v is True or v == "true":
                            if not (keeper.get(field) is True or keeper.get(field) == "true"):
                                patch[field] = True
                            break
                else:
                    # First non-empty value wins (keeper first, then dups in score order)
                    if not is_empty(keeper.get(field)):
                        continue  # keeper already has it
                    for d in dups:
                        dv = d.get(field)
                        if not is_empty(dv):
                            patch[field] = dv
                            break

            return keeper, patch, dups

        # ── Grouping passes ────────────────────────────────────────────
        claimed_ids = set()
        groups_to_merge = []  # list of (match_type, match_key, [rows])

        # Pass 1: exact normalised name
        name_map = {}
        for row in rows:
            key = norm_name(row.get("company_name"))
            if key:
                name_map.setdefault(key, []).append(row)
        for key, grp in name_map.items():
            if len(grp) >= 2:
                groups_to_merge.append(("name", key, grp))
                for r in grp:
                    claimed_ids.add(r["id"])

        # Pass 2: same root_domain (unclaimed rows only)
        domain_map = {}
        for row in rows:
            if row["id"] in claimed_ids:
                continue
            dom = (row.get("root_domain") or "").strip().lower()
            if dom and dom not in ("", "none", "null"):
                domain_map.setdefault(dom, []).append(row)
        for dom, grp in domain_map.items():
            if len(grp) >= 2:
                groups_to_merge.append(("domain", dom, grp))
                for r in grp:
                    claimed_ids.add(r["id"])

        # Pass 3: same Facebook URL slug (unclaimed rows only)
        fb_map = {}
        for row in rows:
            if row["id"] in claimed_ids:
                continue
            slug = norm_fb_slug(row.get("facebook_url"))
            if slug:
                fb_map.setdefault(slug, []).append(row)
        for slug, grp in fb_map.items():
            if len(grp) >= 2:
                groups_to_merge.append(("facebook", slug, grp))
                for r in grp:
                    claimed_ids.add(r["id"])

        if not groups_to_merge:
            return redirect(url_for("index", message="No duplicates found.", type="success"))

        # ── Merge and delete ───────────────────────────────────────────
        deleted_count = 0
        group_count   = len(groups_to_merge)

        for match_type, match_key, group in groups_to_merge:
            keeper, patch, dups = merge_group(group, match_type, match_key)
            dup_desc = ", ".join(f"ID {d['id']} ({d.get('company_name','')})" for d in dups)
            fields_merged = [k for k in patch if k not in ("region", "date_added")]

            # Patch keeper with merged data
            if patch:
                requests.patch(
                    f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{keeper['id']}",
                    headers=patch_headers, json=patch, timeout=15)

            # Audit entry on the keeper
            audit_note = (
                f"Dedupe ({match_type} match: '{match_key}'): "
                f"merged {len(dups)} duplicate(s) into this record — "
                f"deleted: {dup_desc}. "
                + (f"Fields enriched from duplicates: {', '.join(fields_merged)}." if fields_merged else "No new fields needed.")
            )
            if write_audit:
                write_audit("updated", str(keeper["id"]), keeper.get("company_name", ""),
                            changes=audit_note, triggered_by="manual-dedupe")

            # Delete duplicates
            for dup in dups:
                requests.delete(
                    f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{dup['id']}",
                    headers=del_headers, timeout=15)
                deleted_count += 1

        msg = (f"Deduplication complete — {deleted_count} duplicate(s) removed across "
               f"{group_count} group(s). All data preserved in the surviving records.")
        return redirect(url_for("index", message=msg, type="success"))

    except Exception as e:
        import traceback as _tb
        print(f"  [Dedupe error] {e}\n{_tb.format_exc()}")
        return redirect(url_for("index", message=f"Dedupe error: {e}", type="error"))


@app.route("/backup-db", methods=["POST"])
def backup_db():
    companies = get_companies()
    if not companies:
        return redirect(url_for("index", message="Nothing to back up — database is empty.", type="error"))

    fields = sorted(companies[0].keys())
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for c in companies:
        writer.writerow({f: c.get(f, "") or "" for f in fields})
    csv_data = output.getvalue()

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"pspla_backup_{timestamp}.csv"
    notes = []
    local_result = "skipped"
    dropbox_result = "skipped"

    # Local backup
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    try:
        with open(os.path.join(backup_dir, filename), "w", encoding="utf-8", newline="") as f:
            f.write(csv_data)
        local_result = f"backups/{filename}"
        notes.append(f"Local: {local_result}")
    except Exception as e:
        local_result = f"FAILED: {e}"
        notes.append(f"Local FAILED: {e}")

    # Dropbox
    if DROPBOX_TOKEN:
        try:
            resp = requests.post(
                "https://content.dropboxapi.com/2/files/upload",
                headers={
                    "Authorization": f"Bearer {DROPBOX_TOKEN}",
                    "Dropbox-API-Arg": json.dumps({
                        "path": f"/pspla-backup/{filename}",
                        "mode": "add",
                        "autorename": True,
                        "mute": False
                    }),
                    "Content-Type": "application/octet-stream"
                },
                data=csv_data.encode("utf-8"),
                timeout=30
            )
            if resp.status_code == 200:
                dropbox_result = f"/Apps/pspla-backup/{filename}"
                notes.append(f"Dropbox: {dropbox_result}")
            else:
                dropbox_result = f"FAILED: {resp.text[:200]}"
                notes.append(f"Dropbox FAILED: {resp.text[:200]}")
        except Exception as e:
            dropbox_result = f"FAILED: {e}"
            notes.append(f"Dropbox FAILED: {e}")
    else:
        dropbox_result = "no token"
        notes.append("Dropbox token not set — skipped")

    _write_backup_log("manual", len(companies), local_result, dropbox_result)
    msg = f"Backup complete ({len(companies)} records). " + " | ".join(notes)
    return redirect(url_for("index", message=msg, type="success"))


@app.route("/clear-db", methods=["POST"])
def clear_db():
    # Password check
    if EXPORT_PASSWORD and request.form.get("clear_password") != EXPORT_PASSWORD:
        return redirect(url_for("index", message="Incorrect password — database not cleared.", type="error"))

    companies = get_companies()
    if not companies:
        return redirect(url_for("index", message="Database is already empty.", type="error"))

    # Build CSV — use all columns returned from Supabase
    fields = sorted(companies[0].keys())
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for c in companies:
        writer.writerow({f: c.get(f, "") or "" for f in fields})
    csv_data = output.getvalue()

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"pspla_backup_{timestamp}.csv"
    backup_notes = []
    local_result = "skipped"
    dropbox_result = "skipped"

    # Save local backup
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    try:
        with open(os.path.join(backup_dir, filename), "w", encoding="utf-8", newline="") as f:
            f.write(csv_data)
        local_result = f"backups/{filename}"
        backup_notes.append(f"Local: {local_result}")
    except Exception as e:
        local_result = f"FAILED: {e}"
        backup_notes.append(f"Local FAILED: {e}")

    # Upload to Dropbox
    if DROPBOX_TOKEN:
        try:
            resp = requests.post(
                "https://content.dropboxapi.com/2/files/upload",
                headers={
                    "Authorization": f"Bearer {DROPBOX_TOKEN}",
                    "Dropbox-API-Arg": json.dumps({
                        "path": f"/pspla-backup/{filename}",
                        "mode": "add",
                        "autorename": True,
                        "mute": False
                    }),
                    "Content-Type": "application/octet-stream"
                },
                data=csv_data.encode("utf-8"),
                timeout=30
            )
            if resp.status_code == 200:
                dropbox_result = f"/Apps/pspla-backup/{filename}"
                backup_notes.append(f"Dropbox: {dropbox_result}")
            else:
                dropbox_result = f"FAILED: {resp.text[:200]}"
                backup_notes.append(f"Dropbox FAILED: {resp.text[:200]}")
        except Exception as e:
            dropbox_result = f"FAILED: {e}"
            backup_notes.append(f"Dropbox FAILED: {e}")
    else:
        dropbox_result = "no token"
        backup_notes.append("Dropbox token not set — skipped")

    _write_backup_log("clear-db", len(companies), local_result, dropbox_result)

    # Delete from Supabase
    try:
        del_url = f"{SUPABASE_URL}/rest/v1/Companies?id=not.is.null"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        response = requests.delete(del_url, headers=headers)
        if response.status_code in [200, 204]:
            for path in [PROGRESS_FILE, RUNNING_FLAG, PAUSE_FLAG, PID_FILE, START_FILE]:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            msg = f"Database cleared ({len(companies)} records). " + " | ".join(backup_notes)
            return redirect(url_for("index", message=msg, type="success"))
        else:
            return redirect(url_for("index", message=f"Backup done but delete failed: {response.text[:200]}", type="error"))
    except Exception as e:
        return redirect(url_for("index", message=f"Backup done but error during delete: {e}", type="error"))


@app.route("/export.csv", methods=["POST"])
def export_csv():
    if EXPORT_PASSWORD and request.form.get("export_password") != EXPORT_PASSWORD:
        return redirect(url_for("index", message="Incorrect export password.", type="error"))
    companies = get_companies()
    fields = sorted(companies[0].keys()) if companies else []
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


@app.route("/search-running-info")
def search_running_info():
    """Lightweight endpoint: is a search running, what type, for how long?"""
    from flask import jsonify
    running = _search_process_alive()
    if not running:
        return jsonify({"running": False})
    _type_labels = {
        "full": "Full Search", "google-weekly": "Weekly Scan",
        "facebook": "Facebook Search", "directories": "Directory Import",
        "google-partial": "Partial Search", "bulk-recheck": "Bulk Recheck",
    }
    search_type = "search"
    started_iso = None
    minutes = 0
    if os.path.exists(START_FILE):
        try:
            with open(START_FILE) as f:
                start_data = json.load(f)
            search_type = start_data.get("type", "search")
            started_iso = start_data.get("started")
            if started_iso:
                started = datetime.fromisoformat(started_iso)
                elapsed = datetime.now(timezone.utc) - started
                minutes = int(elapsed.total_seconds() / 60)
        except Exception:
            pass
    return jsonify({
        "running": True,
        "type": search_type,
        "type_label": _type_labels.get(search_type, search_type.replace("-", " ").title()),
        "minutes": minutes,
        "started": started_iso,
    })


@app.route("/search-status")
def search_status():
    global _was_running
    running = _search_process_alive()
    paused = running and os.path.exists(PAUSE_FLAG)
    # Crash detection: if we were running last poll but not any more, mark stale "running" entries
    if _was_running and not running:
        _detect_and_mark_crashes()
    _was_running = running
    status = {"running": running, "paused": paused}
    if running and os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                status.update(json.load(f))
        except Exception:
            pass
    if running and os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            status["log_lines"] = [l.rstrip() for l in lines[-200:]]
        except Exception as e:
            status["log_lines"] = [f"[log read error: {e}]"]
    try:
        from searcher import get_llm_status, get_token_usage
        llm_errors = get_llm_status()
        if llm_errors >= 3:
            status["llm_warning"] = f"LLM API appears unavailable ({llm_errors} consecutive failures). Matches are being saved as low-confidence. Check Anthropic API key / credit balance."
        status["tokens"] = get_token_usage()
    except Exception:
        pass
    from flask import jsonify
    return jsonify(status)


@app.route("/api-credits")
def api_credits():
    from flask import jsonify
    result = {}

    # SerpAPI — fetch account info
    if SERPAPI_KEY:
        try:
            r = requests.get(
                "https://serpapi.com/account",
                params={"api_key": SERPAPI_KEY},
                timeout=6,
            )
            if r.ok:
                data = r.json()
                result["serp_searches_left"]  = data.get("plan_searches_left")
                result["serp_searches_month"] = data.get("searches_per_month")
                result["serp_this_month"]     = data.get("this_month_usage")
            else:
                result["serp_error"] = f"HTTP {r.status_code}"
        except Exception as e:
            result["serp_error"] = str(e)
    else:
        result["serp_error"] = "SERPAPI_KEY not set"

    # Claude token usage — from current searcher session
    try:
        from searcher import get_token_usage
        result["tokens"] = get_token_usage()
    except Exception as e:
        result["tokens"] = {"error": str(e)}

    return jsonify(result)


@app.route("/search-log")
def search_log():
    from flask import jsonify
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": []})
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in lines[-200:]]})
    except Exception as e:
        return jsonify({"lines": [f"[log error: {e}]"]})


@app.route("/recheck-log")
def recheck_log():
    from flask import jsonify
    try:
        if not os.path.exists(RECHECK_LOG_FILE):
            return jsonify({"lines": []})
        with open(RECHECK_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in lines[-300:]]})
    except Exception as e:
        return jsonify({"lines": [f"[recheck log error: {e}]"]})


@app.route("/open-terminal", methods=["POST"])
def open_terminal():
    from flask import jsonify
    try:
        log_path = LOG_FILE
        ps_cmd = f"Get-Content -Path '{log_path}' -Wait -Tail 80"
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", ps_cmd],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/search-terms")
def get_search_terms():
    from flask import jsonify
    return jsonify(_load_terms())


@app.route("/save-terms", methods=["POST"])
def save_terms():
    from flask import jsonify
    try:
        data = request.get_json()
        existing = _load_terms()
        if "google" in data:
            existing["google"] = [t.strip() for t in data["google"] if t.strip()]
        if "facebook" in data:
            existing["facebook"] = [t.strip() for t in data["facebook"] if t.strip()]
        with open(TERMS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/start-partial-search", methods=["POST"])
def start_partial_search():
    from flask import jsonify
    if os.path.exists(RUNNING_FLAG):
        return jsonify({"ok": False, "error": "A search is already running."}), 409
    try:
        data = request.get_json()
        config = {
            "regions": data.get("regions", []),
            "google_terms": data.get("google_terms", []),
            "include_facebook": bool(data.get("include_facebook", False)),
            "include_nationwide": bool(data.get("include_nationwide", False)),
        }
        fresh = bool(data.get("fresh", False))
        with open(PARTIAL_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        _launch("run_partial.py", ["--fresh"] if fresh else [])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/start-bulk-recheck", methods=["POST"])
def start_bulk_recheck():
    from flask import jsonify
    if os.path.exists(RUNNING_FLAG):
        return jsonify({"ok": False, "error": "A search is already running."}), 409
    data = request.get_json(silent=True) or {}
    checks = data.get("checks", [])
    company_ids = data.get("company_ids", "all")
    if not checks:
        return jsonify({"error": "No checks selected"}), 400
    config = {"checks": checks, "company_ids": company_ids}
    with open(RECHECK_CONFIG_FILE, "w") as f:
        json.dump(config, f)
    _launch("run_recheck.py")
    scope = "all companies" if company_ids == "all" else f"{len(company_ids)} selected companies"
    return jsonify({"ok": True, "message": f"Bulk recheck started for {scope}"})


@app.route("/search-history-data")
def search_history_data():
    from flask import jsonify
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass
    # If no process is running, any "running" entry is a crash remnant
    if not _search_process_alive():
        changed = False
        for entry in history:
            if entry.get("status") == "running":
                entry["status"] = "crashed"
                entry["finished"] = entry.get("finished") or datetime.now(timezone.utc).isoformat()
                if not entry.get("notes"):
                    entry["notes"] = "Process exited without writing a completion record. Check search_log.txt for details."
                changed = True
        if changed:
            try:
                with open(HISTORY_FILE, "w") as f:
                    json.dump(history, f, indent=2)
            except Exception:
                pass
    return jsonify(history)


SEARCH_HISTORY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Search History — PSPLA Checker</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" referrerpolicy="no-referrer" />
    <style>
        * { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f4f4f4; }
        .page-header { background: #2c3e50; color: white; padding: 14px 24px;
                       display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .page-header h1 { margin: 0; font-size: 18px; }
        .content { padding: 24px; }
        .back { color: #aac; text-decoration: none; font-size: 13px; }
        .back:hover { color: white; }
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px;
                overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); font-size: 13px; }
        th { background: #2c3e50; color: white; padding: 10px 14px; text-align: left; white-space: nowrap; }
        th.right { text-align: right; }
        td { padding: 9px 14px; border-bottom: 1px solid #eee; vertical-align: middle; }
        td.right { text-align: right; }
        tr:hover td { background: #f9f9f9; }
        .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; white-space: nowrap; }
        .stat-row { display: flex; gap: 15px; margin-bottom: 24px; flex-wrap: wrap; }
        .stat-box { background: white; padding: 14px 20px; border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; min-width: 120px; }
        .stat-box h2 { margin: 0; font-size: 1.8em; color: #2c3e50; }
        .stat-box p { margin: 4px 0 0; color: #888; font-size: 12px; }
        .filter-row { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
        .filter-row select, .filter-row input {
            padding: 7px 11px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
    </style>
</head>
<body>
<div class="page-header">
    <h1><i class="fa-solid fa-clock-rotate-left"></i> Search History</h1>
    <a href="/" class="back"><i class="fa-solid fa-arrow-left"></i> Back to Dashboard</a>
</div>
<div class="content">
    <div class="stat-row" id="stats"></div>
    <div class="filter-row">
        <select id="typeFilter" onchange="renderTable()">
            <option value="">All Types</option>
            <option value="full">Full</option>
            <option value="google-weekly">Weekly</option>
            <option value="facebook">Facebook</option>
            <option value="google-partial">Partial</option>
            <option value="directories">Directories</option>
            <option value="bulk-recheck">Bulk Recheck</option>
        </select>
        <select id="statusFilter" onchange="renderTable()">
            <option value="">All Statuses</option>
            <option value="completed">Completed</option>
            <option value="running">Running</option>
            <option value="crashed">Crashed</option>
            <option value="stopped">Stopped</option>
            <option value="error">Error</option>
        </select>
        <input type="text" id="searchBox" placeholder="Search..." oninput="renderTable()" style="min-width:180px;">
    </div>
    <table>
        <thead>
            <tr>
                <th>Date (NZT)</th>
                <th>Type</th>
                <th>Triggered by</th>
                <th class="right">Duration</th>
                <th class="right">Found</th>
                <th class="right">New</th>
                <th>Status</th>
                <th>Notes</th>
            </tr>
        </thead>
        <tbody id="tableBody"><tr><td colspan="8" style="text-align:center;color:#aaa;padding:30px;">Loading...</td></tr></tbody>
    </table>
</div>
<script>
var TYPE_LABELS   = {full:'Full','google-weekly':'Weekly',facebook:'Facebook','google-partial':'Partial',directories:'Directories','bulk-recheck':'Bulk Recheck'};
var STATUS_COLORS = {completed:'#27ae60',stopped:'#e67e22',error:'#e74c3c',running:'#e67e22',crashed:'#c0392b'};
var _allRows = [];

function fmt(iso) {
    if (!iso) return '-';
    var d = new Date(iso.replace('+00:00','Z'));
    return d.toLocaleDateString('en-NZ',{day:'2-digit',month:'short',year:'numeric'})
         + ' ' + d.toLocaleTimeString('en-NZ',{hour:'2-digit',minute:'2-digit'});
}

function renderStats(rows) {
    var total = rows.length, totalNew = 0, totalFound = 0;
    rows.forEach(function(r){ totalNew += (r.total_new||0); totalFound += (r.total_found||0); });
    document.getElementById('stats').innerHTML =
        '<div class="stat-box"><h2>' + total + '</h2><p>Total Runs</p></div>' +
        '<div class="stat-box"><h2>' + totalFound.toLocaleString() + '</h2><p>Total Found</p></div>' +
        '<div class="stat-box"><h2>' + totalNew.toLocaleString() + '</h2><p>Total New Added</p></div>';
}

function renderTable() {
    var type   = document.getElementById('typeFilter').value;
    var status = document.getElementById('statusFilter').value;
    var search = document.getElementById('searchBox').value.toLowerCase();
    var rows = _allRows.filter(function(r) {
        return (!type   || r.type === type)
            && (!status || r.status === status)
            && (!search || (r.type||'').toLowerCase().includes(search)
                        || (r.status||'').toLowerCase().includes(search)
                        || (r.triggered_by||'').toLowerCase().includes(search));
    });
    if (!rows.length) {
        document.getElementById('tableBody').innerHTML =
            '<tr><td colspan="8" style="text-align:center;color:#aaa;padding:30px;">No records match.</td></tr>';
        return;
    }
    var html = '';
    rows.forEach(function(r, idx) {
        var col = STATUS_COLORS[r.status] || '#888';
        var lbl = TYPE_LABELS[r.type] || r.type;
        var dur = r.duration_minutes ? r.duration_minutes + ' min' : (r.status === 'running' ? '(running...)' : '-');
        var notes = r.notes || '';
        var notesTd = '';
        var notesRow = '';
        if (notes) {
            var short = notes.length > 60 ? notes.substring(0, 60) + '...' : notes;
            notesTd = '<td style="max-width:220px;color:#888;font-size:11px;" title="' + notes.replace(/"/g,'&quot;') + '">'
                + '<span>' + short.replace(/</g,'&lt;') + '</span>'
                + (notes.length > 60 ? ' <a href="#" onclick="toggleNotes(event,' + idx + ');return false;" style="color:#3498db;font-size:10px;">[expand]</a>' : '')
                + '</td>';
            if (notes.length > 60) {
                notesRow = '<tr id="notes-' + idx + '" style="display:none;">'
                    + '<td colspan="8" style="background:#fff8f8;padding:10px 14px;font-size:11px;color:#555;white-space:pre-wrap;font-family:monospace;">'
                    + notes.replace(/</g,'&lt;') + '</td></tr>';
            }
        } else {
            notesTd = '<td></td>';
        }
        html += '<tr style="' + (r.status==='crashed'?'background:#fff5f5;':r.status==='running'?'background:#fffaf0;':'') + '">'
            + '<td>' + fmt(r.started) + '</td>'
            + '<td>' + lbl + '</td>'
            + '<td style="color:#888;">' + (r.triggered_by||'-') + '</td>'
            + '<td class="right" style="color:#888;">' + dur + '</td>'
            + '<td class="right">' + (r.total_found||0).toLocaleString() + '</td>'
            + '<td class="right" style="font-weight:bold;">' + (r.total_new||0).toLocaleString() + '</td>'
            + '<td><span class="badge" style="background:' + col + '20;color:' + col + ';">' + r.status + '</span></td>'
            + notesTd
            + '</tr>' + notesRow;
    });
    document.getElementById('tableBody').innerHTML = html;
}

function toggleNotes(e, idx) {
    var row = document.getElementById('notes-' + idx);
    if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}

fetch('/search-history-data')
    .then(function(r){ return r.json(); })
    .then(function(data) {
        _allRows = data;
        renderStats(data);
        renderTable();
    })
    .catch(function(){ document.getElementById('tableBody').innerHTML =
        '<tr><td colspan="8" style="text-align:center;color:#e74c3c;padding:30px;">Failed to load history.</td></tr>'; });
</script>
</body>
</html>"""


@app.route("/search-history")
def search_history():
    return render_template_string(SEARCH_HISTORY_TEMPLATE)


@app.route("/audit-log-data")
def audit_log_data():
    from flask import jsonify
    limit = request.args.get("limit", 1000)
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/AuditLog?select=*&order=timestamp.desc&limit={limit}",
        headers=headers, timeout=15
    )
    return jsonify(resp.json() if resp.ok else [])


@app.route("/company-ai-decisions")
def company_ai_decisions():
    from flask import jsonify
    company_name = request.args.get("name", "")
    if not company_name:
        return jsonify([])
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    encoded = requests.utils.quote(company_name)
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/AuditLog"
        f"?select=*&action=eq.llm_decision&company_name=eq.{encoded}"
        f"&order=timestamp.asc",
        headers=headers, timeout=10
    )
    return jsonify(resp.json() if resp.ok else [])


@app.route("/audit-log")
def audit_log_page():
    return render_template_string(AUDIT_LOG_TEMPLATE)


@app.route("/llm-log")
def llm_log_page():
    log_path = os.path.join(BASE_DIR, "llm_debug.log")
    try:
        content = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    except Exception as e:
        content = f"Error reading log: {e}"
    all_entries = _parse_llm_log(content)
    total_count = len(all_entries)
    entries = all_entries[-100:]  # show last 100 only
    return render_template_string("""<!DOCTYPE html>
<html>
<head>
<title>LLM Debug Log</title>
<style>
  body { font-family: monospace; background:#1a1a2e; color:#e0e0e0; margin:0; padding:0; }
  .toolbar { background:#111; padding:12px 20px; display:flex; align-items:center; gap:16px; position:sticky; top:0; z-index:10; border-bottom:1px solid #333; }
  .toolbar a { color:#aaa; text-decoration:none; font-size:13px; }
  .toolbar a:hover { color:white; }
  h1 { color:#27ae60; margin:0; font-size:18px; }
  .controls { display:flex; gap:10px; align-items:center; margin-left:auto; }
  input[type=text] { background:#222; border:1px solid #444; color:white; padding:5px 10px; border-radius:4px; font-size:13px; width:250px; }
  button { background:#27ae60; color:white; border:none; padding:5px 12px; border-radius:4px; cursor:pointer; font-size:13px; }
  button.danger { background:#c0392b; }
  .log { padding:20px; white-space:pre-wrap; font-size:12px; line-height:1.6; }
  .entry { border:1px solid #333; border-radius:6px; margin-bottom:16px; overflow:hidden; }
  .entry-header { background:#222; padding:8px 14px; color:#27ae60; font-weight:bold; font-size:12px; }
  .entry-prompt { background:#1a1a1a; padding:12px 14px; color:#ccc; border-top:1px solid #2a2a2a; }
  .entry-response { background:#0d1a0d; padding:12px 14px; color:#7fff7f; border-top:1px solid #2a2a2a; }
  .label { color:#888; font-size:11px; margin-bottom:4px; }
  .empty { color:#666; text-align:center; padding:60px; font-size:14px; }
  mark { background:#5a4000; color:#ffe; border-radius:2px; }
</style>
</head>
<body>
<div class="toolbar">
  <h1>&#x1F916; LLM Debug Log</h1>
  <span style="color:#888; font-size:12px;">Showing last {{ entries|length }} of {{ total_count }} entries</span>
  <div class="controls">
    <input type="text" id="search" placeholder="Filter entries..." oninput="filterEntries()">
    <button onclick="scrollToBottom()">&#x2193; Latest</button>
    <form method="POST" action="/llm-log/clear" style="margin:0;" onsubmit="return confirm('Clear the log file?')">
      <button class="danger" type="submit">&#x1F5D1; Clear Log</button>
    </form>
    <a href="/">&#x2190; Dashboard</a>
  </div>
</div>
<div class="log" id="log">
{% if entries %}
  {% for e in entries %}
  <div class="entry" data-text="{{ e.header }} {{ e.prompt }} {{ e.response }}">
    <div class="entry-header">{{ e.header }}</div>
    <div class="entry-prompt"><div class="label">PROMPT</div>{{ e.prompt }}</div>
    <div class="entry-response"><div class="label">RESPONSE</div>{{ e.response }}</div>
  </div>
  {% endfor %}
{% else %}
  <div class="empty">No LLM calls logged yet. Run a search or use a Re-check button to generate entries.</div>
{% endif %}
</div>
<script>
function filterEntries() {
  var q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.entry').forEach(function(el) {
    el.style.display = !q || el.dataset.text.toLowerCase().includes(q) ? '' : 'none';
  });
}
function scrollToBottom() {
  window.scrollTo(0, document.body.scrollHeight);
}
// Auto-scroll to bottom on load (latest entries)
window.onload = function() { scrollToBottom(); };
</script>
</body>
</html>""", entries=entries, total_count=total_count)


def _parse_llm_log(content):
    """Parse llm_debug.log into a list of {header, prompt, response} dicts."""
    entries = []
    if not content.strip():
        return entries
    blocks = content.split("\n" + "="*80)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        header = lines[0].strip() if lines else ""
        prompt, response = "", ""
        section = None
        buf = []
        for line in lines[1:]:
            if "─" in line and "PROMPT" in line:
                section = "prompt"; buf = []
            elif "─" in line and "RESPONSE" in line:
                if section == "prompt":
                    prompt = "\n".join(buf).strip()
                section = "response"; buf = []
            else:
                buf.append(line)
        if section == "response":
            response = "\n".join(buf).strip()
        if header:
            entries.append({"header": header, "prompt": prompt, "response": response})
    return entries


@app.route("/llm-log/clear", methods=["POST"])
def llm_log_clear():
    log_path = os.path.join(BASE_DIR, "llm_debug.log")
    try:
        open(log_path, "w").close()
    except Exception:
        pass
    return redirect("/llm-log")


AUDIT_LOG_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Audit Log</title>
<style>
body { font-family: Arial, sans-serif; font-size: 13px; padding: 20px; background: #f5f5f5; }
h1 { color: #2c3e50; margin-bottom: 6px; }
.back { margin-bottom: 16px; display: inline-block; color: #2980b9; text-decoration: none; }
.controls { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.controls input, .controls select { padding: 5px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
.controls input { width: 220px; }
.controls button { padding: 5px 14px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; cursor: pointer; background: white; }
.count { color: #888; font-size: 12px; }
table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.08); }
th { background: #ecf0f1; text-align: left; padding: 8px 10px; font-size: 12px; font-weight: 600; color: #555; border-bottom: 2px solid #ddd; }
td { padding: 7px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #fafafa; }
.badge { display: inline-block; border-radius: 3px; padding: 1px 8px; font-size: 11px; color: white; font-weight: bold; }
.badge-added { background: #27ae60; }
.badge-updated { background: #2980b9; }
.badge-deleted { background: #e74c3c; }
.badge-email { background: #8e44ad; }
.badge-correction { background: #d35400; }
.ts { color: #888; white-space: nowrap; }
.changes { color: #555; max-width: 380px; }
.tby { color: #999; }
.co { font-weight: 600; }
#loading { color: #aaa; padding: 20px 0; }
</style>
</head>
<body>
<a class="back" href="/">&#8592; Back to dashboard</a>
<h1>&#x1F4CB; Audit Log</h1>
<div class="controls">
  <input id="filterName" type="text" placeholder="Filter by company..." oninput="render()">
  <select id="filterAction" onchange="render()">
    <option value="">All actions</option>
    <option value="added">Added</option>
    <option value="updated">Updated</option>
    <option value="deleted">Deleted</option>
    <option value="email">Email</option>
    <option value="correction">Correction</option>
  </select>
  <input id="filterDate" type="date" onchange="render()" title="Filter by date">
  <button onclick="load()">&#x21BA; Refresh</button>
  <span class="count" id="countLabel"></span>
</div>
<div id="loading">Loading...</div>
<table id="auditTable" style="display:none">
  <thead>
    <tr>
      <th style="width:140px">Time (NZ)</th>
      <th style="width:80px">Action</th>
      <th>Company</th>
      <th>Changes</th>
      <th style="width:150px">Triggered By</th>
    </tr>
  </thead>
  <tbody id="auditBody"></tbody>
</table>
<script>
var _data = [];
function load() {
  document.getElementById('loading').style.display = '';
  document.getElementById('auditTable').style.display = 'none';
  fetch('/audit-log-data?limit=2000').then(function(r){return r.json();}).then(function(d){
    _data = d;
    render();
    document.getElementById('loading').style.display = 'none';
    document.getElementById('auditTable').style.display = '';
  }).catch(function(){
    document.getElementById('loading').textContent = 'Failed to load.';
  });
}
function render() {
  var name = document.getElementById('filterName').value.toLowerCase();
  var action = document.getElementById('filterAction').value;
  var date = document.getElementById('filterDate').value;
  var rows = _data.filter(function(r) {
    if (name && !(r.company_name||'').toLowerCase().includes(name)) return false;
    if (action && r.action !== action) return false;
    if (date && !(r.timestamp||'').startsWith(date)) return false;
    return true;
  });
  document.getElementById('countLabel').textContent = rows.length + ' of ' + _data.length + ' entries';
  var html = '';
  var nzOffset = 13 * 60;
  rows.forEach(function(r) {
    var ts = '-';
    if (r.timestamp) {
      var d = new Date(r.timestamp);
      ts = d.toLocaleString('en-NZ', {timeZone:'Pacific/Auckland',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
    }
    var badgeCls = 'badge-' + (r.action||'');
    var notes = r.notes ? '<br><small style="color:#aaa">' + escHtml(r.notes) + '</small>' : '';
    html += '<tr>'
      + '<td class="ts">' + ts + '</td>'
      + '<td><span class="badge ' + badgeCls + '">' + escHtml(r.action||'') + '</span></td>'
      + '<td class="co">' + escHtml(r.company_name||'-') + '</td>'
      + '<td class="changes">' + escHtml(r.changes||'') + notes + '</td>'
      + '<td class="tby">' + escHtml(r.triggered_by||'') + '</td>'
      + '</tr>';
  });
  document.getElementById('auditBody').innerHTML = html || '<tr><td colspan="5" style="color:#aaa;padding:20px">No entries match.</td></tr>';
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
load();
</script>

<!-- ── Claude Code Context Panel ──────────────────────────────────────────── -->
<div id="claude-context-panel" style="
    position:fixed; bottom:18px; right:18px; z-index:9999;
    font-family:monospace; font-size:12px;">
  <button onclick="document.getElementById('claude-context-box').style.display=
      document.getElementById('claude-context-box').style.display==='none'?'block':'none'"
      style="padding:5px 12px; background:#2c3e50; color:#ecf0f1; border:none;
             border-radius:6px; cursor:pointer; font-size:12px; box-shadow:0 2px 6px rgba(0,0,0,0.3);">
    🤖 Claude context
  </button>
  <div id="claude-context-box" style="display:none; position:absolute; bottom:36px; right:0;
       width:560px; max-height:70vh; overflow-y:auto; background:#1e2a38; color:#ecf0f1;
       border-radius:8px; padding:14px 16px; box-shadow:0 4px 20px rgba(0,0,0,0.5);
       white-space:pre-wrap; line-height:1.5;">
<button onclick="
  navigator.clipboard.writeText(document.getElementById('claude-context-text').innerText)
  .then(()=>{this.textContent='✓ Copied!';setTimeout(()=>this.textContent='📋 Copy all',1500)})
" style="float:right;margin-bottom:8px;padding:3px 10px;background:#3498db;color:white;
         border:none;border-radius:4px;cursor:pointer;font-size:11px;">📋 Copy all</button>
<div id="claude-context-text">PSPLA Checker — Claude Code context (paste this at the start of a new session)

PROJECT: Automated NZ security company licence checker. Finds NZ security companies
via Google/Facebook/NZSA/LinkedIn, checks each against PSPLA licence register,
stores results in Supabase. Flask dashboard for browsing, managing, correcting results.
Owner: Wade. Location: C:\\Users\\WadeAdmin\\pspla-checker\\

READ FIRST: CLAUDE.md in the project root — full pipeline, AI functions, DB schema,
design patterns, common tasks.

KEY FILES:
- searcher.py        Core engine: search, scrape, match, verify, save (~3500+ lines)
- dashboard.py       Flask web UI + APScheduler + all API endpoints
- run_weekly.py      Full Google search entry point (all regions x all terms)
- run_facebook.py    Facebook-only search entry point
- run_directories.py NZSA + LinkedIn directory import entry point
- run_partial.py     Partial/targeted search (reads partial_config.json)
- corrections.json   Blocked false-positive matches (checked before every PSPLA accept)
- lessons.json       LLM-generated rules from past corrections (injected into verify prompts)
- search_terms.json  Editable search terms (Google + Facebook)
- .env               ANTHROPIC_API_KEY, SERPAPI_KEY, SUPABASE_URL, SUPABASE_KEY, SMTP_*

PIPELINE: Google/FB search → scrape website → extract company info (Haiku LLM)
→ scrape Facebook page (3-tier: snippet cache / og:meta / mobile fallback)
→ Companies Office check (Google, parses CO status + directors + incorporation date)
→ PSPLA check (4 strategies: name variants → keywords → single keyword → Haiku suggests)
→ verify match (Sonnet: hard pre-check + lessons injection + LLM decision → audit log)
→ deep verify if low/medium confidence (Sonnet with all context)
→ NZSA check → cross-check all sources (Haiku) → save to Supabase

AI FUNCTIONS (all use Anthropic SDK, all fail gracefully):
- extract_company_info()        Haiku — name/region from scraped page text
- _llm_suggest_pspla_names()    Haiku — PSPLA search terms when strategies 1-3 fail
- verify_pspla_match()          Sonnet — is PSPLA result the same company?
- _llm_deep_verify()            Sonnet — full-context verify for low/medium confidence
- _llm_cross_check_sources()    Haiku — consistency check across PSPLA/CO/NZSA/FB
- parse_and_save_correction()   Haiku — parse user correction into structured JSON
- _generate_and_save_lesson()   Haiku — create rule from false positive correction

PSPLA API: https://forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select
Solr fields: name_txt, permitStatus_s, permitNumber_txt, permitEndDate_s,
  permitStartDates_s, permitTempOrPerm_s, isIndividual_b, registeredOffice_txt,
  securityTechnician_s, monitoringOfficer_s, propertyGuard_s, crowdController_s,
  personalGuard_s, privateInvestigator_s, repossessionAgent_s, securityConsultant_s

KEY PATTERNS:
- RECORD_TEMPLATE = all DB columns with None defaults; check_schema() validates at startup
- running.flag/pause.flag = file IPC; both deleted on dashboard startup + stop
- LLM failure → low-confidence acceptance (not rejection) so search keeps running
- CO "Removed" → successor keyword search → retry PSPLA with successor name
- _FB_SNIPPET_CACHE: snippet from SerpAPI stored when FB URL found; used by scrape tier 1
- All LLM calls write to AuditLog (action=llm_decision); viewable per company on dashboard
- corrections.json blocks specific company→PSPLA pairs; lessons.json injects rules into prompts
- __main__ guards on all run_*.py prevent accidental import-triggered searches
- Git repo; dashboard has Rollback button; commit after every significant change session</div>
  </div>
</div>

</body>
</html>
"""


@app.route("/toggle-schedule", methods=["POST"])
def toggle_schedule():
    if os.path.exists(SCHEDULE_FLAG):
        os.remove(SCHEDULE_FLAG)
        msg = "Scheduled searches disabled."
    else:
        open(SCHEDULE_FLAG, "w").close()
        msg = "Scheduled searches enabled."
    return redirect(url_for("index", message=msg, type="success"))


@app.route("/pause-search", methods=["POST"])
def pause_search():
    open(PAUSE_FLAG, "w").close()
    return redirect(url_for("index", message="Search paused — it will stop after the current company.", type="success"))


@app.route("/resume-search", methods=["POST"])
def resume_search():
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    return redirect(url_for("index", message="Search resumed.", type="success"))


def _write_stopped_history():
    """Write a 'stopped' history entry using search_start.json + current STATUS_FILE."""
    try:
        if not os.path.exists(START_FILE):
            return
        with open(START_FILE) as f:
            start = json.load(f)
        status = {}
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE) as f:
                status = json.load(f)
        finished = datetime.now(timezone.utc)
        started_iso = start.get("started", finished.isoformat())
        try:
            started_dt = datetime.fromisoformat(started_iso)
            duration = round((finished - started_dt).total_seconds() / 60, 1)
        except Exception:
            duration = None
        record = {
            "type": start.get("type", "full"),
            "started": started_iso,
            "finished": finished.isoformat(),
            "duration_minutes": duration,
            "total_found": status.get("total_found", 0),
            "total_new": status.get("total_new", 0),
            "status": "stopped",
            "triggered_by": start.get("triggered_by", "manual"),
        }
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        history.insert(0, record)
        with open(HISTORY_FILE, "w") as f:
            json.dump(history[:100], f, indent=2)
        os.remove(START_FILE)
    except Exception:
        pass


def _kill_search_processes():
    """Kill any Python search subprocess — by handle if available, else by scanning processes."""
    global _search_proc
    # Kill via handle if we have one
    if _search_proc is not None and _search_proc.poll() is None:
        _search_proc.terminate()
        try:
            _search_proc.wait(timeout=8)
        except Exception:
            _search_proc.kill()
    _search_proc = None
    # Also scan for orphaned search processes (e.g. after dashboard restart)
    # Match both "run_directories.py" and bare "run_directories" (covers import-style launches)
    search_scripts = {
        "searcher.py", "run_weekly.py", "run_facebook.py", "run_partial.py", "run_directories.py",
        "run_recheck.py",
        "searcher", "run_weekly", "run_facebook", "run_partial", "run_directories",
        "run_recheck",
    }
    our_pid = str(os.getpid())
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-WmiObject Win32_Process | Where-Object { $_.Name -like 'python*' } "
             "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10)
        if result.stdout.strip():
            import json as _json
            procs = _json.loads(result.stdout)
            if isinstance(procs, dict):
                procs = [procs]
            for proc in procs:
                pid = str(proc.get("ProcessId", ""))
                cmd = proc.get("CommandLine") or ""
                if pid == our_pid:
                    continue
                if any(s in cmd for s in search_scripts):
                    subprocess.run(["powershell", "-Command", f"Stop-Process -Id {pid} -Force"],
                                   capture_output=True, timeout=5)
    except Exception:
        pass


@app.route("/stop-search", methods=["POST"])
def stop_search():
    _write_stopped_history()
    _kill_search_processes()
    # Clean up all flags and status so the UI resets immediately
    for path in [RUNNING_FLAG, PAUSE_FLAG, PID_FILE, STATUS_FILE, START_FILE]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return redirect(url_for("index", message="Search stopped.", type="success"))


@app.route("/find-facebook", methods=["POST"])
def find_facebook_for_company():
    """Look up a Facebook page URL, scrape its profile data, and save all fb_* fields."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import find_facebook_url, scrape_facebook_page, write_audit
        print(f"[Facebook] Searching for Facebook page: {company_name}")
        fb_url = find_facebook_url(company_name)
        if fb_url:
            print(f"[Facebook] Found URL: {fb_url}")
            fb_data = scrape_facebook_page(fb_url, company_name=company_name)
            if fb_data.get("followers"): print(f"[Facebook] Followers: {fb_data['followers']}")
            if fb_data.get("phone"):     print(f"[Facebook] Phone: {fb_data['phone']}")
            if fb_data.get("email"):     print(f"[Facebook] Email: {fb_data['email']}")
            if fb_data.get("category"):  print(f"[Facebook] Category: {fb_data['category']}")
            patch = {
                "facebook_url": fb_url,
                "fb_followers": fb_data.get("followers"),
                "fb_phone":     fb_data.get("phone"),
                "fb_email":     fb_data.get("email"),
                "fb_address":   fb_data.get("address"),
                "fb_description": fb_data.get("description"),
                "fb_category":  fb_data.get("category"),
                "fb_rating":    fb_data.get("rating"),
            }
            patch = {k: v for k, v in patch.items() if v is not None}
            patch["facebook_url"] = fb_url  # always save URL even if no extra data
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                           headers=headers, json=patch)
            write_audit("updated", company_id, company_name,
                        changes=f"Facebook recheck: url={fb_url} followers={fb_data.get('followers')}",
                        triggered_by="manual (dashboard)")
            return jsonify({"found": True, "url": fb_url, **fb_data})
        print(f"[Facebook] No Facebook page found")
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-companies-office", methods=["POST"])
def recheck_companies_office_for_company():
    """Re-run Companies Office lookup for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import check_companies_office, write_audit
        print(f"[Companies Office] Searching for: {company_name}")
        result = check_companies_office(company_name)
        if result.get("name"):
            display = result.get("registered_name") or result.get("name")
            trading = result.get("trading_name")
            print(f"[Companies Office] Found: {display}" + (f" t/a {trading}" if trading else "") +
                  f" | Status: {result.get('status')} | NZBN: {result.get('nzbn')}")
            if result.get("directors"):
                print(f"[Companies Office] Directors: {', '.join(result['directors'])}")
            if result.get("address"):
                print(f"[Companies Office] Address: {result['address']}")
            if result.get("website"):
                print(f"[Companies Office] Website: {result['website']}")
        else:
            print(f"[Companies Office] Not found on Companies Register")
        patch = {
            "companies_office_name":    result.get("registered_name") or result.get("name"),
            "companies_office_address": result.get("address"),
            "companies_office_number":  result.get("company_number"),
            "nzbn":                     result.get("nzbn"),
            "co_status":                result.get("status"),
            "co_incorporated":          result.get("incorporated"),
            "co_website":               result.get("website"),
        }
        directors = result.get("directors") or []
        if directors:
            patch["director_name"] = ", ".join(directors)
        patch = {k: v for k, v in patch.items() if v is not None}
        if patch:
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                           headers=headers, json=patch)
        write_audit("updated", company_id, company_name,
                    changes=f"CO recheck: name={result.get('name')} status={result.get('status')} nzbn={result.get('nzbn')}",
                    triggered_by="manual (dashboard)")
        return jsonify({
            "found": bool(result.get("name")),
            "co_name": result.get("name"),
            "co_status": result.get("status"),
            "nzbn": result.get("nzbn"),
            "co_incorporated": result.get("incorporated"),
            "director_name": ", ".join(result.get("directors") or []) or None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-google-profile", methods=["POST"])
def recheck_google_profile_for_company():
    """Re-run Google Business Profile lookup for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    company_region = request.json.get("region", "") or ""
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import get_google_business_profile, write_audit
        print(f"[Google Profile] Searching for: {company_name}" + (f" ({company_region})" if company_region else ""))
        result = get_google_business_profile(company_name, company_region)
        patch = {
            "google_rating":  result.get("rating"),
            "google_reviews": result.get("reviews"),
            "google_phone":   result.get("phone"),
            "google_address": result.get("address"),
            "google_email":   result.get("email"),
        }
        patch = {k: v for k, v in patch.items() if v is not None}
        if patch:
            print(f"[Google Profile] Found: rating={result.get('rating')} reviews={result.get('reviews')}")
            if result.get("phone"):  print(f"[Google Profile] Phone: {result['phone']}")
            if result.get("address"): print(f"[Google Profile] Address: {result['address']}")
            if result.get("email"):  print(f"[Google Profile] Email: {result['email']}")
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                           headers=headers, json=patch)
        else:
            print(f"[Google Profile] No Business Profile found")
        write_audit("updated", company_id, company_name,
                    changes=f"Google profile recheck: rating={result.get('rating')} reviews={result.get('reviews')} email={result.get('email')}",
                    triggered_by="manual (dashboard)")
        return jsonify({
            "found": bool(result.get("rating") or result.get("phone") or result.get("address")),
            "google_rating": result.get("rating"),
            "google_reviews": result.get("reviews"),
            "google_phone": result.get("phone"),
            "google_address": result.get("address"),
            "google_email": result.get("email"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/full-recheck", methods=["POST"])
def full_recheck_for_company():
    """Re-run all checks (CO, Facebook, Google, PSPLA, NZSA) for a single company and save everything."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    website_url = request.json.get("website", "")
    company_region = request.json.get("region", "") or ""
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import (
            check_companies_office, check_pspla, check_pspla_individual,
            check_nzsa, find_facebook_url, scrape_facebook_page,
            find_linkedin_url, scrape_linkedin_page,
            get_google_business_profile, write_audit,
            scrape_website, gather_service_text, detect_services,
        )
        summary = {}
        patch = {}
        headers = {
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        }

        # 1. Companies Office
        co_result = check_companies_office(company_name)
        if co_result.get("name"):
            patch.update({
                "companies_office_name":    co_result.get("name"),
                "companies_office_address": co_result.get("address"),
                "companies_office_number":  co_result.get("company_number"),
                "nzbn":                     co_result.get("nzbn"),
                "co_status":                co_result.get("status"),
                "co_incorporated":          co_result.get("incorporated"),
            })
            if co_result.get("directors"):
                patch["director_name"] = ", ".join(co_result["directors"])
            summary["co"] = co_result.get("name")

        # 2. Facebook — find URL if missing, then scrape profile
        row_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=facebook_url,linkedin_url,nzsa_member_name,nzsa_grade",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10
        )
        row = row_resp.json()[0] if row_resp.ok and row_resp.json() else {}
        fb_url = row.get("facebook_url") or find_facebook_url(company_name)
        if fb_url:
            patch["facebook_url"] = fb_url
            fb_data = scrape_facebook_page(fb_url, company_name=company_name)
            for field in ("followers", "phone", "email", "address", "description", "category", "rating"):
                if fb_data.get(field):
                    patch[f"fb_{field}"] = fb_data[field]
            summary["fb"] = fb_url

        # 2b. LinkedIn — find URL if missing, scrape followers/description
        li_url = row.get("linkedin_url") or find_linkedin_url(company_name)
        if li_url:
            patch["linkedin_url"] = li_url
            li_data = scrape_linkedin_page(li_url, company_name=company_name)
            for field in ("followers", "description", "industry", "location", "website", "size"):
                if li_data.get(field):
                    patch[f"linkedin_{field}"] = li_data[field]
            summary["li_followers"] = li_data.get("followers")

        # 3. Google Business Profile
        gp = get_google_business_profile(company_name, company_region)
        for field in ("rating", "reviews", "phone", "address", "email"):
            if gp.get(field):
                patch[f"google_{field}"] = gp[field]
        if gp.get("rating"):
            summary["google_rating"] = gp["rating"]

        # 4. PSPLA — build extra_context from everything gathered so far
        extra_context = {
            "facebook_snippet": fb_data.get("description", "") if fb_url else "",
            "linkedin_url": row.get("linkedin_url") or "",
            "nzsa_data": {"member_name": row.get("nzsa_member_name"), "grade": row.get("nzsa_grade")}
                         if row.get("nzsa_member_name") else None,
        }
        directors = [d.strip() for d in (patch.get("director_name") or "").split(",") if d.strip()]
        pspla_result = check_pspla(company_name, website_region=company_region,
                                   co_result=co_result, directors=directors,
                                   extra_context=extra_context)
        if not pspla_result.get("licensed") and co_result.get("name") and co_result["name"] != company_name:
            co_try = check_pspla(co_result["name"], website_region=company_region,
                                 co_result=co_result, directors=directors,
                                 extra_context=extra_context)
            if co_try.get("matched_name"):
                pspla_result = co_try
        licensed = pspla_result.get("licensed")
        individual_license = None
        if not licensed:
            for d in directors[:3]:
                ind = check_pspla_individual(d)
                if ind.get("found"):
                    individual_license = ind["name"]
                    licensed = True
                    break
        patch.update({
            "pspla_licensed":        licensed,
            "pspla_name":            pspla_result.get("matched_name"),
            "pspla_license_number":  pspla_result.get("pspla_license_number"),
            "pspla_license_status":  pspla_result.get("pspla_license_status"),
            "pspla_license_expiry":  pspla_result.get("pspla_license_expiry"),
            "pspla_license_classes": pspla_result.get("pspla_license_classes"),
            "pspla_license_start":   pspla_result.get("pspla_license_start"),
            "pspla_permit_type":     pspla_result.get("pspla_permit_type"),
            "license_type":          pspla_result.get("license_type"),
            "match_method":          pspla_result.get("match_method"),
            "individual_license":    individual_license,
        })
        summary["pspla_licensed"] = licensed
        summary["pspla_name"] = pspla_result.get("matched_name")

        # 5. NZSA
        nzsa_result = check_nzsa(company_name, website=website_url)
        patch.update({
            "nzsa_member":       "true" if nzsa_result["member"] else "false",
            "nzsa_member_name":  nzsa_result["member_name"],
            "nzsa_accredited":   "true" if nzsa_result["accredited"] else "false",
            "nzsa_grade":        nzsa_result["grade"],
            "nzsa_contact_name": nzsa_result.get("contact_name") or None,
            "nzsa_phone":        nzsa_result.get("phone") or None,
            "nzsa_email":        nzsa_result.get("email") or None,
            "nzsa_overview":     nzsa_result.get("overview") or None,
        })
        summary["nzsa_member"] = nzsa_result["member"]

        # 6. Service detection — scrape website and detect alarm/CCTV/monitoring mentions
        if website_url:
            page_text, _, _, _ = scrape_website(website_url)
            service_text = gather_service_text(website_url, page_text)
            services = detect_services(service_text)
            patch.update({
                "has_alarm_systems":    services.get("has_alarm_systems"),
                "has_cctv_cameras":     services.get("has_cctv_cameras"),
                "has_alarm_monitoring": services.get("has_alarm_monitoring"),
            })
            summary["services"] = [k for k, v in services.items() if v]

        # Save everything in one patch
        from datetime import datetime, timezone
        patch["last_checked"] = datetime.now(timezone.utc).isoformat()
        clean_patch = {k: v for k, v in patch.items() if v is not None}
        clean_patch["pspla_licensed"] = licensed  # always save even if False
        clean_patch["nzsa_member"] = "true" if nzsa_result["member"] else "false"
        requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                       headers=headers, json=clean_patch)

        write_audit("updated", company_id, company_name,
                    changes=f"Full recheck: pspla={licensed} co={co_result.get('name')} nzsa={nzsa_result['member']} google_rating={gp.get('rating')}",
                    triggered_by="manual (dashboard)")
        return jsonify({"ok": True, "summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-llm-sense", methods=["POST"])
def recheck_llm_sense():
    """Use Claude Sonnet to sense-check all associations on a record and clear any that
    are clearly wrong (NZSA, PSPLA, Facebook, LinkedIn, Companies Office)."""
    from flask import jsonify
    import anthropic as _anthropic
    import json as _json

    company_id = request.json.get("id")
    if not company_id:
        return jsonify({"error": "No id provided"}), 400

    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        rows = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=*",
            headers=headers, timeout=10
        ).json()
        if not rows:
            return jsonify({"error": "Company not found"}), 404
        c = rows[0]
        company_name = c.get("company_name") or ""
        website      = c.get("website") or ""

        print(f"[LLM Sense] Checking: {company_name}")
        print(f"[LLM Sense] Website:  {website or '(none)'}")

        # Build a readable summary of all associations
        lines = []

        if c.get("nzsa_member_name"):
            lines.append(f"\nNZSA MEMBERSHIP:")
            lines.append(f"  Member name: {c['nzsa_member_name']}")
            if c.get("nzsa_accredited"): lines.append(f"  Accredited: {c['nzsa_accredited']}")
            if c.get("nzsa_grade"):      lines.append(f"  Grade: {c['nzsa_grade']}")
            if c.get("nzsa_contact_name"): lines.append(f"  Contact: {c['nzsa_contact_name']}")
            if c.get("nzsa_email"):      lines.append(f"  Email: {c['nzsa_email']}")
            if c.get("nzsa_overview"):   lines.append(f"  Overview: {c['nzsa_overview'][:300]}")

        if c.get("pspla_name"):
            lines.append(f"\nPSPLA LICENCE:")
            lines.append(f"  Matched name: {c['pspla_name']}")
            if c.get("pspla_license_status"): lines.append(f"  Status: {c['pspla_license_status']}")
            if c.get("pspla_address"):   lines.append(f"  Address: {c['pspla_address']}")
            if c.get("pspla_license_classes"): lines.append(f"  Classes: {c['pspla_license_classes']}")
            if c.get("match_reason"):    lines.append(f"  Match reason: {c['match_reason']}")

        if c.get("facebook_url"):
            lines.append(f"\nFACEBOOK:")
            lines.append(f"  URL: {c['facebook_url']}")
            if c.get("fb_description"): lines.append(f"  Description: {c['fb_description'][:300]}")
            if c.get("fb_category"):    lines.append(f"  Category: {c['fb_category']}")
            if c.get("fb_address"):     lines.append(f"  Address: {c['fb_address']}")

        if c.get("linkedin_url"):
            lines.append(f"\nLINKEDIN:")
            lines.append(f"  URL: {c['linkedin_url']}")
            if c.get("linkedin_description"): lines.append(f"  Description: {c['linkedin_description'][:200]}")
            if c.get("linkedin_location"):    lines.append(f"  Location: {c['linkedin_location']}")

        if c.get("companies_office_name"):
            lines.append(f"\nCOMPANIES OFFICE:")
            lines.append(f"  Registered name: {c['companies_office_name']}")
            if c.get("companies_office_address"): lines.append(f"  Address: {c['companies_office_address']}")
            if c.get("co_status"): lines.append(f"  Status: {c['co_status']}")

        if c.get("google_address"):
            lines.append(f"\nGOOGLE BUSINESS:")
            lines.append(f"  Address: {c['google_address']}")
            if c.get("google_phone"): lines.append(f"  Phone: {c['google_phone']}")

        # Email — include domain so Claude can flag obvious mismatches
        _free_domains = {"gmail.com","hotmail.com","yahoo.com","outlook.com",
                         "xtra.co.nz","yahoo.co.nz","icloud.com","live.com","me.com","msn.com"}
        _dir_domains  = {"facebook.com","linkedin.com","google.com","moneyhub.co.nz",
                         "yellowpages.co.nz","trademe.co.nz"}
        email_val = c.get("email") or ""
        if email_val:
            email_dom = email_val.split("@")[-1].lower() if "@" in email_val else ""
            website_dom = (website.lower().replace("https://","").replace("http://","")
                           .replace("www.","").split("/")[0]) if website else ""
            lines.append(f"\nEMAIL ON RECORD: {email_val}")
            if email_dom and email_dom not in _free_domains and email_dom not in _dir_domains:
                if website_dom and website_dom not in _dir_domains and email_dom != website_dom:
                    lines.append(f"  Note: email domain ({email_dom}) differs from stored website domain ({website_dom})")
                else:
                    lines.append(f"  Email domain: {email_dom}")

        if not lines:
            print("[LLM Sense] No associations to check.")
            return jsonify({"ok": True, "cleared": [], "message": "No associations to check."})

        context = "\n".join(lines)

        prompt = f"""You are auditing a New Zealand security company database record.

COMPANY: {company_name}
PRIMARY WEBSITE: {website or "(none recorded)"}
REGION: {c.get("region") or "unknown"}

The following associations were found automatically by a web search tool. Your job is to
identify any that are CLEARLY wrong — i.e. they obviously belong to a different company
and not to "{company_name}".
{context}

RULES:
- Be CONSERVATIVE. Only flag something if you are SURE it is wrong. If you are uncertain, leave it.
- Minor name variations are fine (Ltd/Limited, punctuation, word order, trading names).
- A PSPLA or NZSA name that is a known trading name or abbreviation of the company is fine.
- A Facebook/LinkedIn URL whose slug or description clearly matches the company name is fine.
- Only flag if the association is OBVIOUSLY a completely different organisation.
- NEVER clear something just because the name is slightly different — clear only if it is a different company entirely.
- For NZSA: if the NZSA member name shares no meaningful words with "{company_name}" and the overview/contact details don't fit, it may be wrong.
- For PSPLA: if the matched licence name is for a completely different business, flag it.
- For Facebook: if the URL slug or description clearly refers to a different business or overseas entity, flag it.
- For LinkedIn: same as Facebook.
- For Companies Office: if the registered name is for a completely different business, flag it.
- For Google Business: if the address or phone clearly belongs to a different business at a different location, flag it.
- For Email: if the email domain clearly belongs to a completely different unrelated company, flag it.
  IMPORTANT email rules: NEVER flag gmail/hotmail/xtra/yahoo/outlook/icloud — these are personal email providers.
  NEVER flag if the stored website is a Facebook or directory URL (email domain can't be compared meaningfully to a social media URL).
  ONLY flag if the email domain refers to a clearly different business — e.g. email is for a plumbing company when this is a security company.
  If the email domain contains words that relate to the company name (even partially), it is fine.

Respond with ONLY valid JSON — no markdown, no explanation outside the JSON.
Use this exact structure:
{{
  "clear_nzsa":            false,
  "clear_nzsa_reason":     "",
  "clear_pspla":           false,
  "clear_pspla_reason":    "",
  "clear_facebook":        false,
  "clear_facebook_reason": "",
  "clear_linkedin":        false,
  "clear_linkedin_reason": "",
  "clear_companies_office": false,
  "clear_companies_office_reason": "",
  "clear_google":          false,
  "clear_google_reason":   "",
  "clear_email":           false,
  "clear_email_reason":    ""
}}

Set a value to true ONLY if you are confident the association is for a different company."""

        ai_client = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        result = _json.loads(raw.strip())

        print(f"[LLM Sense] AI response received.")

        # Apply clearances
        patch = {}
        cleared = []

        if result.get("clear_nzsa"):
            reason = result.get("clear_nzsa_reason", "")
            print(f"[LLM Sense] Clearing NZSA: {reason}")
            patch.update({"nzsa_member": "false", "nzsa_member_name": None,
                          "nzsa_accredited": "false", "nzsa_grade": None,
                          "nzsa_contact_name": None, "nzsa_phone": None,
                          "nzsa_email": None, "nzsa_overview": None})
            cleared.append(f"NZSA: {reason}")

        if result.get("clear_pspla"):
            reason = result.get("clear_pspla_reason", "")
            print(f"[LLM Sense] Clearing PSPLA: {reason}")
            patch.update({"pspla_licensed": None, "pspla_name": None,
                          "pspla_license_number": None, "pspla_license_status": None,
                          "pspla_license_expiry": None, "pspla_license_classes": None,
                          "pspla_license_start": None, "pspla_permit_type": None,
                          "match_method": None, "match_reason": None})
            cleared.append(f"PSPLA: {reason}")

        if result.get("clear_facebook"):
            reason = result.get("clear_facebook_reason", "")
            print(f"[LLM Sense] Clearing Facebook: {reason}")
            patch.update({"facebook_url": None, "fb_followers": None, "fb_phone": None,
                          "fb_email": None, "fb_address": None, "fb_description": None,
                          "fb_category": None, "fb_rating": None,
                          "fb_alarm_systems": None, "fb_cctv_cameras": None, "fb_alarm_monitoring": None})
            cleared.append(f"Facebook: {reason}")

        if result.get("clear_linkedin"):
            reason = result.get("clear_linkedin_reason", "")
            print(f"[LLM Sense] Clearing LinkedIn: {reason}")
            patch.update({"linkedin_url": None, "linkedin_followers": None,
                          "linkedin_description": None, "linkedin_industry": None,
                          "linkedin_location": None, "linkedin_website": None, "linkedin_size": None})
            cleared.append(f"LinkedIn: {reason}")

        if result.get("clear_companies_office"):
            reason = result.get("clear_companies_office_reason", "")
            print(f"[LLM Sense] Clearing Companies Office: {reason}")
            patch.update({"companies_office_name": None, "companies_office_address": None,
                          "companies_office_number": None, "nzbn": None,
                          "co_status": None, "co_incorporated": None, "co_website": None,
                          "director_name": None, "individual_license": None})
            cleared.append(f"Companies Office: {reason}")

        if result.get("clear_google"):
            reason = result.get("clear_google_reason", "")
            print(f"[LLM Sense] Clearing Google Business: {reason}")
            patch.update({"google_rating": None, "google_reviews": None,
                          "google_phone": None, "google_address": None,
                          "google_email": None})
            cleared.append(f"Google: {reason}")

        if result.get("clear_email"):
            reason = result.get("clear_email_reason", "")
            print(f"[LLM Sense] Clearing email: {reason}")
            patch["email"] = None
            cleared.append(f"Email: {reason}")

        if patch:
            ph = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
            requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                           headers=ph, json=patch, timeout=10)
            from searcher import write_audit
            write_audit("updated", str(company_id), company_name,
                        changes="LLM sense-check cleared: " + "; ".join(cleared),
                        triggered_by="manual (dashboard)")
            print(f"[LLM Sense] Cleared {len(cleared)} association(s).")
        else:
            print("[LLM Sense] All associations look correct — nothing cleared.")

        return jsonify({"ok": True, "cleared": cleared,
                        "message": f"Cleared {len(cleared)} association(s)." if cleared else "All associations look correct."})

    except Exception as e:
        print(f"[LLM Sense] Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-nzsa", methods=["POST"])
def recheck_nzsa_for_company():
    """Re-check NZSA membership for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import check_nzsa
        # Fetch website for domain matching
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        row = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=website",
            headers=headers, timeout=10
        ).json()
        website = row[0].get("website") if row else None

        print(f"[NZSA] Checking membership for: {company_name}")
        if website:
            print(f"[NZSA] Using website for domain match: {website}")
        result = check_nzsa(company_name, website=website)
        if result["member"]:
            print(f"[NZSA] Member found: {result['member_name']}" + (" (Accredited)" if result.get("accredited") else ""))
            if result.get("grade"):    print(f"[NZSA] Grade: {result['grade']}")
            if result.get("contact_name"): print(f"[NZSA] Contact: {result['contact_name']}")
            if result.get("email"):    print(f"[NZSA] Email: {result['email']}")
        else:
            print(f"[NZSA] Not a member")
        patch_headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=patch_headers,
            json={
                "nzsa_member": "true" if result["member"] else "false",
                "nzsa_member_name": result["member_name"],
                "nzsa_accredited": "true" if result["accredited"] else "false",
                "nzsa_grade": result["grade"],
                "nzsa_contact_name": result.get("contact_name") or None,
                "nzsa_phone": result.get("phone") or None,
                "nzsa_email": result.get("email") or None,
                "nzsa_overview": result.get("overview") or None,
            },
        )
        from searcher import write_audit
        write_audit("updated", company_id, company_name,
                    changes=f"NZSA recheck: member={result['member']}, name={result['member_name']}",
                    triggered_by="manual (dashboard)")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-services", methods=["POST"])
def recheck_services_for_company():
    """Re-scrape the company website and detect alarm/CCTV/monitoring services."""
    from flask import jsonify
    company_id = request.json.get("id")
    website_url = request.json.get("website", "")
    if not company_id or not website_url:
        return jsonify({"error": "Missing id or website"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import scrape_website, gather_service_text, detect_services, write_audit
        print(f"[Services] Scraping: {website_url}")
        page_text, _, _, _ = scrape_website(website_url)
        print(f"[Services] Page scraped ({len(page_text or '')} chars) — gathering service text")
        service_text = gather_service_text(website_url, page_text)
        print(f"[Services] Running service detection ({len(service_text or '')} chars of text)")
        services = detect_services(service_text)
        detected = [k for k, v in services.items() if v]
        print(f"[Services] Detected: {', '.join(detected) if detected else 'none'}")
        headers = {
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal",
        }
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=headers,
            json={
                "has_alarm_systems":    services.get("has_alarm_systems"),
                "has_cctv_cameras":     services.get("has_cctv_cameras"),
                "has_alarm_monitoring": services.get("has_alarm_monitoring"),
            },
        )
        return jsonify({"ok": True, **services})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/find-linkedin", methods=["POST"])
def find_linkedin_for_company():
    """Look up a LinkedIn company page for a single company by ID and save it."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import find_linkedin_url, scrape_linkedin_page, write_audit
        print(f"[LinkedIn] Searching for LinkedIn page: {company_name}")
        li_url = find_linkedin_url(company_name)
        if li_url:
            print(f"[LinkedIn] Found URL: {li_url}")
            li_data = scrape_linkedin_page(li_url, company_name=company_name)
            if li_data.get("followers"): print(f"[LinkedIn] Followers: {li_data['followers']}")
            if li_data.get("industry"):  print(f"[LinkedIn] Industry: {li_data['industry']}")
            if li_data.get("location"):  print(f"[LinkedIn] Location: {li_data['location']}")
            patch = {"linkedin_url": li_url}
            for field in ("followers", "description", "industry", "location", "website", "size"):
                if li_data.get(field):
                    patch[f"linkedin_{field}"] = li_data[field]
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                headers=headers,
                json=patch,
            )
            write_audit("updated", company_id, company_name,
                        changes=f"LinkedIn found: {li_url} followers={li_data.get('followers')} industry={li_data.get('industry')}",
                        triggered_by="manual (dashboard)")
            return jsonify({"found": True, "url": li_url,
                            "followers": li_data.get("followers"),
                            "description": li_data.get("description"),
                            "industry": li_data.get("industry"),
                            "location": li_data.get("location"),
                            "website": li_data.get("website"),
                            "size": li_data.get("size")})
        print(f"[LinkedIn] No LinkedIn page found")
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/recheck-pspla", methods=["POST"])
def recheck_pspla_for_company():
    """Re-run PSPLA check for a single company by ID and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    company_region = request.json.get("region", "") or None
    co_name = request.json.get("co_name", "") or None
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    _rl = _recheck_log_capture(); _rl.__enter__()
    try:
        from searcher import check_pspla, check_pspla_individual
        print(f"[PSPLA] Starting recheck for: {company_name}" + (f" (region: {company_region})" if company_region else ""))
        if co_name and co_name != company_name:
            print(f"[PSPLA] Also checking Companies Office name: {co_name}")
        # Fetch stored context to improve LLM-assisted matching
        row_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=director_name,companies_office_name,companies_office_address,facebook_url,linkedin_url,nzsa_member_name,nzsa_grade",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10
        )
        row = row_resp.json()[0] if row_resp.ok and row_resp.json() else {}
        stored_directors = [d.strip() for d in (row.get("director_name") or "").split(",") if d.strip()]
        stored_co = {"name": row.get("companies_office_name"), "address": row.get("companies_office_address")} if row.get("companies_office_name") else None
        extra_context = {
            "facebook_snippet": "",
            "linkedin_url": row.get("linkedin_url") or "",
            "nzsa_data": {"member_name": row.get("nzsa_member_name"), "grade": row.get("nzsa_grade")} if row.get("nzsa_member_name") else None,
        }
        result = check_pspla(company_name, website_region=company_region,
                             co_result=stored_co, directors=stored_directors,
                             extra_context=extra_context)
        # If no active licensed match found and we have a Companies Office name, try that too.
        # Only replace result if CO search is licensed (active), or if the original found no
        # matched_name at all — don't replace a known-expired result or we skip individual check.
        if not result.get("licensed") and co_name and co_name != company_name:
            co_result = check_pspla(co_name, website_region=company_region,
                                    co_result=stored_co, directors=stored_directors,
                                    extra_context=extra_context)
            if co_result.get("matched_name") and (co_result.get("licensed") or not result.get("matched_name")):
                result = co_result
        licensed = result.get("licensed")
        pspla_name = result.get("matched_name")
        if pspla_name:
            print(f"[PSPLA] Match: {pspla_name} | Licensed: {licensed} | Method: {result.get('match_method')}")
        else:
            print(f"[PSPLA] No company licence match found")

        # If no active company license, check individual license using the stored director names
        individual_license = None
        if not licensed:
            director_str = request.json.get("directors", "")
            directors = [d.strip() for d in director_str.split(",") if d.strip()] if director_str else []
            for director in directors:
                print(f"[PSPLA] Checking individual licence for director: {director}")
                ind = check_pspla_individual(director)
                if ind.get("found"):
                    individual_license = ind["name"]
                    licensed = True
                    print(f"[PSPLA] Individual licence found: {individual_license}")
                    break

        _pspla_fields = ["pspla_name", "pspla_address", "pspla_license_number",
                         "pspla_license_status", "pspla_license_expiry",
                         "license_type", "match_method"]
        if licensed or pspla_name:
            # Match found — save what we have, skip None values so we don't wipe good data
            update = {
                "pspla_licensed": licensed,
                "pspla_name": pspla_name,
                "pspla_address": result.get("pspla_address"),
                "pspla_license_number": result.get("pspla_license_number"),
                "pspla_license_status": result.get("pspla_license_status"),
                "pspla_license_expiry": result.get("pspla_license_expiry"),
                "license_type": result.get("license_type"),
                "match_method": result.get("match_method"),
                "individual_license": individual_license,
            }
            update = {k: v for k, v in update.items() if v is not None}
            update["pspla_licensed"] = licensed
        else:
            # No match found — explicitly null out all PSPLA fields so stale data is cleared.
            # Without this, old wrong data (e.g. "Addz Livewire Electrical") stays in DB
            # because None values were previously stripped before patching.
            update = {f: None for f in _pspla_fields}
            update["pspla_licensed"] = False
            update["individual_license"] = individual_license
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=headers,
            json=update,
        )
        from searcher import write_audit
        write_audit("updated", company_id, company_name,
                    changes=f"PSPLA recheck: licensed={licensed}, name={pspla_name}",
                    triggered_by="manual (dashboard)")
        return jsonify({
            "licensed": licensed,
            "pspla_name": pspla_name,
            "individual_license": individual_license,
            "pspla_license_status": result.get("pspla_license_status"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _rl.__exit__(None, None, None)


@app.route("/duplicates")
def duplicates_page():
    """Show all records that share the same root_domain (potential duplicates)."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=root_domain.asc,id.asc&limit=2000",
        headers=headers,
    )
    all_companies = resp.json() if resp.ok else []

    # Group by root_domain
    from collections import defaultdict
    groups = defaultdict(list)
    for c in all_companies:
        domain = c.get("root_domain") or ""
        groups[domain].append(c)

    # Only groups with 2+ records and a non-empty domain
    dup_groups = {d: recs for d, recs in groups.items() if d and len(recs) >= 2}

    return render_template_string(DUPLICATES_TEMPLATE, dup_groups=dup_groups)


DUPLICATES_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Duplicate Companies</title>
<style>
body { font-family: Arial, sans-serif; font-size: 13px; padding: 20px; background: #f5f5f5; }
h1 { color: #2c3e50; }
.back { margin-bottom: 16px; display: inline-block; color: #2980b9; text-decoration: none; }
.group { background: white; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 18px; padding: 14px; }
.group h3 { margin: 0 0 10px; font-size: 14px; color: #555; }
table { width: 100%; border-collapse: collapse; }
th { background: #ecf0f1; text-align: left; padding: 6px 8px; font-size: 12px; }
td { padding: 6px 8px; border-top: 1px solid #eee; vertical-align: top; }
.del-btn { padding: 2px 8px; background: #c0392b; color: white; border: none; border-radius: 3px; cursor: pointer; font-size: 11px; }
.keep { background: #eafaf1; }
</style>
</head>
<body>
<a class="back" href="/">← Back to dashboard</a>
<h1>Duplicate Records (same website domain)</h1>
<p>{{ dup_groups|length }} domain(s) with multiple entries. Delete the unwanted record using the ✕ button.</p>
{% for domain, recs in dup_groups.items() %}
<div class="group">
    <h3>{{ domain }} — {{ recs|length }} records</h3>
    <table>
        <tr><th>ID</th><th>Company Name</th><th>Website</th><th>Region</th><th>PSPLA</th><th>Found Via</th><th>Delete</th></tr>
        {% for c in recs %}
        <tr id="dup-row-{{ c.id }}">
            <td>{{ c.id }}</td>
            <td>{{ c.company_name or '-' }}</td>
            <td style="font-size:11px">{{ c.website or '-' }}</td>
            <td>{{ c.region or '-' }}</td>
            <td>{{ 'Licensed' if c.pspla_licensed == true else ('Not licensed' if c.pspla_licensed == false else '?') }}</td>
            <td style="font-size:11px">{{ c.notes or '-' }}</td>
            <td><button class="del-btn" onclick="deleteDup({{ c.id }}, '{{ (c.company_name or '') | replace("'", "\\\\'") }}')">✕</button></td>
        </tr>
        {% endfor %}
    </table>
</div>
{% else %}
<p style="color: #27ae60; font-weight: bold;">No duplicates found.</p>
{% endfor %}
<script>
function deleteDup(id, name) {
    if (!confirm('Delete "' + name + '"?')) return;
    fetch('/delete-company', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: id})
    })
    .then(r => r.json())
    .then(d => {
        if (d.ok) {
            var row = document.getElementById('dup-row-' + id);
            if (row) row.remove();
        } else {
            alert('Delete failed: ' + (d.error || 'unknown'));
        }
    });
}
</script>
</body>
</html>
"""


@app.route("/suspect-records")
def suspect_records():
    """Show records that may be wrong, low-quality, or overseas — for manual review."""
    import re as _re

    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=id.asc&limit=3000",
        headers=headers,
    )
    all_companies = resp.json() if resp.ok else []

    _OVERSEAS = ["united states", "united states of america", " usa ", "u.s.a.",
                 "us-based", "u.s. based", " u.s. ", "(usa)", "(u.s.)",
                 "co.uk", "united kingdom", ".com.au", "australia"]

    suspects = []
    for c in all_companies:
        reasons = []
        name      = c.get("company_name") or ""
        website   = c.get("website") or ""
        fb_url    = c.get("facebook_url") or ""
        src_url   = c.get("source_url") or ""
        fb_desc   = c.get("fb_description") or ""
        notes     = c.get("notes") or ""
        pspla     = c.get("pspla_licensed")
        co_name   = c.get("companies_office_name") or ""
        nzsa      = c.get("nzsa_member")
        email     = c.get("email") or ""
        phone     = c.get("phone") or ""

        # 1. No real website — using FB page as website
        if fb_url and website and website.rstrip("/") == fb_url.rstrip("/"):
            reasons.append("No real website — only a Facebook page URL")

        # 2. Overseas signals in FB description
        overseas_hits = [s for s in _OVERSEAS if s in fb_desc.lower()]
        if overseas_hits:
            reasons.append(f"Overseas signal in FB description: {overseas_hits[0]!r}")

        # 3. Non-ASCII / non-English company name
        if name and not all(ord(ch) < 128 for ch in name):
            reasons.append("Company name contains non-ASCII characters")

        # 4. Thin record — found via Facebook, no PSPLA, no CO, no NZSA, no email, no phone
        from_fb = "facebook" in notes.lower() or "facebook.com" in src_url.lower()
        if from_fb and pspla is None and not co_name and nzsa not in ("true", True) and not email and not phone:
            reasons.append("Found via Facebook with no corroborating data (no CO/NZSA/email/phone)")

        # 5. Source URL is a Facebook group post (should have been filtered)
        if "facebook.com/groups/" in src_url.lower():
            reasons.append("Source URL is a Facebook group post (not a business page)")

        # 6. FB URL slug looks non-NZ: ends in 'us', 'uk', 'au', 'ca' as a word boundary
        if fb_url:
            slug_m = _re.search(r"facebook\.com/([^/?#]+)", fb_url)
            if slug_m:
                slug = slug_m.group(1).lower()
                for suffix in ("us", "uk", "au", "ca", "usa"):
                    if slug.endswith(suffix) and len(slug) > len(suffix) + 2:
                        reasons.append(f"Facebook slug ends with country suffix: '…{suffix}'")
                        break

        if reasons:
            suspects.append({"record": c, "reasons": reasons})

    # Sort: most reasons first
    suspects.sort(key=lambda x: -len(x["reasons"]))

    return render_template_string(SUSPECTS_TEMPLATE, suspects=suspects)


SUSPECTS_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Suspect Records</title>
<style>
body { font-family: Arial, sans-serif; font-size: 13px; padding: 20px; background: #f5f5f5; }
h1 { color: #c0392b; }
.back { margin-bottom: 16px; display: inline-block; color: #2980b9; text-decoration: none; }
.card { background: white; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 14px; padding: 14px; }
.card.kept { opacity: 0.4; border-color: #27ae60; }
.card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.card-name { font-size: 15px; font-weight: bold; color: #2c3e50; }
.card-id { font-size: 11px; color: #999; margin-left: 6px; }
.reasons { margin: 8px 0 10px; }
.reason-tag { display: inline-block; background: #fdecea; color: #c0392b; border: 1px solid #f5c6c0;
              border-radius: 3px; padding: 2px 7px; font-size: 11px; margin: 2px 3px 2px 0; }
.meta { font-size: 11px; color: #555; margin-top: 6px; line-height: 1.7; }
.meta a { color: #2980b9; word-break: break-all; }
.btns { display: flex; gap: 8px; flex-shrink: 0; }
.btn-delete { padding: 5px 14px; background: #c0392b; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
.btn-keep   { padding: 5px 14px; background: #27ae60; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
.btn-delete:disabled, .btn-keep:disabled { opacity: 0.5; cursor: default; }
.count { color: #555; font-size: 13px; margin-bottom: 12px; }
</style>
</head>
<body>
<a class="back" href="/">← Back to dashboard</a>
<h1>&#9888; Suspect Records</h1>
<p class="count">{{ suspects|length }} record(s) flagged for review. Check each one and Delete or Keep.</p>

{% for s in suspects %}
{% set c = s.record %}
<div class="card" id="card-{{ c.id }}">
  <div class="card-header">
    <div>
      <span class="card-name">{{ c.company_name or '(no name)' }}</span>
      <span class="card-id">ID {{ c.id }}</span>
      <div class="reasons">
        {% for r in s.reasons %}
        <span class="reason-tag">{{ r }}</span>
        {% endfor %}
      </div>
      <div class="meta">
        {% if c.website %}<div><b>Website:</b> <a href="{{ c.website }}" target="_blank">{{ c.website }}</a></div>{% endif %}
        {% if c.facebook_url %}<div><b>Facebook:</b> <a href="{{ c.facebook_url }}" target="_blank">{{ c.facebook_url }}</a></div>{% endif %}
        {% if c.source_url and c.source_url != c.website and c.source_url != c.facebook_url %}
          <div><b>Found via:</b> <a href="{{ c.source_url }}" target="_blank">{{ c.source_url }}</a></div>
        {% endif %}
        <div><b>Region:</b> {{ c.region or '—' }} &nbsp;|&nbsp; <b>Notes:</b> {{ c.notes or '—' }}</div>
        {% if c.fb_description %}<div><b>FB description:</b> {{ c.fb_description[:200] }}</div>{% endif %}
      </div>
    </div>
    <div class="btns">
      <button class="btn-keep"   id="keep-{{ c.id }}"   onclick="keepRecord({{ c.id }})">&#10003; Keep</button>
      <button class="btn-delete" id="del-{{ c.id }}"    onclick="deleteRecord({{ c.id }}, '{{ (c.company_name or '') | replace("'", "\\\\'") }}')">&#10005; Delete</button>
    </div>
  </div>
</div>
{% else %}
<p style="color:#27ae60; font-weight:bold;">No suspect records found — database looks clean.</p>
{% endfor %}

<script>
function keepRecord(id) {
  document.getElementById('card-' + id).classList.add('kept');
  document.getElementById('keep-' + id).disabled = true;
  document.getElementById('del-' + id).disabled = true;
}
function deleteRecord(id, name) {
  if (!confirm('Delete "' + name + '"?\\nThis cannot be undone.')) return;
  fetch('/delete-company', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      var card = document.getElementById('card-' + id);
      if (card) card.remove();
    } else {
      alert('Delete failed: ' + (d.error || 'unknown'));
    }
  })
  .catch(function() { alert('Delete request failed.'); });
}
</script>
</body>
</html>
"""


@app.route("/update-company", methods=["POST"])
def update_company():
    """Update editable fields on a company record."""
    company_id = request.json.get("id")
    if not company_id:
        return jsonify({"error": "No id provided"}), 400
    allowed = {"company_name", "website", "email", "phone", "region"}
    update = {k: v for k, v in request.json.items() if k in allowed and v is not None}
    if not update:
        return jsonify({"error": "No valid fields to update"}), 400
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=headers, json=update,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


CORRECTIONS_FILE = os.path.join(BASE_DIR, "corrections.md")


@app.route("/save-correction", methods=["POST"])
def save_correction():
    """Save a correction note, parse with Claude, block false match, auto-recheck, learn."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("company_name", "Unknown")
    correction = request.json.get("correction", "").strip()
    if not correction:
        return jsonify({"error": "No correction text provided"}), 400
    try:
        from datetime import datetime as _dt
        from searcher import parse_and_save_correction, write_audit, apply_correction_and_recheck

        # 1. Write to human-readable markdown log
        timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n## {company_name} (ID: {company_id}) — {timestamp}\n{correction}\n"
        with open(CORRECTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(entry)

        # 2. Fetch existing PSPLA name and region from DB (needed for recheck)
        headers_sb = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        row_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=pspla_name,region",
            headers=headers_sb, timeout=10
        )
        row = row_resp.json()[0] if row_resp.ok and row_resp.json() else {}
        old_pspla_name = row.get("pspla_name") or ""
        website_region = row.get("region") or ""

        # 3. Parse correction with Claude and save to corrections.json
        parsed = parse_and_save_correction(company_name, company_id, correction)
        correction_type = parsed.get("type", "other")
        summary = parsed.get("summary", correction)
        blocked_name = parsed.get("blocked_pspla_name", "")
        print(f"  [Correction saved] {company_name}: type={correction_type}, blocked={blocked_name}")

        # 4. Log to audit
        write_audit("correction", company_id, company_name,
                    changes=f"Type: {correction_type} | {summary}" + (f" | Blocked PSPLA: {blocked_name}" if blocked_name else ""),
                    triggered_by="manual (dashboard)",
                    notes=correction)

        # 5. Run recheck and generate lesson — but don't save to DB yet, return for user review
        proposed = None
        lesson_rule = None
        if old_pspla_name:
            from searcher import check_pspla, _generate_and_save_lesson
            new_result = check_pspla(company_name, website_region=website_region)
            lesson = _generate_and_save_lesson(company_name, old_pspla_name, new_result)
            lesson_rule = lesson.get("rule_to_apply", "")
            proposed = {
                "licensed": new_result.get("licensed"),
                "pspla_name": new_result.get("matched_name"),
                "pspla_address": new_result.get("pspla_address"),
                "pspla_license_number": new_result.get("pspla_license_number"),
                "pspla_license_status": new_result.get("pspla_license_status"),
                "pspla_license_expiry": new_result.get("pspla_license_expiry"),
                "license_type": new_result.get("license_type"),
                "match_method": new_result.get("match_method"),
                "match_reason": new_result.get("match_reason"),
            }

        return jsonify({
            "ok": True,
            "type": correction_type,
            "summary": summary,
            "proposed": proposed,
            "lesson_rule": lesson_rule,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/confirm-recheck", methods=["POST"])
def confirm_recheck():
    """User approved or rejected the proposed PSPLA match after a correction."""
    from flask import jsonify
    from searcher import write_audit
    company_id  = request.json.get("id")
    company_name = request.json.get("company_name", "Unknown")
    approved    = request.json.get("approved", False)
    proposed    = request.json.get("proposed", {})

    if approved and proposed:
        patch = {k: v for k, v in proposed.items() if v is not None}
        patch["pspla_licensed"] = proposed.get("licensed")  # always include
    else:
        # Rejected — clear the PSPLA fields
        patch = {
            "pspla_licensed": False,
            "pspla_name": None,
            "pspla_license_number": None,
            "pspla_license_status": None,
            "pspla_license_expiry": None,
            "license_type": None,
            "match_method": "correction-rejected",
        }

    try:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                   "Content-Type": "application/json", "Prefer": "return=minimal"}
        requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                       headers=headers, json=patch, timeout=15)
        action = "approved" if approved else "rejected"
        write_audit("updated", company_id, company_name,
                    changes=f"Recheck {action} by user. New PSPLA: {proposed.get('pspla_name') if approved else 'None'}",
                    triggered_by="manual (correction confirm)")
        return jsonify({"ok": True, "approved": approved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/delete-company", methods=["POST"])
def delete_company():
    """Delete a single company record by ID."""
    from flask import jsonify
    company_id = request.json.get("id")
    if not company_id:
        return jsonify({"error": "No id provided"}), 400
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer": "return=minimal",
        }
        # Fetch company name before deleting for audit log
        fetch_headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        fetch_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=company_name",
            headers=fetch_headers, timeout=10
        )
        company_name = ""
        if fetch_resp.ok:
            rows = fetch_resp.json()
            if rows:
                company_name = rows[0].get("company_name", "")
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=headers,
        )
        if resp.status_code in (200, 204):
            from searcher import write_audit
            write_audit("deleted", company_id, company_name, triggered_by="manual (dashboard)")
            return jsonify({"ok": True})
        return jsonify({"error": f"Supabase returned {resp.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_search_proc = None   # module-level reference to the running subprocess
_was_running = False  # tracks previous poll state for crash detection


def _detect_and_mark_crashes():
    """Scan search_history.json for stale 'running' entries and mark them 'crashed'.
    Called when the dashboard detects a search process just died without writing a final entry."""
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        return
    changed = False
    for entry in history:
        if entry.get("status") == "running":
            entry["status"] = "crashed"
            entry["finished"] = datetime.now(timezone.utc).isoformat()
            if not entry.get("notes"):
                entry["notes"] = "Process exited without writing a completion record. Check search_log.txt for details."
            changed = True
    if changed:
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
            print("  [Crash detected] Marked stale 'running' history entries as 'crashed'.")
        except Exception as e:
            print(f"  [Crash detection write error] {e}")


def _search_process_alive():
    """Return True if a search subprocess is currently running."""
    global _search_proc
    # Primary: we launched it in this session — most reliable
    if _search_proc is not None and _search_proc.poll() is None:
        return True
    _search_proc = None
    # Fallback 1: RUNNING_FLAG exists and is less than 8 hours old
    if os.path.exists(RUNNING_FLAG):
        age = _time.time() - os.path.getmtime(RUNNING_FLAG)
        if age < 28800:
            return True
        # Flag is stale — clean up
        for path in [RUNNING_FLAG, PAUSE_FLAG, PID_FILE]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    # Fallback 2: search_status.json was written within the last 90 seconds
    # (the search scripts write it on every region+term iteration — reliable heartbeat)
    if os.path.exists(STATUS_FILE):
        age = _time.time() - os.path.getmtime(STATUS_FILE)
        if age < 90:
            return True
    return False


def _launch(script, args=None, triggered_by="manual"):
    """Launch a search script as a subprocess, capturing output to search_log.txt."""
    global _search_proc
    if _search_process_alive():
        return
    # Map script filename to search type label
    _type_map = {
        "searcher.py": "full", "run_weekly.py": "google-weekly",
        "run_facebook.py": "facebook", "run_partial.py": "google-partial",
        "run_directories.py": "directories", "run_recheck.py": "bulk-recheck",
    }
    started_iso = datetime.now(timezone.utc).isoformat()
    try:
        with open(START_FILE, "w") as f:
            json.dump({"started": started_iso, "type": _type_map.get(script, script),
                       "triggered_by": triggered_by}, f)
    except Exception:
        pass
    cmd = ["python", "-u", os.path.join(BASE_DIR, script)] + (args or [])
    log = open(LOG_FILE, "w", encoding="utf-8", buffering=1)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    _search_proc = subprocess.Popen(
        cmd, cwd=BASE_DIR,
        stdout=log, stderr=log, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(_search_proc.pid))
    except Exception:
        pass


def _scheduled_full():
    if os.path.exists(SCHEDULE_FLAG):
        _launch("searcher.py", ["--scheduled"], triggered_by="scheduled")


def _scheduled_weekly():
    if os.path.exists(SCHEDULE_FLAG):
        _launch("run_weekly.py", ["--scheduled"], triggered_by="scheduled")


def _scheduled_facebook():
    if os.path.exists(SCHEDULE_FLAG):
        _launch("run_facebook.py", ["--scheduled"], triggered_by="scheduled")


def _scheduled_directories():
    if os.path.exists(SCHEDULE_FLAG):
        _launch("run_directories.py", ["--scheduled"], triggered_by="scheduled")


if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Pacific/Auckland")
    # Full search: 1st of each month at 2am NZ
    scheduler.add_job(_scheduled_full, CronTrigger(day=1, hour=2, minute=0),
                      id="full", name="Full search (monthly)")
    # Google weekly: 8th, 15th, 22nd at 2am NZ
    scheduler.add_job(_scheduled_weekly, CronTrigger(day="8,15,22", hour=2, minute=0),
                      id="weekly", name="Google weekly scan")
    # Facebook: Tuesday and Friday at 3am NZ
    scheduler.add_job(_scheduled_facebook, CronTrigger(day_of_week="tue,fri", hour=3, minute=0),
                      id="facebook", name="Facebook scan")
    # Directory import (NZSA + LinkedIn): 15th of each month at 4am NZ
    scheduler.add_job(_scheduled_directories, CronTrigger(day=15, hour=4, minute=0),
                      id="directories", name="Directory import (NZSA + LinkedIn)")

    scheduler.start()
    print("Dashboard running at http://localhost:5000")
    print("Scheduler started — scheduled searches run automatically when enabled.")
    app.run(host="0.0.0.0", port=5000, debug=False)
