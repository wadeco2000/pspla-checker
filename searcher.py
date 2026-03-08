import os
import json
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from datetime import datetime, timezone

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TERMS_FILE = os.path.join(BASE_DIR, "search_terms.json")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

NZ_REGIONS = [
    # Major cities
    "Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga",
    "Dunedin", "Palmerston North", "Napier", "New Plymouth", "Whangarei",
    "Nelson", "Invercargill", "Gisborne", "Whanganui", "Rotorua",
    "Hastings", "Blenheim", "Timaru", "Pukekohe", "Taupo",
    # Northland
    "Kerikeri", "Kaitaia", "Dargaville",
    # Wellington region
    "Lower Hutt", "Upper Hutt", "Porirua", "Paraparaumu",
    # Waikato
    "Thames", "Te Awamutu", "Tokoroa",
    # Bay of Plenty
    "Whakatane", "Katikati", "Te Puke",
    # Hawke's Bay
    "Waipukurau", "Wairoa",
    # Taranaki
    "Hawera", "Stratford",
    # Manawatu
    "Levin", "Feilding",
    # Tasman/Nelson
    "Motueka", "Richmond",
    # Marlborough
    "Picton",
    # West Coast
    "Greymouth", "Westport",
    # Canterbury
    "Rangiora", "Ashburton", "Rolleston",
    # Otago
    "Queenstown", "Wanaka", "Oamaru", "Alexandra",
    # Southland
    "Gore",
]

_DEFAULT_GOOGLE_TERMS = [
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
    # Social / search
    "youtube.com", "facebook.com", "google.com", "wikipedia.org",
    "linkedin.com", "instagram.com", "twitter.com", "x.com",
    # NZ directories / review sites
    "trademe.co.nz", "yellowpages.co.nz", "localist.co.nz",
    "neighbourly.co.nz", "finda.co.nz", "nzherald.co.nz",
    "stuff.co.nz", "yelp.com",
    # NZ retail chains (sell gear, don't install)
    "pbtech.co.nz", "jbhifi.co.nz", "noelleeming.co.nz",
    "harveynorman.co.nz", "thewarehouse.co.nz", "countdown.co.nz",
    "supercheapauto.co.nz", "mitre10.co.nz", "bunnings.co.nz",
    "smarthomenz.nz", "aliexpress.com", "amazon.com",
    # Aggregator / listicle sites
    "angi.com", "hipages.com.au", "bark.com",
    "houzz.com", "homestars.com",
]


SERPAPI_EXHAUSTED = "SERPAPI_EXHAUSTED"


def google_search(query, num_results=10, time_filter=None):
    url = "https://serpapi.com/search"
    params = {
        "api_key": SERPAPI_KEY,
        "engine": "google",
        "q": query,
        "num": num_results,
        "gl": "nz",
        "hl": "en"
    }
    if time_filter:
        params["tbs"] = time_filter
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
            error_msg = data["error"]
            print(f"  [SerpAPI error] {error_msg}")
            if "run out" in error_msg.lower() or "limit" in error_msg.lower() or "credits" in error_msg.lower():
                return SERPAPI_EXHAUSTED
        return results
    except Exception as e:
        print(f"  [Search error] {e}")
        return []


def find_facebook_url(company_name, soup=None, raw_html=""):
    """Return a facebook.com page URL for the company, or None.
    Checks the already-scraped website HTML first (free), then falls
    back to a Google site:facebook.com search (uses SerpAPI quota)."""
    import re as _re

    # 1. Look for a facebook.com link in the scraped HTML
    if soup:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = _re.match(r"https?://(www\.)?facebook\.com/([^/?#\s\"']+)", href)
            if m and m.group(2).lower() not in ("sharer", "share", "sharer.php",
                                                  "dialog", "login", "home.php", ""):
                return href.split("?")[0].rstrip("/")

    # 2. Regex scan the raw HTML for facebook.com URLs (catches JS/data attrs)
    if raw_html:
        for m in _re.finditer(
                r"https?://(www\.)?facebook\.com/([A-Za-z0-9._\-]+)", raw_html):
            slug = m.group(2).lower()
            if slug not in ("sharer", "share", "sharer.php", "dialog",
                             "login", "home.php", ""):
                return m.group(0).split("?")[0].rstrip("/")

    # 3. Google site:facebook.com fallback
    try:
        query = f'site:facebook.com "{company_name}" New Zealand'
        results = google_search(query, num_results=3)
        if results and results is not SERPAPI_EXHAUSTED:
            for r in results:
                link = r.get("link", "")
                m = _re.match(r"https?://(www\.)?facebook\.com/([^/?#\s]+)", link)
                if m and m.group(2).lower() not in ("sharer", "pages", "groups",
                                                      "events", "marketplace", ""):
                    return link.split("?")[0].rstrip("/")
    except Exception:
        pass

    return None


def scrape_website(url):
    """Returns (page_text, email_or_None, facebook_url_or_None).
    Extracts mailto: and facebook links directly from HTML."""
    import re
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-NZ,en;q=0.9",
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 403:
            print(f"  [Scrape blocked 403] {url} - will use search snippet only")
            return "", None, None
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract email from mailto: links before truncating text
        scraped_email = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip().lower()
                if "@" in addr and not addr.startswith("@"):
                    scraped_email = addr
                    break

        # Also scan raw HTML for mailto: in case they're in JS/data attributes
        if not scraped_email:
            m = re.search(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', response.text)
            if m:
                scraped_email = m.group(1).lower()

        # Extract Facebook URL from the website HTML (no API cost)
        scraped_facebook = find_facebook_url("", soup=soup, raw_html=response.text)

        # Remove chrome (nav/header/footer/sidebar) before capping so actual content isn't crowded out
        for tag in soup.find_all(["nav", "header", "footer",
                                   "script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(True, {"role": ["navigation", "banner", "contentinfo"]}):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ", strip=True).split())[:5000]
        return text, scraped_email, scraped_facebook
    except Exception as e:
        print(f"  [Scrape error] {url}: {e}")
        return "", None, None


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
- other_names: list of OTHER NAMES THIS SAME COMPANY trades under or abbreviates itself as. Do NOT include brands they sell, products they stock, partner companies, or manufacturer names.
- phone: phone number (NZ format)
- email: email address
- address: physical address including city
- region: NZ region or city
- director_names: list of any owner, director, founder or principal names mentioned on the page

Important: Look carefully for the full company name in the footer, about page, contact details, and copyright notices. Companies often use abbreviations in their URL but their full name elsewhere.
Also look for owner/director names in "about us", "meet the team", "our story" sections.
If page content is empty or blocked, try to extract what you can from the URL and search snippet alone.
Do NOT include large national retail chains (Noel Leeming, Harvey Norman, JB Hi-Fi, The Warehouse, etc.) - only include companies whose primary business is installing/servicing security systems.

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


def _co_fetch_directors(company_number, co_headers):
    """Fetch director names from a Companies Office company detail page."""
    import re as _re
    try:
        detail_url = f"https://app.companiesoffice.govt.nz/companies/app/ui/pages/companies/{company_number}"
        resp = requests.get(detail_url, headers=co_headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        directors = []
        i = 0
        while i < len(lines):
            # Trigger: "Showing N of N directors"
            if _re.search(r"Showing \d+ of \d+ director", lines[i], _re.IGNORECASE):
                # Names follow immediately — collect until we hit an address/section boundary
                for j in range(i + 1, min(i + 30, len(lines))):
                    line = lines[j]
                    # Stop at known section headers or address lines
                    if any(kw in line for kw in ["Shareholding", "Documents", "PPSR",
                                                  "Company record link", "Trading Name",
                                                  "Phone Number", "Email Address"]):
                        break
                    # Director name: all letters+spaces, 5-60 chars, no digits
                    clean = line.replace(" ", "").replace("-", "").replace("'", "")
                    if (5 < len(line) < 60
                            and not any(c.isdigit() for c in line)
                            and clean.isalpha()):
                        name = " ".join(line.split())  # collapse multiple spaces
                        name = name.title()
                        if name not in directors:
                            directors.append(name)
            i += 1
        return directors
    except Exception:
        return []


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
        co_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36"
        }
        response = requests.get(url, params=params, headers=co_headers, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        # Find company names in results - they appear in ALL CAPS
        # Also capture the company number which appears on the next line as "(NNNNNNN)"
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        company_name_upper = company_name.upper()
        import re as _re

        def _find_match(lines, name_upper):
            address_words = ["road", "street", "avenue", "drive", "place", "lane", "way", "rd ", "st ", "ave "]
            for i, line in enumerate(lines):
                if name_upper in line:
                    address = None
                    company_number = None
                    for j in range(i + 1, min(i + 8, len(lines))):
                        lj = lines[j]
                        # Company number looks like "(9364441)" or "(9364441) (NZBN:...)"
                        m = _re.match(r"\((\d{6,8})\)", lj)
                        if m and not company_number:
                            company_number = m.group(1)
                        if any(w in lj.lower() for w in address_words) and len(lj) > 10 and not address:
                            address = lj
                        if address and company_number:
                            break
                    return {"name": line.title(), "address": address, "company_number": company_number}
            return None

        result = _find_match(lines, company_name_upper)

        # If no exact match, try Claude
        if result is None:
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
                raw = message.content[0].text.strip()
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                result = json.loads(raw.strip())

        if result is None:
            return {"name": None, "address": None, "directors": []}

        # Fetch directors from the detail page if we have a company number
        directors = []
        if result.get("company_number"):
            directors = _co_fetch_directors(result["company_number"], co_headers)
            if directors:
                print(f"  [Companies Office directors] {directors}")

        return {"name": result.get("name"), "address": result.get("address"), "directors": directors}

    except Exception as e:
        print(f"  [Companies Office error] {e}")
    return {"name": None, "address": None, "directors": []}


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


def find_email_via_google(domain):
    """Search Google for any email address at this domain (e.g. "@alarmwatch.co.nz")."""
    import re
    query = f'"@{domain}"'
    results = google_search(query, num_results=5)
    if not results or results == SERPAPI_EXHAUSTED:
        return None
    email_pattern = re.compile(r'[a-zA-Z0-9._%+\-]+@' + re.escape(domain), re.IGNORECASE)
    for r in results:
        for text in (r.get("snippet", ""), r.get("title", "")):
            match = email_pattern.search(text)
            if match:
                return match.group(0).lower()
    return None


def get_root_domain(url):
    """Extract root domain from URL e.g. https://www.adtsecurity.co.nz/branches/hamilton -> adtsecurity.co.nz"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        return domain
    except:
        return url


def company_exists(website):
    url = f"{SUPABASE_URL}/rest/v1/Companies?website=eq.{requests.utils.quote(website)}"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return len(data) > 0
    except:
        return False


def get_domain_record(domain):
    """Check if we already have a record for this root domain."""
    url = f"{SUPABASE_URL}/rest/v1/Companies?root_domain=eq.{requests.utils.quote(domain)}&select=*&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return data[0] if data else None
    except:
        return None


def company_name_exists(name):
    """Check if a company with this name already exists (case-insensitive)."""
    url = f"{SUPABASE_URL}/rest/v1/Companies?company_name=ilike.{requests.utils.quote(name)}&select=id&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return len(data) > 0
    except:
        return False


def get_company_by_name(name):
    """Return the existing DB record for this company name (case-insensitive), or None."""
    if not name:
        return None
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    url = (f"{SUPABASE_URL}/rest/v1/Companies"
           f"?company_name=ilike.{requests.utils.quote(name)}"
           f"&select=id,region&limit=1")
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return data[0] if data else None
    except:
        return None


def append_region_to_company(company_id, existing_region, new_region):
    """Add new_region to the company's region field if it isn't already listed."""
    if not new_region:
        return
    existing = [r.strip() for r in (existing_region or "").split(",") if r.strip()]
    if any(r.lower() == new_region.lower() for r in existing):
        return  # already there
    merged = ", ".join(existing + [new_region])
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}",
            headers=headers,
            json={"region": merged})
    except Exception:
        pass


def check_pspla_individual(name):
    """Search PSPLA for an individual license."""
    try:
        url = "https://forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select"
        params = {
            "rows": "5",
            "fl": "*, score",
            "sort": "score desc",
            "json.nl": "map",
            "q": f"name_txt:({name})",
            "fq": "jurisdictionCode_s:PSPLA AND permitHasBeenIssued_b:true",
            "wt": "json"
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://forms.justice.govt.nz/search/PSPLA/"
        }
        response = requests.get(url, params=params, headers=headers, timeout=15)
        data = response.json()
        docs = data.get("response", {}).get("docs", [])

        def get_status(d):
            s = d.get("permitStatus_s", "")
            if isinstance(s, list): s = s[0] if s else ""
            return s.lower()

        active = [d for d in docs if get_status(d) == "active"]
        if active:
            matched = active[0]
            name_field = matched.get("name_txt") or matched.get("caseTitle_s", name)
            if isinstance(name_field, list): name_field = name_field[0]
            return {"found": True, "name": name_field}
    except Exception as e:
        print(f"  [Individual PSPLA error] {e}")
    return {"found": False, "name": None}


PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pause.flag")
RUNNING_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "running.flag")
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_progress.json")
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_status.json")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_history.json")

# Single source of truth for all columns saved to the Companies table.
# Add new fields here and they are automatically included in the schema check.
RECORD_TEMPLATE = {
    "company_name": None,
    "website": None,
    "phone": None,
    "email": None,
    "address": None,
    "region": None,
    "pspla_licensed": None,
    "pspla_name": None,
    "pspla_address": None,
    "pspla_license_number": None,
    "pspla_license_status": None,
    "pspla_license_expiry": None,
    "license_type": None,
    "match_method": None,
    "match_reason": None,
    "companies_office_name": None,
    "companies_office_address": None,
    "individual_license": None,
    "director_name": None,
    "root_domain": None,
    "source_url": None,
    "last_checked": None,
    "notes": None,
}


def check_schema():
    """Check that the Companies table has all required columns before starting."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    cols = ",".join(RECORD_TEMPLATE.keys())
    try:
        # SELECT all required columns with limit=0 — fast, works with anon key.
        # Returns 200 (even if table is empty) if all columns exist, 400 if any are missing.
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/Companies?select={cols}&limit=0",
            headers=headers,
            timeout=10,
        )
        if response.status_code == 200:
            print(f"  [Schema check OK] All {len(RECORD_TEMPLATE)} required columns present.")
            return True

        # Find exactly which columns are missing by checking one at a time
        missing = []
        for col in RECORD_TEMPLATE:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/Companies?select={col}&limit=0",
                headers=headers,
                timeout=10,
            )
            if r.status_code != 200:
                missing.append(col)

        if missing:
            print(f"  [Schema check FAILED] Missing columns in Companies table:")
            for col in missing:
                print(f"    - {col}")
        else:
            print(f"  [Schema check FAILED] {response.json().get('message', response.text[:200])}")
        return False
    except Exception as e:
        print(f"  [Schema check error] {e}")
        return False


def check_pause():
    """Block here if pause.flag exists, until it's removed."""
    if os.path.exists(PAUSE_FLAG):
        print("  [PAUSED] Waiting for resume...")
        while os.path.exists(PAUSE_FLAG):
            time.sleep(2)
        print("  [RESUMED]")


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_regions": [], "total_found": 0, "total_new": 0}


def save_progress(completed_regions, total_found, total_new):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"completed_regions": completed_regions, "total_found": total_found, "total_new": total_new}, f)


def clear_progress():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


def write_status(phase, region, term, region_idx, term_idx, total_regions, total_terms, total_found, total_new):
    """Write current search position to status file so the dashboard can show a progress bar."""
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({
                "phase": phase,
                "region": region,
                "term": term,
                "region_idx": region_idx,
                "term_idx": term_idx,
                "total_regions": total_regions,
                "total_terms": total_terms,
                "total_found": total_found,
                "total_new": total_new,
            }, f)
    except Exception:
        pass


def clear_status():
    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)


def append_history(run_type, started_iso, total_found, total_new, status="completed", triggered_by="manual"):
    """Append a run record to search_history.json (newest first, capped at 100)."""
    finished = datetime.now(timezone.utc)
    try:
        started_dt = datetime.fromisoformat(started_iso)
        duration_minutes = round((finished - started_dt).total_seconds() / 60, 1)
    except Exception:
        duration_minutes = None
    record = {
        "type": run_type,
        "started": started_iso,
        "finished": finished.isoformat(),
        "duration_minutes": duration_minutes,
        "total_found": total_found,
        "total_new": total_new,
        "status": status,
        "triggered_by": triggered_by,
    }
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    history.insert(0, record)
    history = history[:100]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"  [History write error] {e}")


_DEFAULT_FACEBOOK_TERMS = [
    "security camera installation",
    "CCTV installation",
    "security camera installer",
    "security alarm installation",
    "CCTV installer",
    "security camera company",
]


def _load_search_terms():
    """Load search terms from JSON file, writing defaults if file is missing."""
    try:
        if os.path.exists(TERMS_FILE):
            with open(TERMS_FILE) as f:
                data = json.load(f)
            return (data.get("google") or _DEFAULT_GOOGLE_TERMS,
                    data.get("facebook") or _DEFAULT_FACEBOOK_TERMS)
    except Exception:
        pass
    # Write defaults on first run
    try:
        with open(TERMS_FILE, "w") as f:
            json.dump({"google": _DEFAULT_GOOGLE_TERMS, "facebook": _DEFAULT_FACEBOOK_TERMS}, f, indent=2)
    except Exception:
        pass
    return _DEFAULT_GOOGLE_TERMS, _DEFAULT_FACEBOOK_TERMS


SEARCH_TERMS, FACEBOOK_SEARCH_TERMS = _load_search_terms()


def process_and_save_company(info, website_url, root_domain, source_label, fallback_region=""):
    """Run PSPLA/CO checks on extracted company info and save to DB.
    Returns True if a new record was saved."""
    company_name = info["company_name"]
    website_region = info.get("region") or fallback_region

    # If this company name already exists, just add the region and move on
    existing = get_company_by_name(company_name)
    if existing:
        append_region_to_company(existing["id"], existing.get("region", ""), website_region)
        print(f"  [Skipped] {company_name} already exists — added region '{website_region}' if new")
        return False

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
    for name in names_to_try:
        print(f"  [Checking PSPLA] {name}")
        res = check_pspla(name, website_region=website_region)
        if res.get("matched_name"):
            matched = res["matched_name"]
            verification = verify_pspla_match(
                company_name, matched, website_region, res.get("pspla_address")
            )
            if not verification.get("match"):
                print(f"  [Verify rejected] {company_name} vs {matched} - {verification.get('reason')}")
                if pspla_result is None:
                    pspla_result = {"licensed": False, "matched_name": None, "license_type": None,
                                    "match_method": f"rejected: {verification.get('reason')}",
                                    "pspla_address": None, "pspla_license_number": None,
                                    "pspla_license_status": None, "pspla_license_expiry": None}
                continue
            if name != company_name:
                print(f"  [Matched via] {name}")
            pspla_result = res
            if res.get("licensed"):
                break
        elif pspla_result is None:
            pspla_result = res

    # Check for individual license using director names
    individual_license_found = None
    if not pspla_result.get("licensed"):
        directors = info.get("director_names") or []
        for director in directors:
            print(f"  [Checking individual license] {director}")
            ind = check_pspla_individual(director)
            if ind.get("found"):
                individual_license_found = ind["name"]
                print(f"  [Individual license found] {individual_license_found}")
                break
        time.sleep(1)

    # Companies Office lookup
    co_search_name = pspla_result.get("matched_name") or company_name
    co_result = check_companies_office(co_search_name, pspla_address=pspla_result.get("pspla_address"))
    time.sleep(2)

    licensed_val = pspla_result.get("licensed")
    if not isinstance(licensed_val, bool):
        licensed_val = None

    # Merge directors from website + Companies Office (deduplicated)
    directors = list(info.get("director_names") or [])
    co_directors = co_result.get("directors") or []
    for d in co_directors:
        if d and not any(d.lower() == x.lower() for x in directors):
            directors.append(d)

    # If CO found directors that the website didn't, check their individual PSPLA licences now
    if co_directors and not individual_license_found and not licensed_val:
        for director in co_directors:
            if any(director.lower() == x.lower() for x in (info.get("director_names") or [])):
                continue  # already checked above
            print(f"  [Checking CO director individual license] {director}")
            ind = check_pspla_individual(director)
            if ind.get("found"):
                individual_license_found = director
                licensed_val = True
                break

    reason_parts = []
    if info.get("_fb_snippet"):
        reason_parts.append(f"Facebook description: \"{info['_fb_snippet']}\"")
    reason_parts.append(f"Searched PSPLA for: {', '.join(names_to_try)}.")

    if licensed_val is True:
        reason_parts.append(f"Active company license found for '{pspla_result.get('matched_name')}' (match method: {pspla_result.get('match_method')}).")
        if pspla_result.get("pspla_license_number"):
            reason_parts.append(f"License #{pspla_result.get('pspla_license_number')}, status: {pspla_result.get('pspla_license_status') or 'active'}, expires {pspla_result.get('pspla_license_expiry') or 'unknown'}.")
        if pspla_result.get("pspla_address"):
            reason_parts.append(f"PSPLA registered address: {pspla_result.get('pspla_address')}.")
        if directors:
            reason_parts.append(f"Director/owner names found on website: {', '.join(directors)} (individual license check not needed).")
        else:
            reason_parts.append("No director/owner names found on website.")
    elif individual_license_found:
        reason_parts.append("No active company license found on PSPLA.")
        if directors:
            reason_parts.append(f"Director/owner names found on website: {', '.join(directors)}.")
            reason_parts.append(f"Checked individual PSPLA licenses for each — active individual license found under '{individual_license_found}'.")
        else:
            reason_parts.append(f"Individual PSPLA license found under '{individual_license_found}'.")
    elif (pspla_result.get("pspla_license_status") or "").lower() == "expired":
        reason_parts.append(f"Found PSPLA entry for '{pspla_result.get('matched_name')}' but license status is EXPIRED (match method: {pspla_result.get('match_method')}).")
        if directors:
            reason_parts.append(f"Director/owner names found on website: {', '.join(directors)}.")
            reason_parts.append("Checked individual PSPLA licenses for each — " + (f"active individual license found under '{individual_license_found}'." if individual_license_found else "no active individual license found."))
        else:
            reason_parts.append("No director/owner names found on website to check for individual licenses.")
    elif "rejected" in (pspla_result.get("match_method") or ""):
        reason_parts.append(f"A potential PSPLA match was found but rejected as a different company: {pspla_result.get('match_method')}.")
        if directors:
            reason_parts.append(f"Director/owner names found on website: {', '.join(directors)}.")
            reason_parts.append("Checked individual PSPLA licenses for each — " + (f"active individual license found under '{individual_license_found}'." if individual_license_found else "no active individual license found."))
        else:
            reason_parts.append("No director/owner names found on website to check for individual licenses.")
    else:
        reason_parts.append("No match found on PSPLA.")
        if directors:
            reason_parts.append(f"Director/owner names found on website: {', '.join(directors)}.")
            reason_parts.append("Checked individual PSPLA licenses for each — " + (f"active individual license found under '{individual_license_found}'." if individual_license_found else "no active individual license found."))
        else:
            reason_parts.append("No director/owner names found on website to check for individual licenses.")

    if co_result.get("name"):
        reason_parts.append(f"Companies Office search for '{co_search_name}' found: {co_result['name']}" + (f" at {co_result['address']}." if co_result.get("address") else "."))
    else:
        reason_parts.append(f"Companies Office search for '{co_search_name}' returned no match.")

    record = {
        "company_name": company_name,
        "website": website_url,
        "phone": info.get("phone"),
        "email": info.get("email"),
        "address": info.get("address"),
        "region": website_region,
        "pspla_licensed": licensed_val,
        "pspla_name": pspla_result.get("matched_name"),
        "pspla_address": pspla_result.get("pspla_address"),
        "pspla_license_number": pspla_result.get("pspla_license_number"),
        "pspla_license_status": pspla_result.get("pspla_license_status"),
        "pspla_license_expiry": pspla_result.get("pspla_license_expiry"),
        "license_type": pspla_result.get("license_type"),
        "match_method": pspla_result.get("match_method"),
        "match_reason": " ".join(reason_parts),
        "companies_office_name": co_result.get("name"),
        "companies_office_address": co_result.get("address"),
        "individual_license": individual_license_found,
        "director_name": ", ".join(directors),
        "facebook_url": info.get("facebook_url"),
        "root_domain": root_domain,
        "source_url": info.get("_fb_url") or website_url,
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "notes": f"Found via: {source_label}",
    }

    # Final dedup guard — catches same company found via different URLs/paths
    if company_name_exists(company_name):
        print(f"  [Duplicate name] '{company_name}' already in DB — skipping")
        return False

    if save_to_supabase(record):
        if licensed_val is True:
            status = "LICENSED"
        elif licensed_val is False:
            status = "NOT LICENSED"
        else:
            status = "UNKNOWN"
        print(f"  [Saved] PSPLA Status: {status}")
        return True
    else:
        print("  [Error] Failed to save to database")
        return False


def normalise_fb_url(url):
    """Strip locale/query params from a Facebook URL for deduplication.
    e.g. facebook.com/anztechltd/?locale=fa_IR -> facebook.com/anztechltd/"""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment="")).rstrip("/")


def extract_website_from_snippet(snippet):
    """Pull the first non-Facebook http URL out of a Google search snippet."""
    import re
    SOCIAL = ("facebook.com", "instagram.com", "twitter.com", "linkedin.com",
              "youtube.com", "tiktok.com", "google.com")
    for m in re.finditer(r'https?://[^\s\)\]"\'<>]+', snippet):
        url = m.group(0).rstrip(".,;:-")
        if not any(s in url for s in SOCIAL):
            return url
    return None


def extract_website_from_facebook(fb_url):
    """Scrape a Facebook business page and return the company's own website URL, or None."""
    import re
    from urllib.parse import unquote
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-NZ,en;q=0.9",
        }
        response = requests.get(fb_url, headers=headers, timeout=10)
        html = response.text

        # Facebook encodes outbound links as l.facebook.com/l.php?u=<encoded-url>
        encoded_links = re.findall(r'l\.facebook\.com/l\.php\?u=([^&"\'>\s]+)', html)
        for encoded in encoded_links:
            decoded = unquote(encoded)
            if decoded.startswith("http") and "facebook.com" not in decoded:
                clean = decoded.split("?")[0].rstrip("/")
                if "." in clean:
                    return clean

        # Fallback: look for direct href links to non-Facebook domains
        SOCIAL = ("facebook.com", "instagram.com", "twitter.com", "linkedin.com",
                  "youtube.com", "tiktok.com")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if (href.startswith("http")
                    and not any(s in href for s in SOCIAL)
                    and ("." in href.split("//")[-1].split("/")[0])):
                return href.split("?")[0].rstrip("/")
    except Exception as e:
        print(f"  [FB scrape error] {e}")
    return None


def extract_from_fb_snippet(title, snippet, fb_url, region):
    """Use Claude to extract company info from a Facebook search result snippet."""
    prompt = f"""Extract NZ security/CCTV company information from this Facebook page search result.

Facebook URL: {fb_url}
Page title: {title}
Search snippet: {snippet}
Region hint: {region}

Only extract if this appears to be an NZ company that installs security cameras, CCTV, or security alarms.
If not relevant, return null.

Return JSON:
- company_name: business name (strip "| Facebook" from title)
- phone: phone number if visible
- email: email if visible
- address: physical address if visible
- region: NZ region/city
- director_names: list of owner/director names if mentioned
- other_names: []
- legal_name: null

Return ONLY valid JSON or null."""
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        if text.lower().startswith("null"):
            return None
        if "{" in text:
            start = text.index("{")
            depth = 0
            end = start
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            text = text[start:end + 1]
        return json.loads(text)
    except Exception as e:
        print(f"  [Claude FB extract error] {e}")
        return None


def _process_fb_result(result, found_urls_all, fb_total, fb_new, term, fallback_region):
    """Process a single Facebook search result. Returns updated (fb_total, fb_new)."""
    skip_paths = ["/groups/", "/marketplace/", "/events/", "/photos/",
                  "/videos/", "/posts/", "/reels/", "/stories/"]
    fb_url = result["link"]

    if "facebook.com" not in fb_url:
        return fb_total, fb_new
    if any(p in fb_url for p in skip_paths):
        return fb_total, fb_new

    fb_url_norm = normalise_fb_url(fb_url)
    if fb_url_norm in found_urls_all:
        return fb_total, fb_new
    found_urls_all.add(fb_url_norm)

    if company_exists(fb_url_norm):
        print(f"  [Already in DB] {fb_url_norm}")
        return fb_total, fb_new

    print(f"  [FB Page] {fb_url_norm}")
    fb_total += 1

    website_url = extract_website_from_facebook(fb_url_norm)
    if not website_url or "facebook.com" in website_url:
        website_url = extract_website_from_snippet(result.get("snippet", ""))
    time.sleep(1)

    if website_url and "facebook.com" not in website_url and website_url not in found_urls_all:
        found_urls_all.add(website_url)
        root_domain = get_root_domain(website_url)

        if get_domain_record(root_domain):
            print(f"  [Domain already in DB] {root_domain}")
            return fb_total, fb_new

        print(f"  [Website found] {website_url}")
        page_text, scraped_email, scraped_facebook = scrape_website(website_url)
        time.sleep(1)

        info = extract_company_info(website_url, page_text, result["snippet"])
        if not info or not info.get("company_name"):
            info = extract_from_fb_snippet(result["title"], result["snippet"], fb_url_norm, fallback_region)

        if not info or not info.get("company_name"):
            print("  [Skipped] Could not extract company name")
            return fb_total, fb_new

        if not info.get("email") and scraped_email:
            info["email"] = scraped_email
        if scraped_facebook:
            info["facebook_url"] = scraped_facebook
        if not info.get("email"):
            found_email = find_email_via_google(root_domain)
            if found_email:
                info["email"] = found_email

    else:
        info = extract_from_fb_snippet(result["title"], result["snippet"], fb_url_norm, fallback_region)
        if not info or not info.get("company_name"):
            print("  [Skipped] Could not extract company name from snippet")
            return fb_total, fb_new
        website_url = fb_url_norm
        root_domain = get_root_domain(fb_url_norm)

    info["_fb_snippet"] = result.get("snippet", "")
    info["_fb_url"] = fb_url_norm
    source_label = f"Facebook {term} {fallback_region}".strip()
    if process_and_save_company(info, website_url, root_domain, source_label, fallback_region):
        fb_new += 1

    return fb_total, fb_new


def run_facebook_search(found_urls_all, regions=None, include_nationwide=True):
    """Search Facebook for NZ security camera companies. Returns (total_found, total_new).
    - regions: subset of NZ_REGIONS to search; defaults to all.
    - include_nationwide: also run a 'New Zealand' wide pass to catch businesses
      that don't mention a specific town on their Facebook page."""
    search_regions = regions if regions is not None else NZ_REGIONS
    print("\n" + "=" * 60)
    print("  Facebook Search Pass")
    print("=" * 60)

    fb_total = 0
    fb_new = 0

    for region in search_regions:
        check_pause()
        print(f"\n[Facebook] {region}")

        for term in FACEBOOK_SEARCH_TERMS:
            check_pause()
            region_idx = search_regions.index(region) + 1
            term_idx = FACEBOOK_SEARCH_TERMS.index(term) + 1
            write_status("facebook", region, term, region_idx, term_idx,
                         len(search_regions), len(FACEBOOK_SEARCH_TERMS), fb_total, fb_new)
            query = f'site:facebook.com "{term}" "{region}" New Zealand -group -marketplace -"for sale"'
            print(f"  Query: {query}")

            results = google_search(query, num_results=10)
            time.sleep(1)

            if results is SERPAPI_EXHAUSTED:
                print("\n  [STOPPED] SerpAPI exhausted during Facebook pass.")
                return fb_total, fb_new

            if not results:
                continue

            for result in results:
                check_pause()
                fb_total, fb_new = _process_fb_result(
                    result, found_urls_all, fb_total, fb_new, term, region)

    # Nationwide pass — catches NZ businesses that don't mention a specific town
    if include_nationwide:
        print("\n[Facebook] NZ-wide pass (no region filter)")
        total_regions_display = len(search_regions) + 1
        for term in FACEBOOK_SEARCH_TERMS:
            check_pause()
            term_idx = FACEBOOK_SEARCH_TERMS.index(term) + 1
            write_status("facebook", "NZ nationwide", term,
                         total_regions_display, term_idx,
                         total_regions_display, len(FACEBOOK_SEARCH_TERMS),
                         fb_total, fb_new)
            query = f'site:facebook.com "{term}" "New Zealand" -group -marketplace -"for sale"'
            print(f"  Query: {query}")

            results = google_search(query, num_results=10)
            time.sleep(1)

            if results is SERPAPI_EXHAUSTED:
                print("\n  [STOPPED] SerpAPI exhausted during nationwide Facebook pass.")
                return fb_total, fb_new

            if not results:
                continue

            for result in results:
                check_pause()
                fb_total, fb_new = _process_fb_result(
                    result, found_urls_all, fb_total, fb_new, term, "")

    return fb_total, fb_new


def run_search(triggered_by="manual"):
    print("=" * 60)
    print("  PSPLA Security Camera Company Checker")
    print("=" * 60)
    started_iso = datetime.now(timezone.utc).isoformat()

    # Sanity check — abort if the database is missing any required columns
    print("  Checking database schema...")
    if not check_schema():
        print("  Aborting. Add the missing columns to the Companies table in Supabase, then re-run.")
        return

    # Clear any stale pause flag left over from a previous session
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)

    # Write running flag so dashboard knows search is active
    open(RUNNING_FLAG, "w").close()

    # Load previous progress if resuming
    progress = load_progress()
    completed_regions = progress["completed_regions"]
    total_found = progress["total_found"]
    total_new = progress["total_new"]

    if completed_regions:
        print(f"  Resuming — {len(completed_regions)} regions already done: {', '.join(completed_regions)}")

    try:
        found_urls = set()  # tracks all URLs seen across regions + Facebook pass

        for region in NZ_REGIONS:
            if region in completed_regions:
                print(f"\n[Skipping] {region} — already completed")
                continue

            print(f"\nSearching region: {region}")

            for term in SEARCH_TERMS:
                check_pause()
                region_idx = NZ_REGIONS.index(region) + 1
                term_idx = SEARCH_TERMS.index(term) + 1
                write_status("google", region, term, region_idx, term_idx,
                             len(NZ_REGIONS), len(SEARCH_TERMS), total_found, total_new)
                query = f"{term} {region} New Zealand"
                print(f"  Query: {query}")

                results = google_search(query)
                time.sleep(1)

                if results is SERPAPI_EXHAUSTED:
                    print("\n  [STOPPED] SerpAPI searches exhausted.")
                    print(f"  Progress saved — completed regions: {', '.join(completed_regions) or 'none'}")
                    print("  Upgrade your SerpAPI plan or wait for next month, then re-run to resume.")
                    save_progress(completed_regions, total_found, total_new)
                    append_history("full", started_iso, total_found, total_new, "stopped", triggered_by)
                    return

                for result in results:
                    check_pause()
                    url = result["link"]

                    if url in found_urls:
                        continue
                    found_urls.add(url)

                    if any(domain in url for domain in SKIP_DOMAINS):
                        continue

                    if company_exists(url):
                        print(f"  [Already in DB] {url}")
                        continue

                    # Check if we already have this domain (national company branch pages)
                    root_domain = get_root_domain(url)
                    existing = get_domain_record(root_domain)
                    if existing and existing.get("pspla_licensed") == "true":
                        print(f"  [Domain already licensed] {root_domain} - saving branch entry")
                        branch_record = {
                            "company_name": existing["company_name"],
                            "website": url,
                            "region": region,
                            "pspla_licensed": existing["pspla_licensed"],
                            "pspla_name": existing["pspla_name"],
                            "pspla_address": existing["pspla_address"],
                            "pspla_license_number": existing["pspla_license_number"],
                            "pspla_license_status": existing["pspla_license_status"],
                            "pspla_license_expiry": existing["pspla_license_expiry"],
                            "license_type": existing["license_type"],
                            "match_method": "inherited from parent domain",
                            "root_domain": root_domain,
                            "source_url": url,
                            "last_checked": datetime.now(timezone.utc).isoformat(),
                            "notes": f"Branch of {root_domain}"
                        }
                        save_to_supabase(branch_record)
                        continue

                    print(f"  [Found] {url}")
                    total_found += 1

                    page_text, scraped_email, scraped_facebook = scrape_website(url)
                    time.sleep(1)

                    info = extract_company_info(url, page_text, result["snippet"])
                    if not info or not info.get("company_name"):
                        print("  [Skipped] Could not extract company name")
                        continue

                    # Use mailto: email from HTML if Claude didn't find one in the text
                    if not info.get("email") and scraped_email:
                        info["email"] = scraped_email
                        print(f"  [Email from mailto] {scraped_email}")
                    if scraped_facebook:
                        info["facebook_url"] = scraped_facebook

                    print(f"  [Company] {info['company_name']}")

                    # Email fallback: Google search for "@domain" if still no email
                    if not info.get("email"):
                        found_email = find_email_via_google(root_domain)
                        if found_email:
                            info["email"] = found_email
                            print(f"  [Email via Google] {found_email}")

                    if process_and_save_company(info, url, root_domain, f"{term} {region}", region):
                        total_new += 1

            # Region complete — save progress so we can resume if interrupted
            completed_regions.append(region)
            save_progress(completed_regions, total_found, total_new)
            print(f"  [Progress saved] {region} done ({len(completed_regions)}/{len(NZ_REGIONS)} regions)")

        # All regions done — run Facebook pass then clear progress
        fb_found, fb_new_count = run_facebook_search(found_urls)
        total_found += fb_found
        total_new += fb_new_count

        clear_progress()
        append_history("full", started_iso, total_found, total_new, "completed", triggered_by)

    finally:
        # Always clean up flags and status when done or crashed
        clear_status()
        for flag in [RUNNING_FLAG, PAUSE_FLAG]:
            if os.path.exists(flag):
                os.remove(flag)

    print("\n" + "=" * 60)
    print(f"  Search complete!")
    print(f"  Total URLs found:      {total_found}")
    print(f"  New companies added:   {total_new}")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    triggered_by = "scheduled" if "--scheduled" in sys.argv else "manual"
    run_search(triggered_by=triggered_by)
