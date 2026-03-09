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

from searcher import (
    run_facebook_search, check_schema, clear_status,
    append_history, RUNNING_FLAG, PAUSE_FLAG,
    reset_session_log, get_session_log, send_search_email,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    started_iso = datetime.now(timezone.utc).isoformat()

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
    open(RUNNING_FLAG, "w").close()
    try:
        fb_found, fb_new = run_facebook_search(set())
        append_history("facebook", started_iso, fb_found, fb_new, "completed", triggered_by)
        send_search_email("facebook", started_iso, fb_found, fb_new, triggered_by, get_session_log())
    except Exception as e:
        append_history("facebook", started_iso, 0, 0, f"error: {e}", triggered_by)
        raise
    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Facebook search complete!")
    print(f"  FB pages found:      {fb_found}")
    print(f"  New companies added: {fb_new}")
    print("=" * 60)
