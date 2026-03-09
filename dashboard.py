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
SERPAPI_KEY  = os.getenv("SERPAPI_KEY")
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
        .nzsa-tag { display:inline-block; background:#c0392b; color:white; border-radius:4px;
                    padding:2px 6px; font-size:11px; font-weight:bold; margin-left:4px;
                    vertical-align:middle; white-space:nowrap; cursor:default; }
        .status-icon { margin-right:4px; }
    </style>
</head>
<body>
    <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
        <div>
            <h1>PSPLA Security Camera Company Checker</h1>
            <p class="subtitle">NZ companies found installing security cameras — checked against PSPLA licensing register.</p>
            <div id="api-credits-bar" style="display:flex; gap:14px; flex-wrap:wrap; margin-top:6px; font-size:12px;">
                <span id="credit-serp" style="color:#aaa;">
                    <i class="fa-solid fa-magnifying-glass"></i> SerpAPI: <span style="font-weight:bold;">loading…</span>
                </span>
                <span id="credit-tokens" style="color:#aaa;">
                    <i class="fa-solid fa-robot"></i> Claude: <span style="font-weight:bold;">–</span>
                </span>
            </div>
        </div>
        <div style="display:flex; flex-direction:column; gap:6px; align-items:flex-end; margin-top:10px;">
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:nowrap;">
                <span id="btns-running" style="display:{{ 'contents' if search_running else 'none' }};">
                    <span style="font-size:12px; color:#27ae60; font-weight:bold; white-space:nowrap;"><i class="fa-solid fa-circle" style="font-size:9px;"></i> Search running</span>
                    <form method="POST" action="/resume-search" id="btn-resume" style="display:{{ 'inline' if search_paused else 'none' }};">
                        <button class="btn" style="background:#27ae60; color:white;"><i class="fa-solid fa-play"></i> Resume</button>
                    </form>
                    <form method="POST" action="/pause-search" id="btn-pause" style="display:{{ 'none' if search_paused else 'inline' }};">
                        <button class="btn" style="background:#e67e22; color:white;"><i class="fa-solid fa-pause"></i> Pause</button>
                    </form>
                    <form method="POST" action="/stop-search" onsubmit="return confirm('Stop the running search? Progress so far will be saved.')">
                        <button class="btn" style="background:#c0392b; color:white;"><i class="fa-solid fa-stop"></i> Stop</button>
                    </form>
                </span>
                <span id="btns-idle" style="display:{{ 'none' if search_running else 'contents' }};">
                    <form method="POST" action="/start-search" onsubmit="return confirm('Start a full search? This will run in the background and may take a long time.')">
                        <button class="btn" style="background:#27ae60; color:white;">&#9654; Full Search</button>
                    </form>
                    <form method="POST" action="/start-weekly-search" onsubmit="return confirm('Run a weekly light scan (last 7 days only)?')">
                        <button class="btn" style="background:#16a085; color:white;">&#9654; Weekly Scan</button>
                    </form>
                    <form method="POST" action="/start-facebook-search" onsubmit="return confirm('Run Facebook-only search? This adds Facebook-sourced companies on top of existing data.')">
                        <button class="btn" style="background:#1877f2; color:white;"><i class="fa-brands fa-facebook-f"></i> Facebook</button>
                    </form>
                    <form method="POST" action="/start-directory-import" onsubmit="return confirm('Import from NZSA and LinkedIn directories? Adds members not already in the database.')">
                        <button class="btn" style="background:#c0392b; color:white;">&#9707; Directories</button>
                    </form>
                </span>
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
                <a href="/search-history" class="btn btn-dark" style="text-decoration:none;">&#x1F4CB; Search History</a>
                <a href="/history" class="btn btn-dark" style="text-decoration:none;">&#x1F4DC; Version History</a>
                <a href="/duplicates" class="btn btn-dark" style="text-decoration:none; background:#c0392b;">&#x26A0; Duplicates</a>
                <a href="/audit-log" class="btn btn-dark" style="text-decoration:none; background:#6c3483;">&#x1F4CB; Audit Log</a>
                <a href="/llm-log" class="btn btn-dark" style="text-decoration:none; background:#1a6e3c;">&#x1F916; LLM Log</a>
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

    </div>

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

    <!-- Search Terms + Partial Search row -->
    <div style="display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap;">

        <!-- Search Terms Editor -->
        <div style="flex:1; min-width:300px; background:white; border-radius:8px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1); padding:14px 18px;">
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
                function runPartialSearch() {
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
                    statusEl.textContent = 'Starting...';
                    fetch('/start-partial-search', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({regions: regions, google_terms: allTerms, include_facebook: includeFb, include_nationwide: includeFbNw})
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
                            setTimeout(function(){ statusEl.textContent = ''; }, 8000);
                        } else {
                            statusEl.style.color = '#e74c3c';
                            statusEl.textContent = d.error || 'Error starting search.';
                        }
                    }).catch(function(){ statusEl.style.color = '#e74c3c'; statusEl.textContent = 'Request failed.'; });
                }
                </script>
                <button onclick="runPartialSearch()"
                    style="padding:7px 18px; background:#8e44ad; color:white; border:none; border-radius:5px;
                           cursor:pointer; font-size:13px; font-weight:bold;">
                    <i class="fa-solid fa-play"></i> Run Partial Search
                </button>
                <span id="partial-status" style="font-size:12px; color:#888;"></span>
            </div>
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

    <table id="companyTable">
        <thead>
            <tr>
                <th><i class="fa-solid fa-building"></i> Company (Website)</th>
                <th><i class="fa-solid fa-location-dot"></i> Region</th>
                <th><i class="fa-solid fa-phone"></i> Phone</th>
                <th><i class="fa-solid fa-envelope"></i> Email</th>
                <th style="text-align:center"><i class="fa-brands fa-facebook-f" style="color:#1877f2"></i></th>
                <th style="text-align:center"><i class="fa-brands fa-linkedin-in" style="color:#0a66c2"></i></th>
                <th style="text-align:center"><span style="color:#c0392b;font-size:11px;font-weight:bold">NZSA</span></th>
                <th><i class="fa-solid fa-shield-halved"></i> PSPLA Status</th>
                <th><i class="fa-solid fa-id-card"></i> PSPLA Registered Name</th>
                <th><i class="fa-solid fa-hashtag"></i> License #</th>
                <th><i class="fa-regular fa-calendar"></i> Expiry</th>
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
                data-id="{{ loop.index }}">
                <td class="company-cell">
                    {% if c.website %}<a href="{{ c.website }}" target="_blank">{{ c.company_name or '-' }}</a>{% else %}{{ c.company_name or '-' }}{% endif %}
                </td>
                <td>{{ c.region or '-' }}</td>
                <td>{{ c.phone or '-' }}</td>
                <td>{% if c.email %}<a href="mailto:{{ c.email }}">{{ c.email }}</a>{% else %}-{% endif %}</td>
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
                        <span class="nzsa-tag" style="margin-left:0" title="NZSA Member{% if c.nzsa_accredited == 'true' %} — Accredited{% endif %}{% if c.nzsa_grade %}: {{ c.nzsa_grade }}{% endif %}">NZSA{% if c.nzsa_accredited == 'true' %}<i class="fa-solid fa-star" style="font-size:7px;margin-left:2px;vertical-align:middle;"></i>{% endif %}</span>
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
                <td style="white-space:nowrap; font-size:12px; color:#666;">{{ (c.date_added or '')[:10] or '-' }}</td>
                <td>
                    <button class="expand-btn" onclick="var r=document.getElementById('detail-{{ loop.index }}');if(r){var o=r.classList.toggle('open');this.textContent=o?'\u25b2 less':'\u25bc more';}">&#x25BC; more</button>
                </td>
            </tr>
            <tr class="detail-row" id="detail-{{ loop.index }}">
                <td colspan="14">
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

        function lookupFacebook(id) {
            var btn = document.getElementById('fb-btn-' + id);
            var result = document.getElementById('fb-result-' + id);
            var termInput = document.getElementById('fb-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...'; btn.style.background = '#555';
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
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Search';
                btn.disabled = false;
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
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
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
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Search';
                btn.disabled = false;
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
            })
            .catch(function(e) {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check';
                btn.disabled = false;
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

        function recheckServices(id, btn) {
            var website = btn.dataset.website;
            if (!website) { alert('No website URL available.'); return; }
            btn.disabled = true;
            btn.textContent = 'Checking...';
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
            })
            .catch(function() { btn.textContent = 'Re-check'; btn.disabled = false; });
        }

        function recheckCompaniesOffice(id) {
            var btn = document.getElementById('co-btn-' + id);
            var result = document.getElementById('co-recheck-result-' + id);
            var termInput = document.getElementById('co-term-' + id);
            var name = termInput ? termInput.value.trim() : '';
            if (!name) return;
            btn.disabled = true;
            btn.textContent = 'Checking...';
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
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
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
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed</em>';
                btn.textContent = 'Re-check'; btn.disabled = false;
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
            })
            .catch(function() {
                result.innerHTML = '<em style="color:#e74c3c">Request failed or timed out</em>';
                btn.textContent = 'Re-check all'; btn.disabled = false;
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
        function saveCorrection(id, companyName) {
            var status = document.getElementById('correction-status-' + id);
            var text = document.getElementById('correction-text-' + id).value.trim();
            if (!text) { status.style.color='#e74c3c'; status.textContent='Please enter a note first.'; return; }
            status.style.color = '#888';
            status.textContent = 'Saving...';
            fetch('/save-correction', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: id, company_name: companyName, correction: text})
            }).then(function(r){ return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    status.style.color = '#27ae60';
                    var msg = 'Saved!';
                    if (d.recheck_summary) {
                        msg += ' Re-checked: ' + d.recheck_summary;
                    }
                    if (d.lesson_rule) {
                        msg += ' | Lesson learned: ' + d.lesson_rule.substring(0, 80) + (d.lesson_rule.length > 80 ? '...' : '');
                    }
                    status.textContent = msg;
                    setTimeout(function(){ status.textContent = ''; }, 8000);
                } else {
                    status.style.color = '#e74c3c';
                    status.textContent = d.error || 'Error saving.';
                }
            }).catch(function(){ status.style.color='#e74c3c'; status.textContent='Request failed.'; });
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


@app.route("/start-directory-import", methods=["POST"])
def start_directory_import():
    try:
        _launch("run_directories.py")
        return redirect(url_for("index", message="Directory import started (NZSA + LinkedIn).", type="success"))
    except Exception as e:
        return redirect(url_for("index", message=f"Failed to start directory import: {e}", type="error"))


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
        "facebook_url", "linkedin_url",
        "nzsa_member", "nzsa_accredited", "nzsa_grade", "nzsa_member_name",
        "nzsa_contact_name", "nzsa_phone", "nzsa_email",
        "pspla_licensed", "pspla_name", "pspla_address", "pspla_license_number",
        "pspla_license_status", "pspla_license_expiry", "license_type",
        "match_method", "match_reason", "individual_license", "director_name",
        "companies_office_name", "companies_office_address",
        "companies_office_number", "nzbn",
        "last_checked", "notes"
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
        </select>
        <select id="statusFilter" onchange="renderTable()">
            <option value="">All Statuses</option>
            <option value="completed">Completed</option>
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
            </tr>
        </thead>
        <tbody id="tableBody"><tr><td colspan="7" style="text-align:center;color:#aaa;padding:30px;">Loading...</td></tr></tbody>
    </table>
</div>
<script>
var TYPE_LABELS   = {full:'Full','google-weekly':'Weekly',facebook:'Facebook','google-partial':'Partial',directories:'Directories'};
var STATUS_COLORS = {completed:'#27ae60',stopped:'#e67e22',error:'#e74c3c'};
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
            '<tr><td colspan="7" style="text-align:center;color:#aaa;padding:30px;">No records match.</td></tr>';
        return;
    }
    var html = '';
    rows.forEach(function(r) {
        var col = STATUS_COLORS[r.status] || '#888';
        var lbl = TYPE_LABELS[r.type] || r.type;
        var dur = r.duration_minutes ? r.duration_minutes + ' min' : '-';
        html += '<tr>'
            + '<td>' + fmt(r.started) + '</td>'
            + '<td>' + lbl + '</td>'
            + '<td style="color:#888;">' + (r.triggered_by||'-') + '</td>'
            + '<td class="right" style="color:#888;">' + dur + '</td>'
            + '<td class="right">' + (r.total_found||0).toLocaleString() + '</td>'
            + '<td class="right" style="font-weight:bold;">' + (r.total_new||0).toLocaleString() + '</td>'
            + '<td><span class="badge" style="background:' + col + '20;color:' + col + ';">' + r.status + '</span></td>'
            + '</tr>';
    });
    document.getElementById('tableBody').innerHTML = html;
}

fetch('/search-history-data')
    .then(function(r){ return r.json(); })
    .then(function(data) {
        _allRows = data;
        renderStats(data);
        renderTable();
    })
    .catch(function(){ document.getElementById('tableBody').innerHTML =
        '<tr><td colspan="7" style="text-align:center;color:#e74c3c;padding:30px;">Failed to load history.</td></tr>'; });
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
        "searcher", "run_weekly", "run_facebook", "run_partial", "run_directories",
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
    try:
        from searcher import find_facebook_url, scrape_facebook_page, write_audit
        fb_url = find_facebook_url(company_name)
        if fb_url:
            fb_data = scrape_facebook_page(fb_url, company_name=company_name)
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
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recheck-companies-office", methods=["POST"])
def recheck_companies_office_for_company():
    """Re-run Companies Office lookup for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    try:
        from searcher import check_companies_office, write_audit
        result = check_companies_office(company_name)
        patch = {
            "companies_office_name":    result.get("name"),
            "companies_office_address": result.get("address"),
            "companies_office_number":  result.get("company_number"),
            "nzbn":                     result.get("nzbn"),
            "co_status":                result.get("status"),
            "co_incorporated":          result.get("incorporated"),
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


@app.route("/recheck-google-profile", methods=["POST"])
def recheck_google_profile_for_company():
    """Re-run Google Business Profile lookup for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    company_region = request.json.get("region", "") or ""
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    try:
        from searcher import get_google_business_profile, write_audit
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
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            requests.patch(f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
                           headers=headers, json=patch)
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


@app.route("/recheck-nzsa", methods=["POST"])
def recheck_nzsa_for_company():
    """Re-check NZSA membership for a single company and save the result."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    try:
        from searcher import check_nzsa
        # Fetch website for domain matching
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        row = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=website",
            headers=headers, timeout=10
        ).json()
        website = row[0].get("website") if row else None

        result = check_nzsa(company_name, website=website)
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


@app.route("/recheck-services", methods=["POST"])
def recheck_services_for_company():
    """Re-scrape the company website and detect alarm/CCTV/monitoring services."""
    from flask import jsonify
    company_id = request.json.get("id")
    website_url = request.json.get("website", "")
    if not company_id or not website_url:
        return jsonify({"error": "Missing id or website"}), 400
    try:
        from searcher import scrape_website, gather_service_text, detect_services, write_audit
        page_text, _, _, _ = scrape_website(website_url)
        service_text = gather_service_text(website_url, page_text)
        services = detect_services(service_text)
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


@app.route("/find-linkedin", methods=["POST"])
def find_linkedin_for_company():
    """Look up a LinkedIn company page for a single company by ID and save it."""
    from flask import jsonify
    company_id = request.json.get("id")
    company_name = request.json.get("name", "")
    if not company_name:
        return jsonify({"error": "No company name provided"}), 400
    try:
        from searcher import find_linkedin_url, scrape_linkedin_page, write_audit
        li_url = find_linkedin_url(company_name)
        if li_url:
            li_data = scrape_linkedin_page(li_url, company_name=company_name)
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
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    try:
        from searcher import check_pspla, check_pspla_individual
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

        # If no active company license, check individual license using the stored director names
        individual_license = None
        if not licensed:
            director_str = request.json.get("directors", "")
            directors = [d.strip() for d in director_str.split(",") if d.strip()] if director_str else []
            for director in directors:
                ind = check_pspla_individual(director)
                if ind.get("found"):
                    individual_license = ind["name"]
                    licensed = True
                    break

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
        # Remove None values to avoid overwriting good data with null
        update = {k: v for k, v in update.items() if v is not None}
        # Always save pspla_licensed even if False/None
        update["pspla_licensed"] = licensed
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

        # 5. Auto-recheck PSPLA and generate lesson (only if it was a false PSPLA match)
        recheck_summary = None
        lesson_rule = None
        if correction_type == "false_pspla_match" and old_pspla_name:
            recheck = apply_correction_and_recheck(
                company_id, company_name, old_pspla_name, website_region=website_region
            )
            recheck_summary = recheck.get("summary")
            lesson_rule = recheck.get("lesson", {}).get("rule_to_apply")

        return jsonify({
            "ok": True,
            "type": correction_type,
            "summary": summary,
            "recheck_summary": recheck_summary,
            "lesson_rule": lesson_rule,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
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
        "run_directories.py": "directories",
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
