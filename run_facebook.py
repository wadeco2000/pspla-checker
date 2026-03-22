"""
run_facebook.py — standalone entry point to run only the Facebook search pass.
Adds Facebook-sourced companies on top of whatever is already in the database.
Called by the dashboard "Facebook Search" button, the APScheduler, or directly:
    python run_facebook.py
    python run_facebook.py --scheduled
"""

import os
import sys
from datetime import datetime, timezone

import traceback as _tb

from searcher import (
    run_facebook_search, check_schema, clear_status,
    check_and_launch_queue, install_graceful_shutdown,
    append_history, record_search_start, RUNNING_FLAG, PAUSE_FLAG,
    reset_session_log, reset_token_usage, get_session_log, send_search_email,
    clear_fb_progress, is_schedule_enabled,
    reset_serp_query_count,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    fresh = "--fresh" in sys.argv
    _tbu = None
    for _i, _a in enumerate(sys.argv):
        if _a == "--triggered-by-user" and _i + 1 < len(sys.argv):
            _tbu = sys.argv[_i + 1]

    # Scheduled runs default to last 90 days; manual runs search all time
    fb_time_filter = "qdr:m3" if triggered_by == "scheduled" else None

    if triggered_by == "scheduled" and not is_schedule_enabled():
        print("  Scheduled searches are disabled — exiting.")
        raise SystemExit(0)

    started_iso = datetime.now(timezone.utc).isoformat()
    _config = {"fresh": fresh, "time_filter": fb_time_filter}

    print("=" * 60)
    print("  PSPLA Facebook Search Pass")
    print("=" * 60)

    print("  Checking database schema...")
    if not check_schema():
        print("  Aborting — fix missing columns first.")
        raise SystemExit(1)

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    reset_session_log()
    reset_token_usage()
    reset_serp_query_count()
    open(RUNNING_FLAG, "w").close()
    record_search_start("facebook", started_iso, triggered_by, config=_config, triggered_by_user=_tbu)
    install_graceful_shutdown("facebook", started_iso, triggered_by, _tbu)
    try:
        fb_found, fb_new = run_facebook_search(set(), fresh=fresh, time_filter=fb_time_filter)
        append_history("facebook", started_iso, fb_found, fb_new, "completed", triggered_by, config=_config, triggered_by_user=_tbu)
        send_search_email("facebook", started_iso, fb_found, fb_new, triggered_by, get_session_log())
        clear_fb_progress()
    except Exception as e:
        tb = _tb.format_exc()
        print(f"\n  [CRASH] Unhandled exception in Facebook search: {e}")
        print(tb)
        append_history("facebook", started_iso, 0, 0, f"error: {type(e).__name__}: {e}", triggered_by,
                       notes=tb[:1500], config=_config, triggered_by_user=_tbu)
        raise
    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)
        check_and_launch_queue()

    print("\n" + "=" * 60)
    print(f"  Facebook search complete!")
    print(f"  FB pages found:      {fb_found}")
    print(f"  New companies added: {fb_new}")
    print("=" * 60)
