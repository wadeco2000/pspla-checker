"""
run_partial.py — targeted search for selected regions and terms.
Reads partial_config.json written by the dashboard Partial Search panel.
    python run_partial.py
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

from searcher import (
    google_search, extract_company_info, scrape_website,
    find_email_via_google, find_facebook_url, get_root_domain, get_domain_record,
    company_exists, process_and_save_company, check_schema,
    write_status, clear_status, append_history, check_pause,
    SKIP_DOMAINS, SERPAPI_EXHAUSTED, run_facebook_search,
    is_directory_listing_url,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")
PARTIAL_CONFIG_FILE = os.path.join(BASE_DIR, "partial_config.json")


def run_partial(triggered_by="manual"):
    if not os.path.exists(PARTIAL_CONFIG_FILE):
        print("No partial_config.json found. Aborting.")
        return

    with open(PARTIAL_CONFIG_FILE) as f:
        config = json.load(f)

    regions = config.get("regions", [])
    google_terms = config.get("google_terms", [])
    include_facebook = config.get("include_facebook", False)
    include_nationwide = config.get("include_nationwide", False)

    if not regions:
        print("No regions specified. Aborting.")
        return
    if not google_terms and not include_facebook and not include_nationwide:
        print("No terms and no Facebook options selected. Aborting.")
        return

    started_iso = datetime.now(timezone.utc).isoformat()
    print("=" * 60)
    print(f"  PSPLA Partial Search")
    print(f"  Regions: {len(regions)}  |  Terms: {len(google_terms)}  |  Facebook: {include_facebook}  |  NZ-wide FB: {include_nationwide}")
    print("=" * 60)

    if not check_schema():
        print("  Aborting — fix missing columns first.")
        return

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    open(RUNNING_FLAG, "w").close()
    total_found = 0
    total_new = 0
    found_urls = set()

    try:
        for region in regions:
            print(f"\nSearching: {region}")

            for term in google_terms:
                check_pause()
                region_idx = regions.index(region) + 1
                term_idx = google_terms.index(term) + 1
                write_status("google-partial", region, term, region_idx, term_idx,
                             len(regions), len(google_terms), total_found, total_new)

                query = f"{term} {region} New Zealand"
                print(f"  Query: {query}")
                results = google_search(query, num_results=100)
                time.sleep(1)

                if results is SERPAPI_EXHAUSTED:
                    print("\n  [STOPPED] SerpAPI exhausted.")
                    append_history("google-partial", started_iso, total_found, total_new,
                                   "stopped", triggered_by)
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

                    page_text, scraped_email, scraped_facebook = scrape_website(url)
                    time.sleep(1)

                    info = extract_company_info(url, page_text, result["snippet"])
                    if not info or not info.get("company_name"):
                        print("  [Skipped] Could not extract company name")
                        continue

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
                    if process_and_save_company(info, url, root_domain,
                                                f"partial {term} {region}", region):
                        total_new += 1

        if include_facebook or include_nationwide:
            fb_found, fb_new = run_facebook_search(
                found_urls,
                regions=regions if include_facebook else [],
                include_nationwide=include_nationwide,
            )
            total_found += fb_found
            total_new += fb_new

        append_history("google-partial", started_iso, total_found, total_new,
                       "completed", triggered_by)

    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Partial search complete!")
    print(f"  URLs found:          {total_found}")
    print(f"  New companies added: {total_new}")
    print("=" * 60)


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    run_partial(triggered_by)
