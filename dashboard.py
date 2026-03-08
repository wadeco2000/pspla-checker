import os
import csv
import io
import json
import time as _time
import subprocess
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
from flask import Flask, render_template_string, redirect, url_for, request, Response
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GITHUB_PAT = os.getenv("GITHUB_PAT")
EXPORT_PASSWORD = os.getenv("EXPORT_PASSWORD") or os.getenv("PAGES_PASSWORD", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "wadeco2000/pspla-checker")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
STATUS_FILE = os.path.join(BASE_DIR, "search_status.json")
HISTORY_FILE = os.path.join(BASE_DIR, "search_history.json")
SCHEDULE_FLAG = os.path.join(BASE_DIR, "schedule_enabled.flag")
TERMS_FILE = os.path.join(BASE_DIR, "search_terms.json")
PARTIAL_CONFIG_FILE = os.path.join(BASE_DIR, "partial_config.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "search_progress.json")
PID_FILE = os.path.join(BASE_DIR, "search_pid.txt")
LOG_FILE = os.path.join(BASE_DIR, "search_log.txt")
START_FILE = os.path.join(BASE_DIR, "search_start.json")

NZ_REGIONS = [
    "Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga",
    "Dunedin", "Palmerston North", "Napier", "New Plymouth", "Whangarei",
    "Nelson", "Invercargill", "Gisborne", "Whanganui", "Rotorua",
    "Hastings", "Blenheim", "Timaru", "Pukekohe", "Taupo",
    "Kerikeri", "Kaitaia", "Dargaville",
    "Lower Hutt", "Upper Hutt", "Porirua", "Paraparaumu",
    "Thames", "Te Awamutu", "Tokoroa",
    "Whakatane", "Katikati", "Te Puke",
    "Waipukurau", "Wairoa",
    "Hawera", "Stratford",
    "Levin", "Feilding",
    "Motueka", "Richmond",
    "Picton",
    "Greymouth", "Westport",
    "Rangiora", "Ashburton", "Rolleston",
    "Queenstown", "Wanaka", "Oamaru", "Alexandra",
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PSPLA Security Camera Checker</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"
          integrity="sha512-Avb2QiuDEEvB4bZJYdft2mNjVShBftLdPG8FJ0V7irTLQ8Uum05M9pHhS2Cjx1APTA6wF/hNKF7D5+q/ue5Q=="
          crossorigin="anonymous" referrerpolicy="no-referrer" />
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
        .fb-tag { display:inline-block; background:#1877f2; color:white; border-radius:4px;
                  padding:1px 5px; font-size:10px; font-weight:bold; margin-left:5px;
                  vertical-align:middle; white-space:nowrap; }
        .fb-tag i { font-size:9px; }
        .status-icon { margin-right:4px; }
    </style>
</head>
<body>
    <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
        <div>
            <h1>PSPLA Security Camera Company Checker</h1>
            <p class="subtitle">NZ companies found installing security cameras — checked against PSPLA licensing register.</p>
        </div>
        <div style="display:flex; flex-direction:column; gap:6px; align-items:flex-end; margin-top:10px;">
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:nowrap;">
                {% if search_running %}
                    <span style="font-size:12px; color:#27ae60; font-weight:bold; white-space:nowrap;"><i class="fa-solid fa-circle" style="font-size:9px;"></i> Search running</span>
                    {% if search_paused %}
                        <form method="POST" action="/resume-search">
                            <button class="btn" style="background:#27ae60; color:white;"><i class="fa-solid fa-play"></i> Resume</button>
                        </form>
                    {% else %}
                        <form method="POST" action="/pause-search">
                            <button class="btn" style="background:#e67e22; color:white;"><i class="fa-solid fa-pause"></i> Pause</button>
                        </form>
                    {% endif %}
                    <form method="POST" action="/stop-search" onsubmit="return confirm('Stop the running search? Progress so far will be saved.')">
                        <button class="btn" style="background:#c0392b; color:white;"><i class="fa-solid fa-stop"></i> Stop</button>
                    </form>
                {% else %}
                    <form method="POST" action="/start-search" onsubmit="return confirm('Start a full search? This will run in the background and may take a long time.')">
                        <button class="btn" style="background:#27ae60; color:white;">&#9654; Full Search</button>
                    </form>
                    <form method="POST" action="/start-weekly-search" onsubmit="return confirm('Run a weekly light scan (last 7 days only)?')">
                        <button class="btn" style="background:#16a085; color:white;">&#9654; Weekly Scan</button>
                    </form>
                    <form method="POST" action="/start-facebook-search" onsubmit="return confirm('Run Facebook-only search? This adds Facebook-sourced companies on top of existing data.')">
                        <button class="btn" style="background:#1877f2; color:white;"><i class="fa-brands fa-facebook-f"></i> Facebook</button>
                    </form>
                {% endif %}
                <form method="POST" action="/dedupe-db" onsubmit="return confirm('Merge duplicate company names? Keeps the best record and combines all regions into one entry.')">
                    <button class="btn" style="background:#8e44ad; color:white;"><i class="fa-solid fa-filter"></i> Dedupe DB</button>
                </form>
                <button class="btn" style="background:#e74c3c; color:white;"
                    onclick="document.getElementById('clear-db-modal').style.display='flex';">
                    <i class="fa-solid fa-trash-can"></i> Clear DB
                </button>
            </div>
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:nowrap;">
                <form method="POST" action="/publish" onsubmit="return confirm('Publish current data to the live GitHub Pages site?')">
                    <button class="btn btn-dark" style="background:#8e44ad;">&#x1F310; Publish Live</button>
                </form>
                <button class="btn btn-dark" onclick="document.getElementById('export-modal').style.display='flex';">&#x2B07; Export CSV</button>
                <a href="/history" class="btn btn-dark" style="text-decoration:none;">&#x1F4DC; Version History</a>
            </div>
        </div>
    </div>

    {% set _s = init_status %}
    {% set _pct = ((_s.region_idx - 1) / _s.total_regions * 100) | round | int if _s.region_idx and _s.total_regions else 0 %}
    {% set _phase = 'Facebook' if _s.phase == 'facebook' else 'Google' %}
    {% set _paused_txt = ' — PAUSED' if search_paused else '' %}
    {% set _label = _phase ~ ' search: region ' ~ _s.region_idx ~ ' of ' ~ _s.total_regions ~ ' — ' ~ (_s.region or '') ~ _paused_txt if _s.region_idx else _phase ~ ' search starting...' ~ _paused_txt %}
    {% set _term_txt = 'Term ' ~ _s.term_idx ~ ' of ' ~ _s.total_terms ~ ': ' ~ (_s.term or '') if _s.term_idx else '' %}
    {% set _bar_color = '#1877f2' if _s.phase == 'facebook' else '#27ae60' %}
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

        function poll() {
            fetch('/search-status')
                .then(function(r) { return r.json(); })
                .then(function(s) {
                    if (!s.running) { wrap.style.display = 'none'; return; }
                    wrap.style.display = 'block';
                    var phase = s.phase === 'facebook' ? 'Facebook' : 'Google';
                    var paused = s.paused ? ' — PAUSED' : '';
                    if (s.region_idx != null && s.total_regions != null) {
                        label.textContent = phase + ' search: region ' + s.region_idx + ' of ' + s.total_regions + ' — ' + (s.region || '') + paused;
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
                })
                .catch(function() {});
        }
        poll();
        setInterval(poll, 3000);
    })();

    // Search history
    (function() {
        var TYPE_LABELS = {'full':'Full','google-weekly':'Weekly','facebook':'Facebook','google-partial':'Partial'};
        var STATUS_COLORS = {'completed':'#27ae60','stopped':'#e67e22','error':'#e74c3c'};
        function loadHistory() {
            fetch('/search-history')
                .then(function(r){return r.json();})
                .then(function(rows) {
                    var el = document.getElementById('history-table');
                    if (!rows.length) { el.innerHTML='<span style="color:#aaa;">No searches recorded yet.</span>'; return; }
                    var html = '<table style="width:100%;border-collapse:collapse;">'
                        + '<tr style="color:#888;border-bottom:1px solid #eee;">'
                        + '<th style="text-align:left;padding:3px 6px;font-weight:normal;">Date (NZT)</th>'
                        + '<th style="text-align:left;padding:3px 6px;font-weight:normal;">Type</th>'
                        + '<th style="text-align:left;padding:3px 6px;font-weight:normal;">By</th>'
                        + '<th style="text-align:right;padding:3px 6px;font-weight:normal;">Mins</th>'
                        + '<th style="text-align:right;padding:3px 6px;font-weight:normal;">Found</th>'
                        + '<th style="text-align:right;padding:3px 6px;font-weight:normal;">New</th>'
                        + '<th style="text-align:left;padding:3px 6px;font-weight:normal;">Status</th></tr>';
                    rows.forEach(function(r) {
                        var d = new Date(r.started.replace('+00:00','Z'));
                        var dt = d.toLocaleDateString('en-NZ',{day:'2-digit',month:'short'})
                               + ' ' + d.toLocaleTimeString('en-NZ',{hour:'2-digit',minute:'2-digit'});
                        var col = STATUS_COLORS[r.status] || '#888';
                        var lbl = TYPE_LABELS[r.type] || r.type;
                        html += '<tr style="border-bottom:1px solid #f5f5f5;">'
                            + '<td style="padding:3px 6px;color:#555;">' + dt + '</td>'
                            + '<td style="padding:3px 6px;">' + lbl + '</td>'
                            + '<td style="padding:3px 6px;color:#888;">' + (r.triggered_by||'') + '</td>'
                            + '<td style="padding:3px 6px;text-align:right;color:#888;">' + (r.duration_minutes||'-') + '</td>'
                            + '<td style="padding:3px 6px;text-align:right;">' + (r.total_found||0) + '</td>'
                            + '<td style="padding:3px 6px;text-align:right;font-weight:bold;">' + (r.total_new||0) + '</td>'
                            + '<td style="padding:3px 6px;color:' + col + ';font-weight:bold;">' + r.status + '</td></tr>';
                    });
                    html += '</table>';
                    el.innerHTML = html;
                }).catch(function(){});
        }
        loadHistory();
        setInterval(loadHistory, 15000);
    })();
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

    <div style="display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap;">

        <div style="flex:1; min-width:260px; background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                <strong style="color:#2c3e50; font-size:14px;">&#128197; Scheduled Searches</strong>
                <form method="POST" action="/toggle-schedule" style="margin:0;">
                    <button class="btn" style="padding:4px 10px; font-size:12px;
                        background:{{ '#27ae60' if schedule_enabled else '#95a5a6' }}; color:white;">
                        {{ 'Enabled' if schedule_enabled else 'Disabled' }}
                    </button>
                </form>
            </div>
            <table style="width:100%; font-size:12px; border-collapse:collapse;">
                <tr><td style="color:#666; padding:2px 0;"><i class="fa-solid fa-magnifying-glass" style="width:14px;"></i> Full search</td><td style="color:#2c3e50;">1st of month, 2am</td></tr>
                <tr><td style="color:#666; padding:2px 0;"><i class="fa-solid fa-calendar-week" style="width:14px;"></i> Weekly scan</td><td style="color:#2c3e50;">8th, 15th, 22nd, 2am</td></tr>
                <tr><td style="color:#666; padding:2px 0;"><i class="fa-brands fa-facebook-f" style="width:14px; color:#1877f2;"></i> Facebook scan</td><td style="color:#2c3e50;">Tue &amp; Fri, 3am</td></tr>
            </table>
        </div>

        <div style="flex:2; min-width:360px; background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
            <strong style="color:#2c3e50; font-size:14px;">&#128196; Search History</strong>
            <div id="history-table" style="margin-top:10px; font-size:12px;">
                {% if not init_history %}
                <span style="color:#aaa;">No searches recorded yet.</span>
                {% else %}
                <table style="width:100%;border-collapse:collapse;">
                <tr style="color:#888;border-bottom:1px solid #eee;">
                    <th style="text-align:left;padding:3px 6px;font-weight:normal;">Date (NZT)</th>
                    <th style="text-align:left;padding:3px 6px;font-weight:normal;">Type</th>
                    <th style="text-align:left;padding:3px 6px;font-weight:normal;">By</th>
                    <th style="text-align:right;padding:3px 6px;font-weight:normal;">Mins</th>
                    <th style="text-align:right;padding:3px 6px;font-weight:normal;">Found</th>
                    <th style="text-align:right;padding:3px 6px;font-weight:normal;">New</th>
                    <th style="text-align:left;padding:3px 6px;font-weight:normal;">Status</th>
                </tr>
                {% set type_labels = {'full':'Full','google-weekly':'Weekly','facebook':'Facebook','google-partial':'Partial'} %}
                {% set status_colors = {'completed':'#27ae60','stopped':'#e67e22','error':'#e74c3c'} %}
                {% for r in init_history %}
                <tr style="border-bottom:1px solid #f5f5f5;">
                    <td style="padding:3px 6px;color:#555;">{{ r.started[:16].replace('T',' ') if r.started else '-' }}</td>
                    <td style="padding:3px 6px;">{{ type_labels.get(r.type, r.type) }}</td>
                    <td style="padding:3px 6px;color:#888;">{{ r.triggered_by or '' }}</td>
                    <td style="padding:3px 6px;text-align:right;color:#888;">{{ r.duration_minutes or '-' }}</td>
                    <td style="padding:3px 6px;text-align:right;">{{ r.total_found or 0 }}</td>
                    <td style="padding:3px 6px;text-align:right;font-weight:bold;">{{ r.total_new or 0 }}</td>
                    <td style="padding:3px 6px;color:{{ status_colors.get(r.status, '#888') }};font-weight:bold;">{{ r.status }}</td>
                </tr>
                {% endfor %}
                </table>
                {% endif %}
            </div>
        </div>

    </div>

    <!-- Search Terms + Partial Search row -->
    <div style="display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap;">

        <!-- Search Terms Editor -->
        <div style="flex:1; min-width:300px; background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
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

        <!-- Partial Search Panel -->
        <div style="flex:2; min-width:380px; background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
            <strong style="color:#2c3e50; font-size:14px;"><i class="fa-solid fa-crosshairs"></i> Partial Search</strong>
            <div style="display:flex; gap:12px; margin-top:10px; flex-wrap:wrap;">

                <!-- Region selector -->
                <div style="flex:1; min-width:160px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="font-size:12px; font-weight:bold; color:#555;"><i class="fa-solid fa-location-dot"></i> Regions</label>
                        <span style="font-size:11px;">
                            <a href="#" onclick="partialSelectAllRegions(); return false;" style="color:#2980b9;">All</a> /
                            <a href="#" onclick="partialClearRegions(); return false;" style="color:#2980b9;">None</a>
                        </span>
                    </div>
                    <div id="partial-region-list"
                        style="max-height:190px; overflow-y:auto; border:1px solid #ddd; border-radius:4px; padding:5px; font-size:12px;">
                    </div>
                </div>

                <!-- Terms selector -->
                <div style="flex:1; min-width:200px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="font-size:12px; font-weight:bold; color:#555;"><i class="fa-solid fa-magnifying-glass"></i> Terms</label>
                        <span style="font-size:11px;">
                            <a href="#" onclick="partialSelectAllTerms(); return false;" style="color:#2980b9;">All</a> /
                            <a href="#" onclick="partialClearTerms(); return false;" style="color:#2980b9;">None</a>
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
                <button onclick="runPartialSearch()"
                    style="padding:7px 18px; background:#8e44ad; color:white; border:none; border-radius:5px;
                           cursor:pointer; font-size:13px; font-weight:bold;">
                    <i class="fa-solid fa-play"></i> Run Partial Search
                </button>
                <span id="partial-status" style="font-size:12px; color:#888;"></span>
            </div>
        </div>

    </div>

    <script>
    // ── Search Terms Manager ──────────────────────────────────────────────────
    var _terms = {{ init_terms | tojson }};
    var _activeTab = 'google';

    function showTermsTab(tab) {
        _activeTab = tab;
        document.getElementById('terms-google').style.display = tab === 'google' ? '' : 'none';
        document.getElementById('terms-facebook').style.display = tab === 'facebook' ? '' : 'none';
        document.getElementById('tab-btn-google').style.background = tab === 'google' ? '#2c3e50' : 'white';
        document.getElementById('tab-btn-google').style.color = tab === 'google' ? 'white' : '#555';
        document.getElementById('tab-btn-facebook').style.background = tab === 'facebook' ? '#1877f2' : 'white';
        document.getElementById('tab-btn-facebook').style.color = tab === 'facebook' ? 'white' : '#555';
    }

    function renderTermsList(type) {
        var el = document.getElementById('terms-list-' + type);
        if (!el) return;
        if (!_terms[type].length) { el.innerHTML = '<span style="color:#aaa;">No terms yet.</span>'; return; }
        el.innerHTML = _terms[type].map(function(t, i) {
            return '<div style="display:flex; justify-content:space-between; align-items:center; padding:2px 0; border-bottom:1px solid #f5f5f5;">'
                + '<span>' + t + '</span>'
                + '<button onclick="removeTerm(\'' + type + '\',' + i + ')" '
                + 'style="background:none; border:none; color:#e74c3c; cursor:pointer; font-size:13px; padding:0 4px;" title="Remove">&times;</button>'
                + '</div>';
        }).join('');
    }

    function loadTerms() {
        fetch('/search-terms').then(function(r){return r.json();}).then(function(data) {
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
        fetch('/save-terms', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
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

    // ── Partial Search ────────────────────────────────────────────────────────
    var _allRegions = {{ nz_regions | tojson }};

    function renderPartialRegions() {
        var el = document.getElementById('partial-region-list');
        el.innerHTML = _allRegions.map(function(r) {
            return '<label style="display:block; padding:1px 2px; cursor:pointer;">'
                + '<input type="checkbox" class="partial-region-cb" value="' + r + '" checked style="margin-right:4px;">'
                + r + '</label>';
        }).join('');
    }

    function renderPartialTerms() {
        var el = document.getElementById('partial-term-list');
        el.innerHTML = _terms.google.map(function(t) {
            return '<label style="display:block; padding:1px 2px; cursor:pointer;">'
                + '<input type="checkbox" class="partial-term-cb" value="' + t + '" checked style="margin-right:4px;">'
                + t + '</label>';
        }).join('');
    }

    function partialSelectAllRegions() {
        document.querySelectorAll('.partial-region-cb').forEach(function(cb){ cb.checked = true; });
    }
    function partialClearRegions() {
        document.querySelectorAll('.partial-region-cb').forEach(function(cb){ cb.checked = false; });
    }
    function partialSelectAllTerms() {
        document.querySelectorAll('.partial-term-cb').forEach(function(cb){ cb.checked = true; });
    }
    function partialClearTerms() {
        document.querySelectorAll('.partial-term-cb').forEach(function(cb){ cb.checked = false; });
    }

    function runPartialSearch() {
        var regions = Array.from(document.querySelectorAll('.partial-region-cb:checked')).map(function(cb){ return cb.value; });
        var terms = Array.from(document.querySelectorAll('.partial-term-cb:checked')).map(function(cb){ return cb.value; });
        var extraRaw = document.getElementById('partial-extra-terms').value.trim();
        var extraTerms = extraRaw ? extraRaw.split('\n').map(function(t){ return t.trim(); }).filter(Boolean) : [];
        var allTerms = terms.concat(extraTerms);
        var includeFb = document.getElementById('partial-facebook').checked;
        var includeFbNw = document.getElementById('partial-facebook-nz').checked;

        if (!regions.length) { alert('Please select at least one region.'); return; }
        if (!allTerms.length && !includeFb && !includeFbNw) { alert('Please select at least one term or enable Facebook search.'); return; }

        var statusEl = document.getElementById('partial-status');
        statusEl.textContent = 'Starting...';

        fetch('/start-partial-search', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({regions: regions, google_terms: allTerms, include_facebook: includeFb, include_nationwide: includeFbNw})
        }).then(function(r){ return r.json(); }).then(function(d) {
            if (d.ok) {
                statusEl.textContent = 'Search started!';
                setTimeout(function(){ statusEl.textContent = ''; }, 4000);
            } else {
                statusEl.textContent = d.error || 'Error starting search.';
            }
        }).catch(function(){ statusEl.textContent = 'Request failed.'; });
    }

    loadTerms();
    renderPartialRegions();
    </script>

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
                <th><i class="fa-solid fa-building"></i> Company (Website)</th>
                <th><i class="fa-solid fa-location-dot"></i> Region</th>
                <th><i class="fa-solid fa-phone"></i> Phone</th>
                <th><i class="fa-solid fa-envelope"></i> Email</th>
                <th><i class="fa-solid fa-shield-halved"></i> PSPLA Status</th>
                <th><i class="fa-solid fa-id-card"></i> PSPLA Registered Name</th>
                <th><i class="fa-solid fa-hashtag"></i> License #</th>
                <th><i class="fa-regular fa-calendar"></i> Expiry</th>
                <th><i class="fa-solid fa-landmark"></i> Companies Office</th>
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
                data-id="{{ loop.index }}">
                <td class="company-cell">
                    {% if c.website %}<a href="{{ c.website }}" target="_blank">{{ c.company_name or '-' }}</a>{% else %}{{ c.company_name or '-' }}{% endif %}
                    {% if c.facebook_url %}
                        <a href="{{ c.facebook_url }}" target="_blank" class="fb-tag" title="Facebook page"><i class="fa-brands fa-facebook-f"></i></a>
                    {% elif c.source_url and 'facebook.com' in c.source_url %}
                        <a href="{{ c.source_url }}" target="_blank" class="fb-tag" title="Found via Facebook"><i class="fa-brands fa-facebook-f"></i></a>
                    {% endif %}
                </td>
                <td>{{ c.region or '-' }}</td>
                <td>{{ c.phone or '-' }}</td>
                <td>{% if c.email %}<a href="mailto:{{ c.email }}">{{ c.email }}</a>{% else %}-{% endif %}</td>
                <td>
                    {% if lic == 'true' %}
                        <span class="badge badge-licensed"><i class="fa-solid fa-circle-check status-icon"></i>LICENSED</span>
                    {% elif c.pspla_license_status and c.pspla_license_status|lower == 'expired' %}
                        <span class="badge badge-expired"><i class="fa-solid fa-triangle-exclamation status-icon"></i>EXPIRED</span>
                    {% elif lic == 'false' and c.individual_license %}
                        <span class="badge badge-expired"><i class="fa-solid fa-user-check status-icon"></i>INDIVIDUAL ONLY</span>
                    {% elif lic == 'false' %}
                        <span class="badge badge-unlicensed"><i class="fa-solid fa-circle-xmark status-icon"></i>NOT LICENSED</span>
                    {% else %}
                        <span class="badge badge-unknown"><i class="fa-solid fa-circle-question status-icon"></i>UNKNOWN</span>
                    {% endif %}
                </td>
                <td>
                    {{ c.pspla_name or '-' }}
                    {% if c.pspla_address %}<div class="detail-block">{{ c.pspla_address }}</div>{% endif %}
                </td>
                <td>
                    {% if c.pspla_license_number %}
                        <a href="https://forms.justice.govt.nz/search/PSPLA/" target="_blank"
                           title="Click to copy licence number &amp; open PSPLA register"
                           onclick="copyAndOpen(event, '{{ c.pspla_license_number }}')">{{ c.pspla_license_number }}</a>
                    {% else %}-{% endif %}
                </td>
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
                <td colspan="10">
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
                        {% if c.source_url and 'facebook.com' in c.source_url %}
                        <div class="detail-item"><label><i class="fa-brands fa-facebook" style="color:#1877f2;"></i> Facebook Page</label><span><a href="{{ c.source_url }}" target="_blank">{{ c.source_url }}</a></span></div>
                        {% endif %}
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

        function copyAndOpen(e, licNum) {
            e.preventDefault();
            navigator.clipboard.writeText(licNum).catch(() => {});
            const link = e.currentTarget;
            const orig = link.title;
            link.title = 'Copied! Paste into the PSPLA search box.';
            setTimeout(() => { link.title = orig; }, 3000);
            window.open('https://forms.justice.govt.nz/search/PSPLA/', '_blank');
        }
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
        <p style="color:#888; font-size:13px; margin-bottom:24px;">
            Search progress will also be reset so the next full search starts from scratch.
            <strong>This cannot be undone.</strong>
        </p>
        <form method="POST" action="/clear-db">
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

    search_alive = _search_process_alive()
    search_paused = search_alive and os.path.exists(PAUSE_FLAG)

    # Read live status for server-side progress bar pre-population
    init_status = {}
    if search_alive and os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                init_status = json.load(f)
        except Exception:
            pass

    # Pre-load history for immediate render (AJAX refreshes it later)
    init_history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                init_history = json.load(f)[:20]
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
        init_history=init_history,
        init_terms=init_terms,
        init_log_lines=init_log_lines,
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


@app.route("/start-facebook-search", methods=["POST"])
def start_facebook_search():
    try:
        _launch("run_facebook.py")
        return redirect(url_for("index", message="Facebook search started.", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start Facebook search: {e}", type="error"))


@app.route("/dedupe-db", methods=["POST"])
def dedupe_db():
    """Merge duplicate company names into one record, combining all regions.
    Keeps the record with the most contact info (phone/email), deletes the rest."""
    if _search_process_alive():
        return redirect(url_for("index", message="Cannot dedupe while a search is running — stop it first.", type="error"))
    try:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        patch_headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}

        # Fetch all — use high limit to avoid PostgREST default 1000-row cap
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?select=id,company_name,region,phone,email&order=id.asc&limit=10000",
            headers=headers)
        rows = resp.json()

        # Group by normalised name only
        groups = {}
        for row in rows:
            name = (row.get("company_name") or "").strip().lower()
            groups.setdefault(name, []).append(row)

        to_delete = []
        to_update = []  # (id, merged_region_string)

        for name_key, group in groups.items():
            if len(group) < 2:
                continue

            # Collect all unique regions across the group (preserving original capitalisation)
            seen_regions = []
            for r in group:
                for reg in (r.get("region") or "").split(","):
                    reg = reg.strip()
                    if reg and reg.lower() not in [s.lower() for s in seen_regions]:
                        seen_regions.append(reg)
            merged_region = ", ".join(seen_regions)

            # Keep the record with the most contact info, then lowest id
            def score(r):
                return (1 if r.get("phone") else 0) + (1 if r.get("email") else 0)
            group.sort(key=lambda r: (-score(r), r["id"]))
            keeper = group[0]
            to_update.append((keeper["id"], merged_region))
            for dup in group[1:]:
                to_delete.append(dup["id"])

        for rid, merged_region in to_update:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{rid}",
                headers=patch_headers,
                json={"region": merged_region})

        for rid in to_delete:
            requests.delete(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{rid}",
                            headers={**headers, "Content-Type": "application/json"})

        msg = f"Deduplication complete — {len(to_delete)} duplicate(s) merged and removed."
        return redirect(url_for("index", message=msg, type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Dedupe error: {e}", type="error"))


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
            for path in [PROGRESS_FILE, RUNNING_FLAG, PAUSE_FLAG, PID_FILE, START_FILE]:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            msg = "Database cleared — all entries and search state deleted."
            return redirect(url_for("index", message=msg, type="success"))
        else:
            return redirect(url_for("index", message=f"Delete failed: {response.text[:200]}", type="error"))
    except Exception as e:
        return redirect(url_for("index", message=f"Error: {e}", type="error"))


@app.route("/export.csv", methods=["POST"])
def export_csv():
    if EXPORT_PASSWORD and request.form.get("export_password") != EXPORT_PASSWORD:
        return redirect(url_for("index", message="Incorrect export password.", type="error"))
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


@app.route("/search-status")
def search_status():
    running = _search_process_alive()
    paused = running and os.path.exists(PAUSE_FLAG)
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
    from flask import jsonify
    return jsonify(status)


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
        with open(PARTIAL_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        _launch("run_partial.py", [])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/search-history")
def search_history():
    from flask import jsonify
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass
    return jsonify(history[:20])


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
    search_scripts = {"searcher.py", "run_weekly.py", "run_facebook.py", "run_partial.py"}
    our_pid = str(os.getpid())
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-WmiObject Win32_Process | Where-Object { $_.Name -eq 'python.exe' } "
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
                    subprocess.run(["taskkill", "/F", "/PID", pid],
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


_search_proc = None   # module-level reference to the running subprocess


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
    scheduler.start()
    print("Dashboard running at http://localhost:5000")
    print("Scheduler started — scheduled searches run automatically when enabled.")
    app.run(host="0.0.0.0", port=5000, debug=False)
