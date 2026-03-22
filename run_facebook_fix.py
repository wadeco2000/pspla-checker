#!/usr/bin/env python3
"""Fix Facebook-Only Entries — enrich companies that only have a Facebook link.

For each company where root_domain = 'facebook.com':
1. Re-scrape the Facebook page for email/website
2. If no website found on FB, Google the company name to find their real website
3. Use Haiku to verify the Google result matches the company
4. Update missing fields (website, email, phone, address)
5. Re-run PSPLA, Companies Office, NZSA, services detection
"""

import os
import sys
import json
import traceback
import requests
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

from searcher import (
    check_schema, clear_status, append_history, record_search_start,
    check_pause, check_and_launch_queue, install_graceful_shutdown, write_status,
    scrape_facebook_page, scrape_website, extract_company_info,
    get_google_business_profile, check_pspla, check_companies_office,
    check_nzsa, detect_services, gather_service_text,
    get_root_domain, write_audit, patch_company,
    google_search, SKIP_DOMAINS,
    reset_session_log, get_session_log, send_search_email,
    reset_token_usage,
    RUNNING_FLAG, PAUSE_FLAG,
    _upsert_search_state,
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")


def _llm_verify_website_match(company_name, region, website_title, website_url, website_snippet):
    """Use Haiku to verify a Google result matches the company."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = f"""I have a New Zealand company called "{company_name}" in the "{region}" region.
I found this website via Google search:
- URL: {website_url}
- Title: {website_title}
- Snippet: {website_snippet}

Is this the same company? Consider:
1. Does the name match or is it very similar?
2. Is the location consistent (same region/city in NZ)?
3. Is it the same industry/business?

Return JSON only: {{"match": true/false, "confidence": "high"/"medium"/"low", "reason": "brief explanation"}}"""

        response = client.messages.create(
            model="claude-haiku-4-20250414",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract JSON
        if "{" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
        return json.loads(text)
    except Exception as e:
        print(f"    [AI verify] Error: {e}")
        return {"match": False, "confidence": "low", "reason": f"AI error: {e}"}


def _find_real_website(company_name, region):
    """Google search for the company to find their real website."""
    query = f'"{company_name}" {region} New Zealand'
    print(f"    [Google] Searching: {query}")
    try:
        results = google_search(query)
    except Exception as e:
        print(f"    [Google] Error: {e}")
        return None

    if not results:
        print("    [Google] No results")
        return None

    # Directory/social domains to skip
    skip = set(SKIP_DOMAINS) | {
        "facebook.com", "fb.com", "instagram.com", "twitter.com",
        "linkedin.com", "youtube.com", "tiktok.com",
        "yellowpages.co.nz", "yell.co.nz", "finda.co.nz",
        "nzdirectory.co.nz", "localist.co.nz", "moneyhub.co.nz",
    }

    for r in results[:10]:
        url = r.get("link", "")
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        domain = get_root_domain(url)
        if domain and domain.lower() not in skip:
            return {"url": url, "title": title, "snippet": snippet, "domain": domain}

    print("    [Google] No non-directory websites found")
    return None


def run_facebook_fix(triggered_by="manual"):
    """Main function: find and enrich Facebook-only entries."""
    # Get all FB-only companies
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/Companies",
        params={
            "select": "id,company_name,website,root_domain,region,email,phone,address,"
                      "facebook_url,fb_email,fb_phone,fb_address,fb_description,"
                      "pspla_licensed,pspla_name",
            "root_domain": "eq.facebook.com",
            "order": "company_name",
        },
        headers=headers,
        timeout=30,
    )
    if not resp.ok:
        print(f"  [ERROR] Failed to fetch companies: {resp.status_code} {resp.text}")
        return 0, 0

    companies = resp.json()
    total = len(companies)
    print(f"\n  Found {total} Facebook-only companies to process\n")

    if total == 0:
        return 0, 0

    enriched = 0
    for idx, co in enumerate(companies, 1):
        check_pause()  # Respect pause/stop

        name = co["company_name"]
        region = co.get("region", "")
        co_id = co["id"]
        fb_url = co.get("facebook_url") or co.get("website", "")

        print(f"\n  [{idx}/{total}] {name} ({region})")
        write_status(
            phase=f"Fixing FB-only ({idx}/{total})",
            region=region, term=name,
            region_idx=idx, term_idx=0,
            total_regions=total, total_terms=1,
            total_found=idx, total_new=enriched,
        )

        updates = {}
        website_found = None

        # ── Step 1: Re-scrape Facebook page ─────────────────────────────
        if fb_url:
            print(f"    [FB] Scraping {fb_url}")
            try:
                fb_data = scrape_facebook_page(fb_url, company_name=name)
                if fb_data:
                    # Check for website link on FB page
                    fb_website = fb_data.get("website")
                    if fb_website and "facebook.com" not in fb_website.lower():
                        website_found = fb_website
                        print(f"    [FB] Found website on FB page: {website_found}")

                    # Fill in missing FB fields
                    if fb_data.get("email") and not co.get("fb_email"):
                        updates["fb_email"] = fb_data["email"]
                        if not co.get("email"):
                            updates["email"] = fb_data["email"]
                    if fb_data.get("phone") and not co.get("fb_phone"):
                        updates["fb_phone"] = fb_data["phone"]
                        if not co.get("phone"):
                            updates["phone"] = fb_data["phone"]
                    if fb_data.get("address") and not co.get("fb_address"):
                        updates["fb_address"] = fb_data["address"]
                        if not co.get("address"):
                            updates["address"] = fb_data["address"]
                    if fb_data.get("description") and not co.get("fb_description"):
                        updates["fb_description"] = fb_data["description"]
            except Exception as e:
                print(f"    [FB] Error: {e}")

        # ── Step 2: If no website from FB, Google it ────────────────────
        if not website_found:
            result = _find_real_website(name, region)
            if result:
                # Verify with AI
                print(f"    [AI] Verifying: {result['url']}")
                verify = _llm_verify_website_match(
                    name, region,
                    result["title"], result["url"], result["snippet"]
                )
                print(f"    [AI] Result: match={verify.get('match')}, "
                      f"confidence={verify.get('confidence')}, reason={verify.get('reason')}")

                if verify.get("match") and verify.get("confidence") in ("high", "medium"):
                    website_found = result["url"]
                else:
                    print(f"    [Skip] AI rejected match")

        # ── Step 3: If we found a website, scrape it ────────────────────
        if website_found:
            new_domain = get_root_domain(website_found)
            updates["website"] = website_found
            updates["root_domain"] = new_domain
            print(f"    [Website] Scraping {website_found}")

            try:
                page_text = scrape_website(website_found)
                if page_text:
                    info = extract_company_info(website_found, page_text, "")
                    if info:
                        if info.get("email") and not co.get("email") and "email" not in updates:
                            updates["email"] = info["email"]
                        if info.get("phone") and not co.get("phone") and "phone" not in updates:
                            updates["phone"] = info["phone"]
                        if info.get("address") and not co.get("address") and "address" not in updates:
                            updates["address"] = info["address"]
            except Exception as e:
                print(f"    [Website] Scrape error: {e}")

            # Google Business Profile
            try:
                gbp = get_google_business_profile(name, region)
                if gbp:
                    if gbp.get("phone") and not co.get("phone") and "phone" not in updates:
                        updates["phone"] = gbp["phone"]
                    if gbp.get("address") and not co.get("address") and "address" not in updates:
                        updates["address"] = gbp["address"]
                    if gbp.get("rating"):
                        updates["google_rating"] = gbp["rating"]
                    if gbp.get("reviews"):
                        updates["google_reviews"] = gbp["reviews"]
                    if gbp.get("phone"):
                        updates["google_phone"] = gbp["phone"]
                    if gbp.get("address"):
                        updates["google_address"] = gbp["address"]
            except Exception as e:
                print(f"    [Google] Profile error: {e}")

            # Services detection
            try:
                svc_text = gather_service_text(website_found) if website_found else ""
                if svc_text:
                    svc = detect_services(svc_text)
                    if svc:
                        updates["has_alarm_systems"] = svc.get("has_alarm_systems", False)
                        updates["has_cctv_cameras"] = svc.get("has_cctv_cameras", False)
                        updates["has_alarm_monitoring"] = svc.get("has_alarm_monitoring", False)
            except Exception as e:
                print(f"    [Services] Error: {e}")

            # PSPLA recheck
            try:
                pspla = check_pspla(name, website_region=region)
                if pspla and pspla.get("licensed") is not None:
                    updates["pspla_licensed"] = pspla["licensed"]
                    if pspla.get("pspla_name"):
                        updates["pspla_name"] = pspla["pspla_name"]
                    if pspla.get("license_number"):
                        updates["pspla_license_number"] = pspla["license_number"]
                    if pspla.get("license_status"):
                        updates["pspla_license_status"] = pspla["license_status"]
                    if pspla.get("license_expiry"):
                        updates["pspla_license_expiry"] = pspla["license_expiry"]
                    if pspla.get("license_classes"):
                        updates["pspla_license_classes"] = pspla["license_classes"]
                    if pspla.get("match_method"):
                        updates["match_method"] = pspla["match_method"]
            except Exception as e:
                print(f"    [PSPLA] Error: {e}")

            # Companies Office
            try:
                co_result = check_companies_office(name)
                if co_result:
                    if co_result.get("name"):
                        updates["companies_office_name"] = co_result["name"]
                    if co_result.get("number"):
                        updates["companies_office_number"] = co_result["number"]
                    if co_result.get("address"):
                        updates["companies_office_address"] = co_result["address"]
                    if co_result.get("nzbn"):
                        updates["nzbn"] = co_result["nzbn"]
                    if co_result.get("status"):
                        updates["co_status"] = co_result["status"]
                    if co_result.get("directors"):
                        updates["director_name"] = co_result["directors"]
            except Exception as e:
                print(f"    [CO] Error: {e}")

            # NZSA
            try:
                nzsa = check_nzsa(name, website=website_found)
                if nzsa and nzsa.get("member"):
                    updates["nzsa_member"] = True
                    if nzsa.get("member_name"):
                        updates["nzsa_member_name"] = nzsa["member_name"]
                    if nzsa.get("accredited"):
                        updates["nzsa_accredited"] = nzsa["accredited"]
            except Exception as e:
                print(f"    [NZSA] Error: {e}")

        # ── Step 4: Save updates ────────────────────────────────────────
        if updates:
            updates["last_checked"] = datetime.now(timezone.utc).isoformat()
            print(f"    [Save] Updating {len(updates)} fields: {list(updates.keys())}")
            try:
                patch_company(co_id, updates)
                write_audit("facebook-fix", co_id, name,
                            changes=json.dumps({k: str(v)[:100] for k, v in updates.items()}),
                            triggered_by=triggered_by)
                enriched += 1
            except Exception as e:
                print(f"    [Save] Error: {e}")
        else:
            print(f"    [Skip] No new data found")

    return total, enriched


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"

    # Parse --triggered-by-user
    _tbu = None
    for _i, _a in enumerate(sys.argv):
        if _a == "--triggered-by-user" and _i + 1 < len(sys.argv):
            _tbu = sys.argv[_i + 1]

    if not check_schema():
        raise SystemExit(1)

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    reset_session_log()
    reset_token_usage()
    open(RUNNING_FLAG, "w").close()

    started_iso = datetime.now(timezone.utc).isoformat()
    record_search_start("facebook-fix", started_iso, triggered_by, triggered_by_user=_tbu)
    install_graceful_shutdown("facebook-fix", started_iso, triggered_by, _tbu)

    total_processed = 0
    total_enriched = 0

    try:
        total_processed, total_enriched = run_facebook_fix(triggered_by=triggered_by)

        append_history("facebook-fix", started_iso, total_processed, total_enriched,
                       "completed", triggered_by, triggered_by_user=_tbu)
        send_search_email("facebook-fix", started_iso, total_processed, total_enriched,
                          triggered_by, get_session_log())

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n  [FATAL ERROR] {e}\n{tb}")
        append_history("facebook-fix", started_iso, 0, 0,
                       f"error: {type(e).__name__}: {e}", triggered_by,
                       notes=tb[:1500], triggered_by_user=_tbu)
        raise

    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)
        _upsert_search_state({"is_running": False, "paused": False, "stop_requested": False})
        check_and_launch_queue()

    print("\n" + "=" * 60)
    print(f"  Facebook-only fix complete!")
    print(f"  Companies processed: {total_processed}")
    print(f"  Companies enriched:  {total_enriched}")
    print("=" * 60)
