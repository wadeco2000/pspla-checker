import os
import json
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from datetime import datetime

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

NZ_REGIONS = [
    "Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga",
    "Dunedin", "Palmerston North", "Napier", "New Plymouth", "Whangarei",
    "Nelson", "Invercargill", "Gisborne", "Whanganui", "Rotorua",
    "Hastings", "Blenheim", "Timaru", "Pukekohe", "Taupo"
]

SEARCH_TERMS = [
    "security camera installer",
    "CCTV installer",
    "IP camera installation",
    "security camera installation company",
    "CCTV installation company",
    "security alarm installation",
    "alarm system installer",
    "IT security camera install",
    "network camera installation",
    "surveillance camera installation",
    "security system installer",
    "intruder alarm installer",
    "CCTV security alarm",
    "electrical security camera installation",
    "smart home security camera"
]

SKIP_DOMAINS = [
    "youtube.com", "facebook.com", "trademe.co.nz",
    "google.com", "wikipedia.org", "linkedin.com",
    "instagram.com", "twitter.com", "yellowpages.co.nz"
]


def google_search(query, num_results=10):
    url = "https://serpapi.com/search"
    params = {
        "api_key": SERPAPI_KEY,
        "engine": "google",
        "q": query,
        "num": num_results,
        "gl": "nz",
        "hl": "en"
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        results = []
        if "organic_results" in data:
            for item in data["organic_results"]:
                results.append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", "")
                })
        elif "error" in data:
            print(f"  [SerpAPI error] {data['error']}")
        return results
    except Exception as e:
        print(f"  [Search error] {e}")
        return []


def scrape_website(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-NZ,en;q=0.9",
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 403:
            print(f"  [Scrape blocked 403] {url} - will use search snippet only")
            return ""
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)[:3000]
        return text
    except Exception as e:
        print(f"  [Scrape error] {url}: {e}")
        return ""


def extract_company_info(url, page_text, search_snippet):
    prompt = f"""Extract company information from this New Zealand business website.
The company may be a security company, IT company, electrician, or any other business that installs security cameras, CCTV, IP cameras, surveillance systems, or security alarms.

URL: {url}
Search snippet: {search_snippet}
Page content: {page_text}

Only extract if this company installs security cameras, CCTV, IP cameras, surveillance systems or security alarms in New Zealand.
If they do not offer these services, return null.

If they do, extract and return JSON with these fields:
- company_name: the PRIMARY trading name shown on the website (not abbreviations)
- legal_name: the full legal name including Ltd/Limited if found anywhere on the page
- other_names: list of any other business names, abbreviations, or trading names found
- phone: phone number (NZ format)
- email: email address
- address: physical address including city
- region: NZ region or city

Important: Look carefully for the full company name in the footer, about page, contact details, and copyright notices. Companies often use abbreviations in their URL but their full name elsewhere.
If page content is empty or blocked, try to extract what you can from the URL and search snippet alone.

Return ONLY valid JSON or null."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        # Handle markdown code blocks
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        # Handle "null" response
        if text.lower().startswith("null"):
            return None
        # Extract just the JSON object if there's extra text
        if "{" in text:
            start = text.index("{")
            # Find matching closing brace
            depth = 0
            end = start
            for i, ch in enumerate(text[start:], start):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            text = text[start:end+1]
        result = json.loads(text)
        if isinstance(result, list):
            result = result[0] if result else None
        return result
    except Exception as e:
        print(f"  [Claude extract error] {e}")
        return None


def pspla_search(query):
    """Run a search against the PSPLA Solr API."""
    url = "https://forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select"
    params = {
        "facet": "true",
        "rows": "10",
        "fl": "*, score",
        "facet.limit": "-1",
        "facet.mincount": "-1",
        "sort": "score desc",
        "json.nl": "map",
        "q": f"name_txt:({query})",
        "fq": "jurisdictionCode_s:PSPLA AND permitHasBeenIssued_b:true",
        "wt": "json"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://forms.justice.govt.nz/search/PSPLA/"
    }
    response = requests.get(url, params=params, headers=headers, timeout=15)
    data = response.json()
    return data.get("response", {}).get("docs", []), data.get("response", {}).get("numFound", 0)


def extract_keywords(company_name):
    """Extract meaningful keywords from a company name."""
    stop_words = {"limited", "ltd", "nz", "n.z.", "new", "zealand", "the", "and", "&",
                  "co", "company", "group", "services", "solutions", "systems", "technologies",
                  "technology", "tech", "security", "electrical", "holdings", "cctv", "camera",
                  "cameras", "install", "installer", "installation", "alarm", "alarms",
                  "auckland", "wellington", "christchurch", "hamilton", "tauranga", "dunedin",
                  "northland", "waikato", "otago", "canterbury", "nelson", "gisborne", "taranaki"}
    words = company_name.lower().replace("(", "").replace(")", "").replace(",", "").split()
    keywords = [w for w in words if w not in stop_words and len(w) > 2]
    return keywords


def verify_pspla_match(website_company, pspla_company, website_region, pspla_address):
    """Use Claude to verify if a PSPLA match is genuinely the same company."""
    try:
        prompt = f"""Are these likely the same company?

Website company name: "{website_company}"
Website region: "{website_region or 'unknown'}"

PSPLA registered name: "{pspla_company}"
PSPLA address: "{pspla_address or 'unknown'}"

Consider:
- Are the names similar enough to be the same company (trading name vs registered name)?
- Are the locations compatible?
- Could "Addz Livewire" and "Livewire Electrical Wellington" be the same? No - different cities and different first word.
- Could "Hines Security" and "Hines Electrical & Security NZ" be the same? Yes - same family name, same type of business.

Return ONLY JSON: {{"match": true or false, "confidence": "high/medium/low", "reason": "brief reason"}}"""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [Verify error] {e}")
        return {"match": False, "confidence": "low", "reason": "verification error"}


def check_pspla(company_name, website_region=None):
    try:
        match_method = None

        # Try 1: full name search
        docs, num_found = pspla_search(company_name)
        if num_found > 0:
            match_method = "full name"

        # Try 2: keyword search if no results (need at least 2 meaningful keywords)
        if num_found == 0:
            keywords = extract_keywords(company_name)
            if len(keywords) >= 2:
                keyword_query = " AND ".join(keywords[:3])
                docs, num_found = pspla_search(keyword_query)
                if num_found > 0:
                    match_method = f"keywords: {keyword_query}"

        # Try 3: first significant word only if it's long enough to be unique (6+ chars)
        if num_found == 0:
            keywords = extract_keywords(company_name)
            if keywords and len(keywords[0]) >= 6:
                docs, num_found = pspla_search(keywords[0])
                if num_found > 0:
                    match_method = f"keyword: {keywords[0]}"

        if num_found > 0 and docs:
            # For keyword/partial matches, verify with Claude before trusting
            if match_method and match_method != "full name":
                candidate_name = docs[0].get("name_txt") or docs[0].get("caseTitle_s", "")
                if isinstance(candidate_name, list): candidate_name = candidate_name[0] if candidate_name else ""
                candidate_address = docs[0].get("registeredOffice_txt") or docs[0].get("townCity_txt", "")
                if isinstance(candidate_address, list): candidate_address = candidate_address[0] if candidate_address else ""

                verification = verify_pspla_match(company_name, candidate_name, website_region, candidate_address)
                if not verification.get("match"):
                    print(f"  [Match rejected] {company_name} vs {candidate_name} - {verification.get('reason')}")
                    return {"licensed": False, "matched_name": None, "license_type": None, "match_method": f"rejected: {verification.get('reason')}", "pspla_address": None, "pspla_license_number": None, "pspla_license_status": None, "pspla_license_expiry": None}
                else:
                    match_method = f"{match_method} (verified: {verification.get('confidence')})"

            # Prefer active licenses over expired ones
            def get_status(d):
                s = d.get("permitStatus_s", "")
                if isinstance(s, list): s = s[0] if s else ""
                return s.lower()

            def get_field(d, key):
                val = d.get(key)
                if isinstance(val, list): val = val[0] if val else None
                return val

            active_docs = [d for d in docs if get_status(d) == "active"]
            has_active = len(active_docs) > 0
            matched = active_docs[0] if has_active else docs[0]

            name_field = get_field(matched, "name_txt") or get_field(matched, "caseTitle_s") or company_name
            pspla_address = get_field(matched, "registeredOffice_txt") or get_field(matched, "townCity_txt")
            permit_number = get_field(matched, "permitNumber_txt")
            permit_status = get_field(matched, "permitStatus_s")
            permit_expiry = get_field(matched, "permitEndDate_s")
            license_type = "individual" if matched.get("isIndividual_b") else "company"

            return {
                "licensed": has_active,
                "matched_name": name_field,
                "license_type": license_type,
                "match_method": match_method,
                "pspla_address": pspla_address,
                "pspla_license_number": permit_number,
                "pspla_license_status": permit_status,
                "pspla_license_expiry": permit_expiry
            }
        else:
            return {"licensed": False, "matched_name": None, "license_type": None, "match_method": "no match found", "pspla_address": None, "pspla_license_number": None, "pspla_license_status": None, "pspla_license_expiry": None}

    except Exception as e:
        print(f"  [PSPLA check error] {e}")
        return {"licensed": None, "matched_name": None, "license_type": None, "match_method": "error", "pspla_address": None}


def check_companies_office(company_name, pspla_address=None):
    try:
        # If we have a full registered name from PSPLA, search by first 2 words for specificity
        words = company_name.replace("(", "").replace(")", "").replace(".", "").split()
        non_generic = [w for w in words if w.lower() not in {"limited", "ltd", "nz", "the", "and", "&", "co"}]
        search_term = " ".join(non_generic[:3]) if non_generic else company_name

        url = "https://app.companiesoffice.govt.nz/companies/app/ui/pages/companies/search"
        params = {
            "q": search_term,
            "entityTypes": "ALL",
            "entityStatusGroups": "ALL",
            "start": "0",
            "limit": "15",
            "advancedPanel": "true",
            "mode": "advanced"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36"
        }
        response = requests.get(url, params=params, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        # Find company names in results - they appear in ALL CAPS
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        company_name_upper = company_name.upper()

        # Look for exact or close match
        for i, line in enumerate(lines):
            if company_name_upper in line:
                # Try to get a real address (contains street/road/avenue etc, not just numbers)
                address = None
                address_words = ["road", "street", "avenue", "drive", "place", "lane", "way", "rd ", "st ", "ave "]
                for j in range(i+1, min(i+6, len(lines))):
                    l = lines[j].lower()
                    if any(w in l for w in address_words) and len(lines[j]) > 10:
                        address = lines[j]
                        break
                return {"name": line.title(), "address": address}

        # If no exact match, use Claude to find best match
        candidates = [l for l in lines if l.isupper() and len(l) > 5][:20]
        if candidates and pspla_address:
            prompt = f"""From this list of NZ Companies Office results, which company best matches "{company_name}" with address near "{pspla_address}"?

Companies found:
{chr(10).join(candidates)}

Return ONLY JSON: {{"name": "best match or null", "address": null}}"""
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )
            text = message.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())

    except Exception as e:
        print(f"  [Companies Office error] {e}")
    return {"name": None, "address": None}


def save_to_supabase(record):
    url = f"{SUPABASE_URL}/rest/v1/Companies"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    try:
        response = requests.post(url, headers=headers, json=record)
        if response.status_code not in [200, 201]:
            print(f"  [Supabase error] {response.status_code}: {response.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"  [Supabase save error] {e}")
        return False


def company_exists(website):
    url = f"{SUPABASE_URL}/rest/v1/Companies?website=eq.{requests.utils.quote(website)}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return len(data) > 0
    except:
        return False


def run_search():
    print("=" * 60)
    print("  PSPLA Security Camera Company Checker")
    print("=" * 60)

    total_found = 0
    total_new = 0

    for region in NZ_REGIONS:
        print(f"\nSearching region: {region}")

        found_urls = set()

        for term in SEARCH_TERMS:
            query = f"{term} {region} New Zealand"
            print(f"  Query: {query}")

            results = google_search(query)
            time.sleep(1)

            for result in results:
                url = result["link"]

                if url in found_urls:
                    continue
                found_urls.add(url)

                if any(domain in url for domain in SKIP_DOMAINS):
                    continue

                if company_exists(url):
                    print(f"  [Already in DB] {url}")
                    continue

                print(f"  [Found] {url}")
                total_found += 1

                page_text = scrape_website(url)
                time.sleep(1)

                info = extract_company_info(url, page_text, result["snippet"])
                if not info or not info.get("company_name"):
                    print("  [Skipped] Could not extract company name")
                    continue

                company_name = info["company_name"]
                print(f"  [Company] {company_name}")

                # Build list of all names to try on PSPLA
                names_to_try = []
                if company_name:
                    names_to_try.append(company_name)
                if info.get("legal_name") and info["legal_name"] not in names_to_try:
                    names_to_try.append(info["legal_name"])
                for other in (info.get("other_names") or []):
                    if other and other not in names_to_try:
                        names_to_try.append(other)

                # Try each name on PSPLA until we find a match
                pspla_result = None
                website_region = info.get("region") or region
                for name in names_to_try:
                    print(f"  [Checking PSPLA] {name}")
                    result = check_pspla(name, website_region=website_region)
                    if result.get("licensed"):
                        pspla_result = result
                        if name != company_name:
                            print(f"  [Matched via] {name}")
                        break
                    elif pspla_result is None:
                        pspla_result = result  # keep first result as fallback

                co_result = {"name": None, "address": None}

                time.sleep(2)

                # Ensure pspla_licensed is always bool or None
                licensed_val = pspla_result.get("licensed")
                if not isinstance(licensed_val, bool):
                    licensed_val = None

                record = {
                    "company_name": company_name,
                    "website": url,
                    "phone": info.get("phone"),
                    "email": info.get("email"),
                    "address": info.get("address"),
                    "region": info.get("region") or region,
                    "pspla_licensed": licensed_val,
                    "pspla_name": pspla_result.get("matched_name"),
                    "pspla_address": pspla_result.get("pspla_address"),
                    "pspla_license_number": pspla_result.get("pspla_license_number"),
                    "pspla_license_status": pspla_result.get("pspla_license_status"),
                    "pspla_license_expiry": pspla_result.get("pspla_license_expiry"),
                    "license_type": pspla_result.get("license_type"),
                    "match_method": pspla_result.get("match_method"),
                    "companies_office_name": co_result.get("name"),
                    "companies_office_address": co_result.get("address"),
                    "source_url": url,
                    "last_checked": datetime.utcnow().isoformat(),
                    "notes": f"Found via: {term} {region}"
                }

                if save_to_supabase(record):
                    total_new += 1
                    if pspla_result.get("licensed") is True:
                        status = "LICENSED"
                    elif pspla_result.get("licensed") is False:
                        status = "NOT LICENSED"
                    else:
                        status = "UNKNOWN"
                    print(f"  [Saved] PSPLA Status: {status}")
                else:
                    print("  [Error] Failed to save to database")

    print("\n" + "=" * 60)
    print(f"  Search complete!")
    print(f"  Total URLs found:      {total_found}")
    print(f"  New companies added:   {total_new}")
    print("=" * 60)


if __name__ == "__main__":
    run_search()
