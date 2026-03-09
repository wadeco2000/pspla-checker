"""
run_directories.py — import companies from NZSA and LinkedIn directories.
Adds any members/companies not already in the database.
Called by the dashboard "Directory Import" button, the APScheduler, or directly:
    python run_directories.py
    python run_directories.py --scheduled
    python run_directories.py --test          # limit=5 per import for testing
    python run_directories.py --nzsa-only
    python run_directories.py --linkedin-only
"""

import os
import sys
from datetime import datetime, timezone

from searcher import (
    run_nzsa_import, run_linkedin_import, check_schema,
    clear_status, append_history,
    reset_session_log, get_session_log, send_search_email,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    test_mode = "--test" in sys.argv
    nzsa_only = "--nzsa-only" in sys.argv
    linkedin_only = "--linkedin-only" in sys.argv
    limit = 5 if test_mode else None

    started_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 60)
    print("  PSPLA Directory Import (NZSA + LinkedIn)")
    if test_mode:
        print("  *** TEST MODE — limit 5 per import ***")
    print("=" * 60)

    print("  Checking database schema...")
    if not check_schema():
        print("  Aborting — fix missing columns first.")
        raise SystemExit(1)

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    reset_session_log()
    open(RUNNING_FLAG, "w").close()

    found_urls = set()
    total_found = 0
    total_new = 0

    try:
        if not linkedin_only:
            nzsa_found, nzsa_new = run_nzsa_import(found_urls, limit=limit)
            total_found += nzsa_found
            total_new += nzsa_new

        if not nzsa_only:
            li_found, li_new = run_linkedin_import(found_urls, limit=limit)
            total_found += li_found
            total_new += li_new

        append_history("directories", started_iso, total_found, total_new, "completed", triggered_by)
        send_search_email("directories", started_iso, total_found, total_new, triggered_by, get_session_log())

    except Exception as e:
        append_history("directories", started_iso, total_found, total_new, f"error: {e}", triggered_by)
        raise

    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Directory import complete!")
    print(f"  Companies found:     {total_found}")
    print(f"  New companies added: {total_new}")
    print("=" * 60)
