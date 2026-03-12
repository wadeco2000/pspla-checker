"""
run_weekly.py — light weekly Google scan.
Searches only the last 7 days of results (tbs=qdr:w) using 5 broad terms.
Adds new companies found since the last scan without touching existing records.
Called by the dashboard "Weekly Scan" button or the APScheduler.
    python run_weekly.py
    python run_weekly.py --scheduled
"""

import os
import sys
import time
from datetime import datetime, timezone

from searcher import (
    google_search, extract_company_info, scrape_website,
    find_email_via_google, find_facebook_url, find_linkedin_url,
    get_root_domain, get_domain_record,
    company_exists, process_and_save_company, check_schema,
    write_status, clear_status, append_history, check_pause,
    RUNNING_FLAG, PAUSE_FLAG, SKIP_DOMAINS, NZ_REGIONS, SERPAPI_EXHAUSTED,
    is_directory_listing_url,
    reset_session_log, reset_token_usage, get_session_log, send_search_email,
    record_search_start, is_schedule_enabled,
)

WEEKLY_TERMS = [
    "security camera installation",
    "CCTV installation",
    "security camera installer",
    "security alarm installation",
    "CCTV installer",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")


def run_weekly(triggered_by="manual"):
    started_iso = datetime.now(timezone.utc).isoformat()
    reset_session_log()
    print("=" * 60)
    print("  PSPLA Weekly Light Scan (last 7 days)")
    print("=" * 60)

    print("  Checking database schema...")
    if not check_schema():
        print("  Aborting — fix missing columns first.")
        return

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    open(RUNNING_FLAG, "w").close()
    reset_token_usage()
    record_search_start("google-weekly", started_iso, triggered_by)
    total_found = 0
    total_new = 0
    found_urls = set()

    try:
        for region in NZ_REGIONS:
            print(f"\nSearching: {region}")

            for term in WEEKLY_TERMS:
                check_pause()
                region_idx = NZ_REGIONS.index(region) + 1
                term_idx = WEEKLY_TERMS.index(term) + 1
                write_status("google-weekly", region, term, region_idx, term_idx,
                             len(NZ_REGIONS), len(WEEKLY_TERMS), total_found, total_new)

                query = f"{term} {region} New Zealand"
                print(f"  Query: {query}")
                results = google_search(query, num_results=30, time_filter="qdr:w")
                time.sleep(1)

                if results is SERPAPI_EXHAUSTED:
                    print("\n  [STOPPED] SerpAPI exhausted.")
                    append_history("google-weekly", started_iso, total_found, total_new,
                                   "stopped", triggered_by)
                    send_search_email("google-weekly", started_iso, total_found, total_new, triggered_by, get_session_log())
                    return

                if not results:
                    continue

                for result in results:
                    check_pause()
                    url = result["link"]

                    if url in found_urls:
                        continue
                    found_urls.add(url)

                    if any(domain in url for domain in SKIP_DOMAINS):
                        continue
                    if is_directory_listing_url(url):
                        print(f"  [Skipped] Directory/listing page: {url}")
                        continue
                    if company_exists(url):
                        continue

                    root_domain = get_root_domain(url)
                    if get_domain_record(root_domain):
                        continue

                    print(f"  [Found] {url}")
                    total_found += 1

                    page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(url)
                    time.sleep(1)

                    info = extract_company_info(url, page_text, result["snippet"])
                    if not info or not info.get("company_name"):
                        print("  [Skipped] Could not extract company name")
                        continue

                    info["_page_text"] = page_text

                    if not info.get("email") and scraped_email:
                        info["email"] = scraped_email
                    if not info.get("email"):
                        found_email = find_email_via_google(root_domain)
                        if found_email:
                            info["email"] = found_email

                    print(f"  [Company] {info['company_name']}")
                    fb_url = find_facebook_url(info["company_name"], page_text)
                    if fb_url:
                        info["facebook_url"] = fb_url
                    li_url = scraped_linkedin or find_linkedin_url(info["company_name"], page_text)
                    if li_url:
                        info["linkedin_url"] = li_url
                    if process_and_save_company(info, url, root_domain,
                                                f"weekly {term} {region}", region):
                        total_new += 1

        append_history("google-weekly", started_iso, total_found, total_new,
                       "completed", triggered_by)
        send_search_email("google-weekly", started_iso, total_found, total_new, triggered_by, get_session_log())

    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Weekly scan complete!")
    print(f"  New URLs found:      {total_found}")
    print(f"  New companies added: {total_new}")
    print("=" * 60)


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    if triggered_by == "scheduled" and not is_schedule_enabled():
        print("  Scheduled searches are disabled — exiting.")
        raise SystemExit(0)
    run_weekly(triggered_by)
