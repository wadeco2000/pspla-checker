"""
run_recheck.py — bulk recheck of existing database companies.
Reads recheck_config.json written by the dashboard Bulk Recheck panel.
Checks selected in any combination: facebook, google, linkedin, nzsa, companies_office, pspla
Can run on all companies or a selected subset (by ID list).

    python run_recheck.py
    python run_recheck.py --scheduled
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timezone

import traceback as _tb

from searcher import (
    check_pspla, check_pspla_individual, check_companies_office, check_nzsa,
    find_facebook_url, scrape_facebook_page, find_linkedin_url, scrape_linkedin_page,
    get_google_business_profile, detect_services,
    write_audit, write_status, clear_status, append_history, record_search_start, check_pause,
    reset_session_log, get_session_log, send_search_email,
    patch_company, enrich_existing_record,
    SUPABASE_URL, SUPABASE_KEY,
    RUNNING_FLAG, PAUSE_FLAG,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNING_FLAG = os.path.join(BASE_DIR, "running.flag")
PAUSE_FLAG   = os.path.join(BASE_DIR, "pause.flag")
RECHECK_CONFIG_FILE = os.path.join(BASE_DIR, "recheck_config.json")


def fetch_companies(company_ids="all"):
    """Fetch companies from Supabase. company_ids='all' or a list of int IDs."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if company_ids == "all":
        url = f"{SUPABASE_URL}/rest/v1/Companies?select=*&order=company_name.asc"
    else:
        id_list = ",".join(str(i) for i in company_ids)
        url = f"{SUPABASE_URL}/rest/v1/Companies?id=in.({id_list})&select=*"
    try:
        r = requests.get(url, headers=headers, timeout=30)
        return r.json() if r.ok else []
    except Exception as e:
        print(f"  [Fetch error] {e}")
        return []


def _patch(company_id, updates, company_name, audit_changes, triggered_by="bulk-recheck"):
    """Patch a company record and write an audit entry."""
    clean = {k: v for k, v in updates.items() if v is not None}
    if not clean:
        return
    patch_company(company_id, clean)
    write_audit("updated", str(company_id), company_name,
                changes=audit_changes, triggered_by=triggered_by)


def recheck_facebook(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    existing_fb  = company.get("facebook_url", "")
    website      = company.get("website_url") or company.get("website") or ""

    fb_url = existing_fb
    if not fb_url:
        print(f"  [Facebook] Finding FB URL for: {company_name}")
        fb_url = find_facebook_url(company_name, "")
        time.sleep(1)

    if not fb_url:
        print(f"  [Facebook] No FB URL found for: {company_name}")
        return

    print(f"  [Facebook] Scraping: {fb_url}")
    fb_data = scrape_facebook_page(fb_url, company_name=company_name)
    time.sleep(1)

    # FB service detection from description/category
    fb_text = " ".join(filter(None, [
        fb_data.get("description"), fb_data.get("category"),
        company.get("fb_description", ""), company.get("fb_category", ""),
    ]))
    fb_services = detect_services(fb_text) if fb_text.strip() else {}

    updates = {
        "facebook_url":       fb_url,
        "fb_followers":       fb_data.get("followers"),
        "fb_phone":           fb_data.get("phone"),
        "fb_email":           fb_data.get("email"),
        "fb_address":         fb_data.get("address"),
        "fb_description":     fb_data.get("description"),
        "fb_category":        fb_data.get("category"),
        "fb_rating":          fb_data.get("rating"),
        "fb_alarm_systems":   fb_services.get("has_alarm_systems"),
        "fb_cctv_cameras":    fb_services.get("has_cctv_cameras"),
        "fb_alarm_monitoring":fb_services.get("has_alarm_monitoring"),
        "last_checked":       datetime.now(timezone.utc).isoformat(),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if updates:
        patch_company(company_id, updates)
        changes_str = f"FB recheck: followers={fb_data.get('followers')} phone={fb_data.get('phone')} email={fb_data.get('email')}"
        if not existing_fb and fb_url:
            changes_str = f"FB found: {fb_url}. " + changes_str
        write_audit("updated", str(company_id), company_name,
                    changes=changes_str, triggered_by=triggered_by)
        print(f"  [Facebook] Updated {company_name}: followers={fb_data.get('followers')}")
    else:
        print(f"  [Facebook] No new data for {company_name}")


def recheck_google(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    region       = company.get("region", "") or ""
    print(f"  [Google] Looking up: {company_name}")
    result = get_google_business_profile(company_name, region)
    time.sleep(1)
    updates = {
        "google_rating":  result.get("rating"),
        "google_reviews": result.get("reviews"),
        "google_phone":   result.get("phone"),
        "google_address": result.get("address"),
        "last_checked":   datetime.now(timezone.utc).isoformat(),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if updates:
        patch_company(company_id, updates)
        write_audit("updated", str(company_id), company_name,
                    changes=f"Google recheck: rating={result.get('rating')} phone={result.get('phone')}",
                    triggered_by=triggered_by)
        print(f"  [Google] Updated {company_name}: rating={result.get('rating')}")
    else:
        print(f"  [Google] No data for {company_name}")


def recheck_linkedin(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    existing_li  = company.get("linkedin_url", "")

    li_url = existing_li
    if not li_url:
        print(f"  [LinkedIn] Finding URL for: {company_name}")
        li_url = find_linkedin_url(company_name, "")
        time.sleep(1)

    if not li_url:
        print(f"  [LinkedIn] No URL found for: {company_name}")
        return

    print(f"  [LinkedIn] Scraping: {li_url}")
    li_data = scrape_linkedin_page(li_url, company_name=company_name)
    time.sleep(1)

    updates = {"linkedin_url": li_url, "last_checked": datetime.now(timezone.utc).isoformat()}
    for field in ("followers", "description", "industry", "location", "website", "size"):
        if li_data.get(field):
            updates[f"linkedin_{field}"] = li_data[field]
    patch_company(company_id, updates)
    changes_str = f"LinkedIn recheck: url={li_url} followers={li_data.get('followers')} industry={li_data.get('industry')}"
    if not existing_li:
        changes_str = f"LinkedIn found: {li_url}. " + changes_str
    write_audit("updated", str(company_id), company_name,
                changes=changes_str, triggered_by=triggered_by)
    print(f"  [LinkedIn] Updated {company_name}: {li_url}")


def recheck_nzsa(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    website      = company.get("website_url") or company.get("website") or ""
    print(f"  [NZSA] Checking: {company_name}")
    result = check_nzsa(company_name, website=website)
    time.sleep(1)
    updates = {
        "nzsa_member":       "true" if result["member"] else "false",
        "nzsa_member_name":  result.get("member_name"),
        "nzsa_accredited":   "true" if result.get("accredited") else "false",
        "nzsa_grade":        result.get("grade"),
        "nzsa_contact_name": result.get("contact_name"),
        "nzsa_phone":        result.get("phone"),
        "nzsa_email":        result.get("email"),
        "nzsa_overview":     result.get("overview"),
        "last_checked":      datetime.now(timezone.utc).isoformat(),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    patch_company(company_id, updates)
    write_audit("updated", str(company_id), company_name,
                changes=f"NZSA recheck: member={result['member']} name={result.get('member_name')}",
                triggered_by=triggered_by)
    print(f"  [NZSA] {company_name}: member={result['member']}")


def recheck_companies_office(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    print(f"  [CO] Checking: {company_name}")
    result = check_companies_office(company_name)
    time.sleep(1)
    updates = {
        "companies_office_name":    result.get("name"),
        "companies_office_address": result.get("address"),
        "companies_office_number":  result.get("company_number"),
        "nzbn":                     result.get("nzbn"),
        "co_status":                result.get("status"),
        "co_incorporated":          result.get("incorporated"),
        "last_checked":             datetime.now(timezone.utc).isoformat(),
    }
    if result.get("directors"):
        updates["director_name"] = ", ".join(result["directors"])
    updates = {k: v for k, v in updates.items() if v is not None}
    if updates:
        patch_company(company_id, updates)
        write_audit("updated", str(company_id), company_name,
                    changes=f"CO recheck: name={result.get('name')} status={result.get('status')} nzbn={result.get('nzbn')}",
                    triggered_by=triggered_by)
        print(f"  [CO] Updated {company_name}: {result.get('name')}")
    else:
        print(f"  [CO] No data for {company_name}")


def recheck_pspla(company, triggered_by="bulk-recheck"):
    company_id   = company["id"]
    company_name = company.get("company_name", "")
    region       = company.get("region", "") or ""
    directors    = [d.strip() for d in (company.get("director_name") or "").split(",") if d.strip()]
    co_result    = {"name": company.get("companies_office_name"), "address": company.get("companies_office_address")} if company.get("companies_office_name") else None
    extra_context = {
        "facebook_snippet": "",
        "linkedin_url": company.get("linkedin_url") or "",
        "nzsa_data": {"member_name": company.get("nzsa_member_name"), "grade": company.get("nzsa_grade")} if company.get("nzsa_member_name") else None,
    }
    print(f"  [PSPLA] Checking: {company_name}")
    result = check_pspla(company_name, website_region=region, co_result=co_result,
                         directors=directors, extra_context=extra_context)
    time.sleep(1)

    # Also try CO name if no match
    co_name = company.get("companies_office_name")
    if not result.get("licensed") and co_name and co_name != company_name:
        co_check = check_pspla(co_name, website_region=region, co_result=co_result,
                               directors=directors, extra_context=extra_context)
        if co_check.get("matched_name") and (co_check.get("licensed") or not result.get("matched_name")):
            result = co_check

    licensed = result.get("licensed")
    pspla_name = result.get("matched_name")

    # Individual licence check if no company licence
    individual_license_found = False
    if not licensed:
        for director in directors:
            ind = check_pspla_individual(director)
            if ind:
                individual_license_found = True
                print(f"  [PSPLA] Individual licence found for director: {director}")
                break

    updates = {
        "pspla_licensed":        licensed,
        "pspla_name":            pspla_name,
        "pspla_license_number":  result.get("license_number"),
        "pspla_license_status":  result.get("license_status"),
        "pspla_license_expiry":  result.get("expiry"),
        "pspla_license_classes": result.get("license_classes"),
        "pspla_license_start":   result.get("license_start"),
        "pspla_permit_type":     result.get("permit_type"),
        "match_method":          result.get("match_method"),
        "match_reason":          result.get("match_reason"),
        "individual_license":    individual_license_found,
        "last_checked":          datetime.now(timezone.utc).isoformat(),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    patch_company(company_id, updates)
    write_audit("updated", str(company_id), company_name,
                changes=f"PSPLA recheck: licensed={licensed} name={pspla_name} status={result.get('license_status')}",
                triggered_by=triggered_by)
    print(f"  [PSPLA] {company_name}: licensed={licensed} name={pspla_name}")


CHECK_FUNCTIONS = {
    "facebook":         recheck_facebook,
    "google":           recheck_google,
    "linkedin":         recheck_linkedin,
    "nzsa":             recheck_nzsa,
    "companies_office": recheck_companies_office,
    "pspla":            recheck_pspla,
}

CHECK_LABELS = {
    "facebook": "Facebook", "google": "Google", "linkedin": "LinkedIn",
    "nzsa": "NZSA", "companies_office": "Companies Office", "pspla": "PSPLA",
}


def run_recheck(triggered_by="manual"):
    if not os.path.exists(RECHECK_CONFIG_FILE):
        print("No recheck_config.json found. Aborting.")
        return

    with open(RECHECK_CONFIG_FILE) as f:
        config = json.load(f)

    checks      = config.get("checks", [])
    company_ids = config.get("company_ids", "all")

    if not checks:
        print("No checks selected. Aborting.")
        return

    started_iso = datetime.now(timezone.utc).isoformat()
    reset_session_log()

    check_label = " + ".join(CHECK_LABELS.get(c, c) for c in checks)
    scope_label = "all companies" if company_ids == "all" else f"{len(company_ids)} selected"

    print("=" * 60)
    print(f"  PSPLA Bulk Recheck")
    print(f"  Checks: {check_label}")
    print(f"  Scope:  {scope_label}")
    print("=" * 60)

    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    open(RUNNING_FLAG, "w").close()
    record_search_start("bulk-recheck", started_iso, triggered_by)

    total_processed = 0
    total_updated = 0

    try:
        print("\n  Fetching companies from database...")
        companies = fetch_companies(company_ids)
        total = len(companies)
        print(f"  {total} companies to process\n")

        for idx, company in enumerate(companies, 1):
            check_pause()
            company_name = company.get("company_name", f"ID {company.get('id')}")
            print(f"\n[{idx}/{total}] {company_name}")

            # Write status for dashboard progress bar
            write_status("recheck", f"{idx}/{total}", company_name,
                         idx, 1, total, 1, total_processed, total_updated)

            for check in checks:
                check_pause()
                fn = CHECK_FUNCTIONS.get(check)
                if fn:
                    try:
                        fn(company, triggered_by=triggered_by)
                        total_updated += 1
                    except Exception as e:
                        print(f"  [{CHECK_LABELS.get(check, check)} error] {e}")

            total_processed += 1

        append_history("bulk-recheck", started_iso, total_processed, total_updated,
                       "completed", triggered_by)
        send_search_email("bulk-recheck", started_iso, total_processed, total_updated,
                          triggered_by, get_session_log())

    except Exception as e:
        tb = _tb.format_exc()
        print(f"\n  [CRASH] Unhandled exception in Bulk Recheck: {e}")
        print(tb)
        append_history("bulk-recheck", started_iso, total_processed, total_updated,
                       f"error: {type(e).__name__}: {e}", triggered_by, notes=tb[:1500])
        raise

    finally:
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Bulk recheck complete!")
    print(f"  Companies processed: {total_processed}")
    print(f"  Records updated:     {total_updated}")
    print("=" * 60)


if __name__ == "__main__":
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    run_recheck(triggered_by)
