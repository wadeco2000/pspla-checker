"""
test_facebook.py — dry-run test for the Facebook search functions.

Searches one region (default: Auckland) for a couple of terms, prints
everything found. Does NOT save to the database or call PSPLA/CO checks.
Run from the project directory:
    python test_facebook.py
    python test_facebook.py Wellington
"""

import sys
import time
from searcher import (
    google_search,
    normalise_fb_url,
    extract_website_from_facebook,
    extract_website_from_snippet,
    extract_from_fb_snippet,
    extract_company_info,
    scrape_website,
    find_email_via_google,
    get_root_domain,
    FACEBOOK_SEARCH_TERMS,
    SERPAPI_EXHAUSTED,
)

REGION = sys.argv[1] if len(sys.argv) > 1 else "Auckland"
# Limit to first 3 terms so the test doesn't chew through too many SerpAPI credits
TERMS = FACEBOOK_SEARCH_TERMS[:3]

SKIP_PATHS = ["/groups/", "/marketplace/", "/events/", "/photos/",
              "/videos/", "/posts/", "/reels/", "/stories/"]


def separator():
    print("\n" + "-" * 60)


def run_test():
    print(f"Facebook search test — region: {REGION}")
    print(f"Terms: {TERMS}\n")

    seen_urls = set()
    found = []

    for term in TERMS:
        query = f'site:facebook.com "{term}" "{REGION}" New Zealand -group -marketplace -"for sale"'
        print(f"Query: {query}")

        results = google_search(query, num_results=10)
        time.sleep(1)

        if results is SERPAPI_EXHAUSTED:
            print("[STOPPED] SerpAPI quota exhausted.")
            break

        if not results:
            print("  No results.")
            continue

        for result in results:
            fb_url = result["link"]

            if "facebook.com" not in fb_url:
                continue
            if any(p in fb_url for p in SKIP_PATHS):
                continue
            fb_url_norm = normalise_fb_url(fb_url)
            if fb_url_norm in seen_urls:
                continue
            seen_urls.add(fb_url_norm)

            separator()
            print(f"FB page : {fb_url_norm}")
            print(f"Title   : {result.get('title', '')}")
            print(f"Snippet : {result.get('snippet', '')}")

            # Try to find website: 1) scrape FB page, 2) URL in snippet text
            website_url = extract_website_from_facebook(fb_url_norm)
            if not website_url or "facebook.com" in website_url:
                website_url = extract_website_from_snippet(result.get("snippet", ""))
            time.sleep(1)

            info = None
            source = "snippet"

            if website_url and "facebook.com" not in website_url:
                print(f"Website : {website_url}")
                page_text, scraped_email = scrape_website(website_url)
                time.sleep(1)
                info = extract_company_info(website_url, page_text, result["snippet"])
                if info:
                    source = "website"
                    if not info.get("email") and scraped_email:
                        info["email"] = scraped_email
                    if not info.get("email"):
                        domain = get_root_domain(website_url)
                        found_email = find_email_via_google(domain)
                        if found_email:
                            info["email"] = found_email
            else:
                print("Website : (none found on FB page or snippet)")

            if not info:
                info = extract_from_fb_snippet(result["title"], result["snippet"], fb_url_norm, REGION)
                if info:
                    source = "FB snippet"
                    if not info.get("email"):
                        domain = get_root_domain(fb_url_norm)
                        found_email = find_email_via_google(domain)
                        if found_email:
                            info["email"] = found_email

            if not info:
                print("Result  : [skipped — Claude could not extract company name]")
                continue

            print(f"Source  : {source}")
            print(f"Name    : {info.get('company_name', '-')}")
            print(f"Region  : {info.get('region', '-')}")
            print(f"Phone   : {info.get('phone', '-')}")
            print(f"Email   : {info.get('email', '-')}")
            print(f"Address : {info.get('address', '-')}")
            if info.get("director_names"):
                print(f"Directors: {', '.join(info['director_names'])}")

            found.append({
                "fb_url": fb_url,
                "website": website_url,
                "source": source,
                **info,
            })

    separator()
    print(f"\nDone. {len(found)} companies extracted from {len(seen_urls)} FB pages.")
    print("Nothing was saved to the database.")


if __name__ == "__main__":
    run_test()
