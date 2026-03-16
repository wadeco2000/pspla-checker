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

import traceback as _tb

from searcher import (
    google_search, extract_company_info, scrape_website,
    find_email_via_google, find_facebook_url, find_linkedin_url,
    get_root_domain, get_domain_record,
    company_exists, process_and_save_company, check_schema,
    write_status, clear_status, append_history, record_search_start, check_pause,
    SKIP_DOMAINS, SERPAPI_EXHAUSTED, run_facebook_search,
    is_directory_listing_url,
    reset_session_log, get_session_log, send_search_email,
    load_partial_progress, save_partial_progress, clear_partial_progress,
    reset_token_usage, reset_serp_query_count,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG = os.path.join(BASE_DIR, "pause.flag")
PARTIAL_CONFIG_FILE = os.path.join(BASE_DIR, "partial_config.json")


def run_partial(triggered_by="manual", fresh=False, triggered_by_user=None):
    if not os.path.exists(PARTIAL_CONFIG_FILE):
        print("No partial_config.json found. Aborting.")
        return

    with open(PARTIAL_CONFIG_FILE) as f:
        config = json.load(f)

    regions = config.get("regions", [])
    google_terms = config.get("google_terms", [])
    include_facebook = config.get("include_facebook", False)
    include_nationwide = config.get("include_nationwide", False)
    fb_time_filter = config.get("fb_time_filter")

    if not regions:
        print("No regions specified. Aborting.")
        return
    if not google_terms and not include_facebook and not include_nationwide:
        print("No terms and no Facebook options selected. Aborting.")
        return

    started_iso = datetime.now(timezone.utc).isoformat()
    reset_session_log()
    reset_token_usage()
    reset_serp_query_count()
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
    _hist_config = {"regions": regions, "terms": google_terms,
                     "include_facebook": include_facebook, "include_nationwide": include_nationwide}
    record_search_start("google-partial", started_iso, triggered_by, config=_hist_config, triggered_by_user=triggered_by_user)
    total_found = 0
    total_new = 0
    found_urls = set()

    if fresh:
        clear_partial_progress()
    partial_progress = load_partial_progress()
    completed_regions = partial_progress.get("completed_regions", [])
    if partial_progress.get("google_done"):
        print("  [Skipping] Google search already completed — resuming from Facebook/completion")
    elif completed_regions:
        print(f"  Resuming Google search — {len(completed_regions)} regions already done: {', '.join(completed_regions)}")

    try:
        if not partial_progress.get("google_done"):
            for region in regions:
                if region in completed_regions:
                    print(f"  [Skipping] {region} — already done")
                    continue
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
                                       "stopped", triggered_by, config=_hist_config, triggered_by_user=triggered_by_user)
                        send_search_email("google-partial", started_iso, total_found, total_new, triggered_by, get_session_log())
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
                                                    f"partial {term} {region}", region):
                            total_new += 1

                completed_regions.append(region)
                partial_progress["completed_regions"] = completed_regions
                save_partial_progress(partial_progress)
                print(f"  [Progress saved] {region} done ({len(completed_regions)}/{len(regions)} regions)")

            partial_progress["google_done"] = True
            save_partial_progress(partial_progress)

        if include_facebook or include_nationwide:
            fb_found, fb_new = run_facebook_search(
                found_urls,
                regions=regions if include_facebook else [],
                include_nationwide=include_nationwide,
                track_progress=False,
                time_filter=fb_time_filter,
            )
            total_found += fb_found
            total_new += fb_new
            partial_progress["fb_done"] = True
            save_partial_progress(partial_progress)

        append_history("google-partial", started_iso, total_found, total_new,
                       "completed", triggered_by, config=_hist_config, triggered_by_user=triggered_by_user)
        send_search_email("google-partial", started_iso, total_found, total_new, triggered_by, get_session_log())
        clear_partial_progress()

    except Exception as e:
        tb = _tb.format_exc()
        print(f"\n  [CRASH] Unhandled exception in Partial search: {e}")
        print(tb)
        append_history("google-partial", started_iso, total_found, total_new,
                       f"error: {type(e).__name__}: {e}", triggered_by, notes=tb[:1500], config=_hist_config, triggered_by_user=triggered_by_user)
        raise

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
    fresh = "--fresh" in sys.argv
    _tbu = None
    for _i, _a in enumerate(sys.argv):
        if _a == "--triggered-by-user" and _i + 1 < len(sys.argv):
            _tbu = sys.argv[_i + 1]
    run_partial(triggered_by, fresh=fresh, triggered_by_user=_tbu)
