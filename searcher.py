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
CORRECTIONS_JSON = os.path.join(BASE_DIR, "corrections.json")
LESSONS_JSON = os.path.join(BASE_DIR, "lessons.json")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Session log — tracks new companies added in the current run for email notifications
_session_new_companies = []


def reset_session_log():
    """Call at the start of each search run to clear the session log."""
    global _session_new_companies
    _session_new_companies = []


def get_session_log():
    """Return a copy of the session log."""
    return list(_session_new_companies)

NZ_REGIONS = [
    # Major cities
    "Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga",
    "Dunedin", "Palmerston North", "Napier", "New Plymouth", "Whangarei",
    "Nelson", "Invercargill", "Gisborne", "Whanganui", "Rotorua",
    "Hastings", "Blenheim", "Timaru", "Pukekohe", "Taupo",
    # Auckland suburbs / districts
    "North Shore", "Henderson", "Manukau", "Papakura",
    "Howick", "Onehunga", "Manurewa", "Botany",
    "Pakuranga", "Waitakere", "Orewa", "Silverdale",
    "Takapuna", "Albany", "Glenfield", "Kumeu",
    # Northland
    "Kerikeri", "Kaitaia", "Dargaville",
    # Wellington region
    "Lower Hutt", "Upper Hutt", "Porirua", "Paraparaumu",
    # Waikato
    "Thames", "Te Awamutu", "Tokoroa",
    # Bay of Plenty
    "Whakatane", "Katikati", "Te Puke",
    # Tauranga suburbs
    "Mount Maunganui", "Papamoa",
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
    # Canterbury / Christchurch suburbs
    "Rangiora", "Ashburton", "Rolleston", "Hornby", "Papanui",
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
    # NZ local/city guide listicle sites
    "explorehamilton.co.nz", "exploreauckland.co.nz",
    "explorewellington.co.nz", "explorechristchurch.co.nz",
    "exploredunedin.co.nz", "exploretauranga.co.nz",
    "nzlocal.co.nz", "localguide.co.nz",
    # NZ government / council sites — contain IQP registers, contractor lists etc.
    # not a company's own website
    "govt.nz", "govt.nz/", "council.govt.nz", "ac.nz",
]

# URL path patterns that indicate a listing/directory page rather than a
# company's own website.  Matched against the URL path (case-insensitive).
_LISTING_PATH_PATTERNS = [
    "/best-", "/top-", "/best_", "/top_",
    "/directory/", "/listings/", "/list-of-",
    "/find-a-", "/find-an-", "/hire-a-", "/hire-an-",
    "/local-", "/compare-", "/reviews/",
    "-in-auckland", "-in-hamilton", "-in-wellington",
    "-in-christchurch", "-in-tauranga", "-in-dunedin",
    "-in-new-zealand", "-in-nz",
]


def is_directory_listing_url(url):
    """Return True if the URL looks like a listing/guide page on someone else's site."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.lower()
        # Block PDFs — IQP registers, contractor lists, council documents etc.
        if path.endswith(".pdf") or ".pdf?" in path or ".pdf#" in path:
            return True
        for pat in _LISTING_PATH_PATTERNS:
            if pat in path:
                return True
    except Exception:
        pass
    return False


SERPAPI_EXHAUSTED = "SERPAPI_EXHAUSTED"


def google_search(query, num_results=100, time_filter=None):
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
            # "no results" is normal for narrow queries — don't log it as an error
            no_results = "no results" in error_msg.lower() or "hasn't returned any results" in error_msg.lower()
            if not no_results:
                print(f"  [SerpAPI error] {error_msg}")
            if "run out" in error_msg.lower() or "limit" in error_msg.lower() or "credits" in error_msg.lower():
                return SERPAPI_EXHAUSTED
        return results
    except Exception as e:
        print(f"  [Search error] {e}")
        return []


def find_facebook_url(company_name, page_text=""):
    """Search Google for the company's Facebook page.

    Strategy:
    - Build search terms: original name + alt trading names from page_text.
    - Run site:facebook.com and plain 'name facebook' searches for each term.
    - Score candidates on: word match (original name words) + NZ signal bonus.
      The NZ bonus strongly favours pages with NZ city/domain signals in their
      URL or Google snippet, reliably beating same-named overseas pages even
      when they score equally on word match.
    - Hard-filter obvious overseas results (co.uk / .com.au in snippet).
    - Return the highest-scoring candidate.
    """
    import re as _re

    _SKIP_SLUGS = {"sharer", "sharer.php", "share", "dialog", "login",
                   "home.php", "pages", "groups", "events", "marketplace",
                   "people", ""}
    _SKIP_PATHS = {"posts", "photos", "videos", "events", "about",
                   "reviews", "community", "reels", "stories"}
    # Hard-exclude obvious non-NZ results
    _HARD_OVERSEAS = ["co.uk", ".uk", "united kingdom", ".com.au", "co.au"]
    # NZ city/domain signals — presence boosts score significantly
    _NZ_SIGNALS = ["new zealand", " nz", ".co.nz", ".nz",
                   "auckland", "wellington", "christchurch", "hamilton",
                   "tauranga", "dunedin", "palmerston north", "napier",
                   "hastings", "rotorua", "nelson", "invercargill",
                   "whangarei", "gisborne", "whanganui", "bay of plenty",
                   "waikato", "northland", "otago", "southland", "taranaki"]

    def _is_hard_overseas(result):
        text = (result.get("snippet", "") + " " + result.get("title", "")).lower()
        return any(ind in text for ind in _HARD_OVERSEAS)

    def _nz_bonus(url, snippet, title):
        combined = (url + " " + snippet + " " + title).lower()
        return 3 if any(sig in combined for sig in _NZ_SIGNALS) else 0

    def _word_score(url, snippet, name_words):
        haystack = (url.lower().replace("-", " ").replace("_", " ").replace("/", " ")
                    + " " + snippet.lower())
        return sum(1 for w in name_words if w in haystack)

    def _page_url_from_link(link):
        """Normalise any Facebook link to a page home URL.
        - Strips query string and trailing slash.
        - Normalises m.facebook.com to www.facebook.com.
        - Hard-rejects groups, marketplace, and other non-page sections.
        - If the URL is a content URL (/posts/, /photos/ etc.), extracts the
          base page URL so that posts mentioning the right company lead us to
          the page itself (e.g. /ChaytorCo/posts/xxx -> /ChaytorCo).
        Returns (page_url, is_content_url) or (None, False) if unusable."""
        link = link.split("?")[0].rstrip("/")
        link = _re.sub(r"^https?://m\.facebook\.com", "https://www.facebook.com", link)
        # Hard-reject groups, marketplace, events — never company pages
        if _re.search(r"facebook\.com/(groups|marketplace|events|watch)/", link):
            return None, False
        # Extract base page from content sub-paths
        content_re = r"(https?://(www\.)?facebook\.com/(?:(?:p|people)/)?[^/?#\s]+)/(?:" + \
                     "|".join(_SKIP_PATHS) + r")(?:/|$)"
        cm = _re.match(content_re, link)
        if cm:
            return cm.group(1), True
        # Numeric-only IDs (e.g. /897860640265587/) are unusable — skip
        slug_m = _re.match(r"https?://(www\.)?facebook\.com/(\d+)$", link)
        if slug_m:
            return None, False
        return link, False

    def _has_name_signal(url, name_words):
        """Return True if at least one company name word appears in the page
        URL slug only — not the title, which can contain any company's name
        when Google indexes a post or comment mentioning them."""
        slug_text = url.lower().replace("-", " ").replace("_", " ").replace("/", " ")
        return any(w in slug_text for w in name_words)

    def _extract_fb_candidates(results):
        found = []
        for r in results:
            if _is_hard_overseas(r):
                continue
            link = r.get("link", "")
            if "facebook.com" not in link:
                continue
            snippet = r.get("snippet", "")
            title = r.get("title", "")
            page_url, from_content = _page_url_from_link(link)
            if not page_url:
                continue
            # Validate as a recognisable Facebook page URL
            if not (_re.match(r"https?://(www\.)?facebook\.com/p/[^/?#\s]+", page_url) or
                    _re.match(r"https?://(www\.)?facebook\.com/people/[^/?#\s]+", page_url) or
                    _re.match(r"https?://(www\.)?facebook\.com/([^/?#\s]+)", page_url)):
                continue
            # Slug-only check for standard URLs
            slug_m = _re.match(r"https?://(www\.)?facebook\.com/([^/?#\s]+)$", page_url)
            if slug_m and slug_m.group(2).lower() in _SKIP_SLUGS:
                continue
            # KEY FILTER: reject pages whose slug and title share no words with
            # the company name — catches unrelated pages that merely mentioned
            # the company in a post (BDL Tauranga sharing a Chaytor post)
            if not _has_name_signal(page_url, name_words):
                continue
            found.append((page_url, snippet, title))
        return found

    def _alt_names(company_name, page_text):
        terms = [company_name]
        stripped = _re.sub(
            r'\b(Ltd\.?|Limited|NZ|New Zealand|Group)\b',
            '', company_name, flags=_re.IGNORECASE).strip(" -,")
        if stripped and stripped != company_name and len(stripped) >= 3:
            terms.append(stripped)
        if page_text:
            lead = _re.match(r'^([A-Z]{2,6})', company_name)
            if lead:
                ac = lead.group(1)
                matches = _re.findall(
                    r'\b' + _re.escape(ac) + r'\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?(?:\s+NZ\b)?',
                    page_text)
                for phrase in matches[:5]:
                    phrase = phrase.strip()
                    if phrase != company_name and len(phrase.split()) >= 2:
                        terms.append(phrase)
        return list(dict.fromkeys(terms))

    if not company_name:
        return None

    # Score only on original company name words — keeps scoring tight and predictable
    name_words = [w.lower() for w in _re.split(r'\W+', company_name) if len(w) >= 3]
    search_terms = _alt_names(company_name, page_text)

    try:
        all_candidates = []

        for term in search_terms:
            r1 = google_search(f'site:facebook.com "{term}"', num_results=50)
            if r1 and r1 is not SERPAPI_EXHAUSTED:
                all_candidates += _extract_fb_candidates(r1)
            r2 = google_search(f'"{term}" facebook', num_results=50)
            if r2 and r2 is not SERPAPI_EXHAUSTED:
                all_candidates += _extract_fb_candidates(r2)

        # Dedupe by URL, track score and NZ signal separately
        best_per_url = {}  # url -> (total_score, has_nz_signal)
        for url, snippet, title in all_candidates:
            ws = _word_score(url, snippet, name_words)
            nz = _nz_bonus(url, snippet, title)
            total = ws + nz
            has_nz = nz > 0
            if url not in best_per_url or total > best_per_url[url][0]:
                best_per_url[url] = (total, has_nz)

        if not best_per_url:
            return None

        ranked = sorted(best_per_url.items(), key=lambda x: -x[1][0])

        # Strongly prefer results with a NZ signal — only return non-NZ as last
        # resort after an explicit NZ-targeted search also finds nothing
        nz_results = [(url, data) for url, data in ranked if data[1]]
        if nz_results:
            return nz_results[0][0]

        # No NZ signal found — try one targeted search before giving up
        r_nz = google_search(f'site:facebook.com "{company_name}" "New Zealand"', num_results=10)
        if r_nz and r_nz is not SERPAPI_EXHAUSTED:
            nz_extra = _extract_fb_candidates(r_nz)
            nz_extra_signal = [(u, s, t) for u, s, t in nz_extra if _nz_bonus(u, s, t) > 0]
            if nz_extra_signal:
                best = sorted(nz_extra_signal,
                               key=lambda x: -(_word_score(x[0], x[1], name_words)
                                               + _nz_bonus(x[0], x[1], x[2])))
                return best[0][0]

        # Nothing with NZ signal — return None rather than a wrong-country page
        return None

    except Exception:
        pass
    return None


def find_linkedin_url(company_name, page_text=""):
    """Search Google for the company's LinkedIn company page.
    Returns a linkedin.com/company/... URL or None."""
    import re as _re

    _LI_RE = _re.compile(r'https?://(?:[a-z]{2}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%]+)', _re.I)

    def _extract_candidate(link):
        m = _LI_RE.match(link)
        return f"https://www.linkedin.com/company/{m.group(1).rstrip('/')}" if m else None

    # Check page_text for a linkedin.com/company/ link first
    m = _LI_RE.search(page_text)
    if m:
        return f"https://www.linkedin.com/company/{m.group(1).rstrip('/')}"

    # Name words — include short tokens (≥2 chars) so "ADT", "NZ" etc. are kept
    name_words = [w for w in _re.findall(r'[a-z0-9]+', company_name.lower()) if len(w) >= 2]
    # Generic words that add no signal on their own
    _GENERIC = {"security", "services", "limited", "solutions", "systems",
                "alarm", "alarms", "group", "new", "zealand", "install",
                "camera", "cctv", "surveillance", "protection", "management"}
    sig_words = [w for w in name_words if w not in _GENERIC]

    def _score(r):
        link = r.get("link", "")
        combined = (link + " " + r.get("snippet", "") + " " + r.get("title", "")).lower()
        # Slug from the LinkedIn URL itself — best signal
        slug = link.lower().replace("-", " ").replace("_", " ").replace("/", " ")
        word_hits = sum(1 for w in name_words if w in slug)
        sig_hits = sum(1 for w in sig_words if w in slug)
        nz_hit = 1 if any(s in combined for s in ["new zealand", " nz", ".co.nz", "auckland",
                           "wellington", "christchurch", "hamilton", "tauranga"]) else 0
        return sig_hits * 3 + word_hits * 2 + nz_hit

    # Try three progressively broader queries, collect all linkedin.com/company/ results
    queries = [
        f'site:linkedin.com/company "{company_name}" New Zealand',
        f'site:linkedin.com/company {company_name} New Zealand',
        f'"{company_name}" New Zealand site:linkedin.com',
    ]
    candidates = []
    for query in queries:
        results = google_search(query, num_results=5)
        if results is SERPAPI_EXHAUSTED:
            break
        if not results:
            continue
        for r in results:
            link = r.get("link", "")
            if not _LI_RE.search(link):
                continue
            c = _extract_candidate(link)
            if c:
                candidates.append((c, _score(r), r))
        if candidates:
            break  # Stop as soon as we get at least one linkedin hit

    if not candidates:
        return None

    # Pick highest-scoring candidate; require at least one word match
    candidates.sort(key=lambda x: -x[1])
    best_url, best_score, _ = candidates[0]
    return best_url if best_score >= 1 else None


def scrape_website(url):
    """Returns (page_text, email_or_None, facebook_url_or_None, linkedin_url_or_None).
    Extracts mailto:, facebook, and linkedin/company links directly from HTML."""
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
            return "", None, None, None
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

        # Extract LinkedIn company page URL from links
        scraped_linkedin = None
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0].rstrip("/")
            if re.search(r'linkedin\.com/company/[^/\s]+', href, re.I):
                scraped_linkedin = re.sub(r'^https?://(?:[a-z]{2}\.)?linkedin\.com', 'https://www.linkedin.com', href, flags=re.I)
                break

        # Remove chrome (nav/header/footer/sidebar) before capping so actual content isn't crowded out
        for tag in soup.find_all(["nav", "header", "footer",
                                   "script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(True, {"role": ["navigation", "banner", "contentinfo"]}):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ", strip=True).split())[:5000]
        return text, scraped_email, None, scraped_linkedin
    except Exception as e:
        print(f"  [Scrape error] {url}: {e}")
        return "", None, None, None


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


def generate_name_variations(company_name):
    """Return a list of name variants to try against PSPLA, handling:
    - Hyphens vs spaces:   'On-Guard' → 'On Guard'
    - Joined compound words: 'Onguard' → 'On guard' (split at pos 2 & 3)
    - CamelCase:           'OnGuard'  → 'On Guard'
    - Spaces removed:      'On Guard' → 'OnGuard'
    All variants are deduplicated and normalised to lowercase."""
    import re as _re
    variants = [company_name]

    # Hyphens → spaces
    if '-' in company_name:
        variants.append(company_name.replace('-', ' '))
        variants.append(company_name.replace('-', ''))

    # CamelCase split: 'OnGuard' → 'On Guard'
    camel = _re.sub(r'([a-z])([A-Z])', r'\1 \2', company_name)
    if camel != company_name:
        variants.append(camel)

    # Split each word at position 2 and 3 — catches 'Onguard' → 'On guard',
    # 'Elguard' → 'El guard', 'Proguard' → 'Pro guard' etc.
    words = company_name.split()
    for i, word in enumerate(words):
        w = word.strip('.,&')
        if len(w) >= 6:
            for pos in [2, 3]:
                prefix, suffix = w[:pos], w[pos:]
                if len(prefix) >= 2 and len(suffix) >= 3:
                    new_words = words[:i] + [prefix, suffix] + words[i+1:]
                    variants.append(' '.join(new_words))

    # Joined (spaces removed): 'On Guard' → 'OnGuard'
    joined = ''.join(company_name.split())
    if joined != company_name:
        variants.append(joined)

    # Dedupe preserving order, normalise whitespace
    seen = []
    for v in variants:
        v = ' '.join(v.split())
        if v.lower() not in [s.lower() for s in seen]:
            seen.append(v)
    return seen


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


_lessons_cache = None


def _load_lessons():
    """Load lessons from lessons.json. Cached per process."""
    global _lessons_cache
    if _lessons_cache is not None:
        return _lessons_cache
    if not os.path.exists(LESSONS_JSON):
        _lessons_cache = []
        return _lessons_cache
    try:
        with open(LESSONS_JSON, "r", encoding="utf-8") as f:
            _lessons_cache = json.load(f)
    except Exception:
        _lessons_cache = []
    return _lessons_cache


def _invalidate_lessons_cache():
    global _lessons_cache
    _lessons_cache = None


def _get_relevant_lessons(website_company, pspla_company):
    """Return lessons whose keywords appear in either company name (max 5)."""
    lessons = _load_lessons()
    if not lessons:
        return []
    both = (website_company + " " + pspla_company).lower()
    relevant = []
    for lesson in lessons:
        kws = lesson.get("keywords_to_watch") or []
        if any(kw.lower() in both for kw in kws):
            relevant.append(lesson)
    return relevant[:5]


def _generate_and_save_lesson(company_name, wrong_pspla_name, new_result):
    """Ask Claude why the false match happened and save the lesson."""
    new_matched = new_result.get("matched_name") or "no match found"
    prompt = f"""A PSPLA matching error was found and corrected in a New Zealand security company database.

Company website name: "{company_name}"
Wrong PSPLA match (false positive that was corrected): "{wrong_pspla_name}"
New result after correction: matched_name="{new_matched}", licensed={new_result.get('licensed')}

Analyse WHY the original false match happened and write a rule to prevent similar errors in future.

Return JSON only:
{{
  "pattern_name": "short_snake_case_identifier",
  "what_went_wrong": "1-2 sentences: why did the system incorrectly match these two companies?",
  "rule_to_apply": "A clear, specific rule written as a bullet point guideline for a matching system. Start with: If ...",
  "keywords_to_watch": ["list", "of", "2-6", "words", "that", "triggered", "the", "false", "match"]
}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        lesson = json.loads(raw.strip())
    except Exception as e:
        print(f"  [Lesson generation error] {e}")
        lesson = {
            "pattern_name": "unknown_pattern",
            "what_went_wrong": f"False match: {company_name} -> {wrong_pspla_name}",
            "rule_to_apply": f"If website is '{company_name}', do not match to '{wrong_pspla_name}'.",
            "keywords_to_watch": company_name.lower().split()[:4],
        }

    lesson["example_wrong_match"] = f"{company_name} -> {wrong_pspla_name}"
    lesson["example_correct_result"] = new_matched
    lesson["timestamp"] = datetime.now(timezone.utc).isoformat()

    existing = []
    if os.path.exists(LESSONS_JSON):
        try:
            with open(LESSONS_JSON, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(lesson)
    with open(LESSONS_JSON, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    _invalidate_lessons_cache()
    print(f"  [Lesson saved] {lesson['pattern_name']}: {lesson['rule_to_apply'][:80]}...")
    return lesson


def _llm_suggest_pspla_names(company_name, website_region, page_text, co_result, directors, extra_context=None):
    """Ask Claude to suggest PSPLA search terms based on all available company context.
    Returns a list of suggested names to try."""
    co_name = (co_result or {}).get("name") or ""
    co_address = (co_result or {}).get("address") or ""
    dirs_str = ", ".join((directors or [])[:5]) if directors else "none found"
    text_snippet = (page_text or "")[:2000]
    ctx = extra_context or {}
    facebook_snippet = ctx.get("facebook_snippet", "")
    linkedin_url = ctx.get("linkedin_url", "")
    nzsa_data = ctx.get("nzsa_data") or {}
    nzsa_name = nzsa_data.get("member_name") or nzsa_data.get("name") or ""
    nzsa_grade = nzsa_data.get("grade") or ""

    extra_lines = ""
    if facebook_snippet:
        extra_lines += f"\n- Facebook page description: \"{facebook_snippet[:300]}\""
    if linkedin_url:
        extra_lines += f"\n- LinkedIn company page: {linkedin_url}"
    if nzsa_name:
        extra_lines += f"\n- NZSA member name: \"{nzsa_name}\""
        if nzsa_grade:
            extra_lines += f" (grade: {nzsa_grade})"

    prompt = f"""You are helping find a New Zealand security company in the PSPLA (Private Security Personnel Licensing Authority) database.

All available information about this company:
- Website trading name: "{company_name}"
- Region/city: "{website_region or 'unknown'}"
- Companies Office registered name: "{co_name or 'not found'}"
- Companies Office address: "{co_address or 'not found'}"
- Directors/owners: {dirs_str}
- Website text excerpt: "{text_snippet}"{extra_lines}

Simple keyword searches of the PSPLA database found no match. The PSPLA uses official legal registered company names (e.g. "SMITH SECURITY SERVICES LIMITED") or individual full legal names for sole operators.

Based on all the information above, suggest up to 5 specific names or search terms to try in the PSPLA database. Consider:
- The Companies Office name is often the same as the PSPLA registered name
- The NZSA member name may differ from the trading name but match the legal name
- Directors may hold individual PSPLA licences under their personal name
- The company may trade under a different name than their legal registration
- Common NZ patterns: "TRADING NAME LIMITED", "SURNAME SECURITY LIMITED", etc.

Return JSON only:
{{
  "suggested_names": ["name1", "name2", "name3"],
  "reasoning": "brief explanation"
}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return result.get("suggested_names", [])
    except Exception as e:
        print(f"  [LLM suggest error] {e}")
        return []


def _llm_deep_verify(website_company, pspla_company, website_region, pspla_address, page_text, co_result, directors, extra_context=None):
    """Deep LLM verification using ALL available context — used when verify_pspla_match returns low/medium confidence."""
    co_name = (co_result or {}).get("name") or "not found"
    co_address = (co_result or {}).get("address") or "not found"
    dirs_str = ", ".join((directors or [])[:4]) if directors else "none"
    text_snippet = (page_text or "")[:1500]
    ctx = extra_context or {}
    facebook_snippet = ctx.get("facebook_snippet", "")
    linkedin_url = ctx.get("linkedin_url", "")
    nzsa_data = ctx.get("nzsa_data") or {}
    nzsa_name = nzsa_data.get("member_name") or nzsa_data.get("name") or ""
    nzsa_grade = nzsa_data.get("grade") or ""

    extra_lines = ""
    if facebook_snippet:
        extra_lines += f"\n- Facebook description: \"{facebook_snippet[:300]}\""
    if linkedin_url:
        extra_lines += f"\n- LinkedIn: {linkedin_url}"
    if nzsa_name:
        extra_lines += f"\n- NZSA member name: \"{nzsa_name}\""
        if nzsa_grade:
            extra_lines += f" (grade: {nzsa_grade})"

    prompt = f"""Verify if these are the same New Zealand security company. You have comprehensive evidence.

Website trading name: "{website_company}"
Website region: "{website_region or 'unknown'}"
Companies Office registered name: "{co_name}"
Companies Office address: "{co_address}"
Directors/owners: {dirs_str}
Website text excerpt: "{text_snippet}"{extra_lines}

PSPLA registered name: "{pspla_company}"
PSPLA registered address: "{pspla_address or 'unknown'}"

Using ALL the above evidence together, are these the same company? Look for:
- Whether the Companies Office name matches or resembles the PSPLA name
- Whether the NZSA member name matches the PSPLA name
- Whether addresses/regions are compatible
- Whether directors are mentioned in website text
- Whether the trading name logically maps to the PSPLA registered name

Return ONLY JSON: {{"match": true or false, "confidence": "high/medium/low", "reason": "explanation citing specific evidence used"}}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  [Deep verify error] {e}")
        return {"match": False, "confidence": "low", "reason": "deep verification error"}


def verify_pspla_match(website_company, pspla_company, website_region, pspla_address, _audit_name=None):
    """Use Claude to verify if a PSPLA match is genuinely the same company.
    _audit_name: if set, writes the LLM decision to the audit log."""
    import re as _re
    # Hard pre-check: every significant word in the website company name must appear
    # as an EXACT whole word in the PSPLA name.  Catches "coast" vs "coastal",
    # "guard" vs "guardian", etc. without a Claude call.
    _GENERIC = {'limited', 'security', 'services', 'solutions', 'systems', 'group',
                'new', 'zealand', 'national', 'management', 'alarm', 'alarms',
                'install', 'installer', 'surveillance', 'protection'}
    company_sig = [w for w in _re.findall(r'[a-z]+', website_company.lower())
                   if len(w) >= 4 and w not in _GENERIC]
    pspla_exact = set(_re.findall(r'[a-z]+', pspla_company.lower()))
    if company_sig:
        missing = [w for w in company_sig if w not in pspla_exact]
        if missing and len(missing) == len(company_sig):
            # Every distinctive word is absent — definitely not the same company
            return {"match": False, "confidence": "high",
                    "reason": f"Distinctive word(s) {missing} not found as exact words in PSPLA name"}

    # Load and inject relevant lessons
    relevant_lessons = _get_relevant_lessons(website_company, pspla_company)
    lessons_text = ""
    if relevant_lessons:
        lessons_text = "\nLearned rules from past corrections (apply these):\n"
        for les in relevant_lessons:
            lessons_text += f"- {les.get('rule_to_apply', '')}\n"

    try:
        prompt = f"""Are these likely the same company?

Website company name: "{website_company}"
Website region: "{website_region or 'unknown'}"

PSPLA registered name: "{pspla_company}"
PSPLA address: "{pspla_address or 'unknown'}"

Consider:
- Trading name vs registered name: a shorter trading name can match a longer registered name.
- If the website name appears word-for-word inside the PSPLA name, that is a strong match signal.
- Some companies have multiple regional legal entities (e.g. "Watchu Security Waikato Ltd" and "Watchu Security South Island Ltd") — if the PSPLA name is clearly the same brand with a regional qualifier added, and the address/region is compatible, that is a match.
- Location: use the PSPLA address (not just the name) for location matching. "South Island" covers Canterbury, Christchurch, Otago etc.
- Word differences matter: "Coast" and "Coastal" are DIFFERENT words. Only match if the exact words from the website name appear in the PSPLA name.
- Examples:
  - "Coast Security" vs "Kapiti Coast Security Limited" (region: Kapiti) → YES — same words, region matches.
  - "Coast Security" vs "Coastal Security Limited" → NO — "coast" is not a word in "Coastal Security Limited".
  - "Watchu Security" vs "Watchu Security South Island Limited" (address: Christchurch, region: Canterbury) → YES — same brand, South Island covers Canterbury.
  - "Watchu Security" vs "Watchu Security Waikato Limited" (address: Cambridge, region: Canterbury) → NO — Canterbury is not in the Waikato.
  - "Addz Livewire" vs "Livewire Electrical Wellington" → NO — different first word, different city.
  - "Hines Security" vs "Hines Electrical & Security NZ" → YES — same family name, same business type.
{lessons_text}
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
        result = json.loads(text.strip())
        if _audit_name:
            verdict = "ACCEPTED" if result.get("match") else "REJECTED"
            write_audit("llm_decision", None, _audit_name,
                        changes=f"verify_pspla_match {verdict} '{pspla_company}' "
                                f"(confidence: {result.get('confidence','?')}): {result.get('reason','')}",
                        triggered_by="verify_pspla_match",
                        notes=f"PSPLA address: {pspla_address or 'unknown'} | region: {website_region or 'unknown'}")
        return result
    except Exception as e:
        print(f"  [Verify error] {e}")
        return {"match": False, "confidence": "low", "reason": "verification error"}


_corrections_cache = None


def _load_corrections():
    """Load structured corrections from corrections.json. Cached per process."""
    global _corrections_cache
    if _corrections_cache is not None:
        return _corrections_cache
    if not os.path.exists(CORRECTIONS_JSON):
        _corrections_cache = []
        return _corrections_cache
    try:
        with open(CORRECTIONS_JSON, "r", encoding="utf-8") as f:
            _corrections_cache = json.load(f)
    except Exception:
        _corrections_cache = []
    return _corrections_cache


def invalidate_corrections_cache():
    """Call after saving a new correction so it takes effect immediately."""
    global _corrections_cache
    _corrections_cache = None


def _is_pspla_match_blocked(company_name, pspla_name):
    """Return (True, reason) if this company→PSPLA match has been flagged as a false positive."""
    corrections = _load_corrections()
    co_norm = company_name.lower().strip()
    pspla_norm = pspla_name.lower().strip()
    for c in corrections:
        if c.get("type") != "false_pspla_match":
            continue
        c_company = (c.get("company_name") or "").lower().strip()
        c_blocked = (c.get("blocked_pspla_name") or "").lower().strip()
        if not c_company or not c_blocked:
            continue
        # Flexible match: names can be substrings of each other (handles Ltd/Limited variants)
        company_match = c_company in co_norm or co_norm in c_company
        pspla_match = c_blocked in pspla_norm or pspla_norm in c_blocked
        if company_match and pspla_match:
            return True, c.get("reason", "flagged by user correction")
    return False, ""


def parse_and_save_correction(company_name, company_id, correction_text):
    """Use Claude to parse a free-text correction into structured JSON and save to corrections.json.
    Returns the parsed dict."""
    prompt = f"""A user found an error in a security company database entry.

Company in the database: "{company_name}"
User's correction note: "{correction_text}"

Analyze what type of error this is. Return JSON only with these fields:
- "type": one of "false_pspla_match" (this company was incorrectly matched to a PSPLA/license entry), "not_security_company" (not actually a security camera/alarm company), "wrong_data" (wrong email/phone/website/address), "other"
- "blocked_pspla_name": (only if type is "false_pspla_match") the name of the PSPLA entity that is NOT this company, extracted from the note. Be as specific as possible using words from the note.
- "summary": one sentence summarising what needs to be corrected.

Respond with a single JSON object, nothing else."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())
    except Exception as e:
        print(f"  [Correction parse error] {e}")
        parsed = {"type": "other", "summary": correction_text}

    parsed["company_name"] = company_name
    parsed["company_id"] = str(company_id)
    parsed["raw"] = correction_text
    parsed["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Load existing, append, save
    existing = []
    if os.path.exists(CORRECTIONS_JSON):
        try:
            with open(CORRECTIONS_JSON, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(parsed)
    with open(CORRECTIONS_JSON, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    invalidate_corrections_cache()
    return parsed


def _llm_cross_check_sources(company_name, website_region, pspla_result, co_result, nzsa_result, extra_context):
    """Cross-check all available data sources for internal consistency.
    Returns {"consistent": bool, "confidence": str, "issues": [], "notes": str}."""
    ctx = extra_context or {}
    facebook_snippet = ctx.get("facebook_snippet", "")
    linkedin_url = ctx.get("linkedin_url", "")

    pspla_name = (pspla_result or {}).get("matched_name") or "none"
    pspla_addr = (pspla_result or {}).get("pspla_address") or "unknown"
    co_name = (co_result or {}).get("name") or "none"
    co_address = (co_result or {}).get("address") or "unknown"
    nzsa_member_name = (nzsa_result or {}).get("member_name") or "none"
    nzsa_grade = (nzsa_result or {}).get("grade") or ""

    # Only run if at least 2 named sources are available
    named_sources = [s for s in [pspla_name, co_name, nzsa_member_name] if s and s != "none"]
    if len(named_sources) < 2 and not facebook_snippet:
        return {"consistent": True, "confidence": "high", "issues": [], "notes": "insufficient sources for cross-check"}

    prompt = f"""Cross-check these data sources about a New Zealand security company for consistency.

Website trading name: "{company_name}"
Website region: "{website_region or 'unknown'}"
PSPLA registered name: "{pspla_name}" (address: {pspla_addr})
Companies Office registered name: "{co_name}" (address: {co_address})
NZSA member name: "{nzsa_member_name}"{(' grade: ' + nzsa_grade) if nzsa_grade else ''}
Facebook description: "{facebook_snippet[:300] if facebook_snippet else 'not available'}"
LinkedIn: "{linkedin_url or 'not found'}"

Are all these sources consistently pointing to the same company?
Look for name mismatches, address/region conflicts, or business type conflicts suggesting a data mix-up.

Return ONLY JSON: {{"consistent": true or false, "confidence": "high/medium/low", "issues": ["list specific inconsistencies"], "notes": "brief summary"}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  [Cross-check error] {e}")
        return {"consistent": True, "confidence": "low", "issues": [], "notes": "cross-check error"}


def check_pspla(company_name, website_region=None, page_text=None, co_result=None, directors=None, extra_context=None):
    def _get_status(d):
        s = d.get("permitStatus_s", "")
        if isinstance(s, list): s = s[0] if s else ""
        return s.lower()

    try:
        match_method = None
        docs, num_found = [], 0

        # Try 1: full name + name variations
        # Prefer the first variant that finds an ACTIVE licence.
        # If only expired results are found for the first hit, keep trying other
        # variants — e.g. 'Onguard Security' finds expired Whakatane record but
        # 'On guard Security' finds the active 'On Guard Security Solutions Ltd'.
        fallback_docs, fallback_found, fallback_method = [], 0, None
        for variant in generate_name_variations(company_name):
            vdocs, vfound = pspla_search(variant)
            if vfound > 0:
                vm = "full name" if variant == company_name else f"name variant: {variant}"
                has_active = any(_get_status(d) == "active" for d in vdocs)
                if has_active:
                    docs, num_found, match_method = vdocs, vfound, vm
                    break  # active licence found — stop here
                if fallback_found == 0:
                    fallback_docs, fallback_found, fallback_method = vdocs, vfound, vm

        # If no variant had an active licence, use the first hit (expired/unknown)
        if num_found == 0 and fallback_found > 0:
            docs, num_found, match_method = fallback_docs, fallback_found, fallback_method

        # Try 2: keyword search (need at least 2 meaningful keywords)
        if num_found == 0:
            keywords = extract_keywords(company_name)
            if len(keywords) >= 2:
                keyword_query = " AND ".join(keywords[:3])
                docs, num_found = pspla_search(keyword_query)
                if num_found > 0:
                    match_method = f"keywords: {keyword_query}"

        # Try 3: first significant word only (6+ chars)
        if num_found == 0:
            keywords = extract_keywords(company_name)
            if keywords and len(keywords[0]) >= 6:
                docs, num_found = pspla_search(keywords[0])
                if num_found > 0:
                    match_method = f"keyword: {keywords[0]}"

        # Try 4: LLM-suggested names using all available context
        if num_found == 0 and (page_text or co_result or directors or extra_context):
            print(f"  [LLM search] No match via keywords — asking Claude for PSPLA name suggestions")
            llm_suggestions = _llm_suggest_pspla_names(
                company_name, website_region, page_text, co_result, directors, extra_context
            )
            if llm_suggestions:
                print(f"  [LLM search] Suggestions: {llm_suggestions}")
            for suggested in llm_suggestions:
                if not suggested or suggested.lower() == company_name.lower():
                    continue
                sdocs, sfound = pspla_search(suggested)
                if sfound > 0:
                    docs, num_found = sdocs, sfound
                    match_method = f"LLM-suggested: {suggested}"
                    print(f"  [LLM search] Found results for: {suggested}")
                    write_audit("llm_decision", None, company_name,
                                changes=f"Strategy 4: LLM suggested '{suggested}' -> found PSPLA results",
                                triggered_by="check_pspla",
                                notes=f"All suggestions tried: {llm_suggestions}")
                    break

        if num_found > 0 and docs:
            def get_field(d, key):
                val = d.get(key)
                if isinstance(val, list): val = val[0] if val else None
                return val

            def _date_sort_key(d):
                """Parse permitEndDate_s for sorting — most recent date = lower key value."""
                raw = get_field(d, "permitEndDate_s") or ""
                for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
                    try:
                        from datetime import datetime as _dt2
                        return -int(_dt2.strptime(str(raw), fmt).timestamp())
                    except ValueError:
                        pass
                return 0  # unknown date sorts last

            _STATUS_SORT = {"active": 0, "expired": 1, "withdrawn": 2}

            def _best_doc_for_permit(all_docs, permit_num):
                """Return the best-status doc among all docs sharing the same permit number."""
                same_permit = [d for d in all_docs if get_field(d, "permitNumber_txt") == permit_num]
                if not same_permit:
                    return None
                return sorted(same_permit,
                              key=lambda d: (_STATUS_SORT.get(_get_status(d), 99), _date_sort_key(d)))[0]

            # For keyword/partial/variant matches, verify with Claude before trusting.
            # Use Solr's original text-score order, but boost candidates whose PSPLA name
            # contains whole-word matches from the website region (e.g. "Kapiti Coast" in
            # "KAPITI COAST SECURITY LIMITED") — without disturbing cases with no region match.
            def _region_boost(d):
                """Sort key: number of region words NOT found as whole words in PSPLA name (lower = better)."""
                if not website_region:
                    return 0
                cname_words = set((get_field(d, "name_txt") or get_field(d, "caseTitle_s") or "").lower().split())
                region_words = [w for w in website_region.lower().split() if len(w) >= 4]
                return -sum(1 for rw in region_words if rw in cname_words)

            # Determine whether Solr returned results for multiple distinct companies.
            # If all docs share the same permit number it's genuinely one company and we
            # can trust docs[0] without verification.  If there are multiple permit numbers
            # (e.g. "Coast Security" matches Coastal Security AND Kapiti Coast Security)
            # we must verify even for a "full name" match — Solr fuzzy matching can put
            # the wrong company first.
            permit_nums_in_results = set(
                get_field(d, "permitNumber_txt") for d in docs
                if get_field(d, "permitNumber_txt")
            )
            multi_company_results = len(permit_nums_in_results) > 1

            needs_verification = (match_method and match_method != "full name") or multi_company_results

            if needs_verification:
                candidates = sorted(docs[:5], key=_region_boost)  # region-boosted, else Solr order
                verified_doc = None
                for cand in candidates:
                    cname = get_field(cand, "name_txt") or get_field(cand, "caseTitle_s") or ""
                    caddr = get_field(cand, "registeredOffice_txt") or get_field(cand, "townCity_txt") or ""
                    verification = verify_pspla_match(company_name, cname, website_region, caddr,
                                                     _audit_name=company_name)
                    if verification.get("match"):
                        # For low or medium confidence, do a deeper check using all available context
                        orig_confidence = verification.get("confidence")
                        if orig_confidence in ("low", "medium") and (page_text or co_result or directors or extra_context):
                            print(f"  [Deep verify] {orig_confidence} confidence match — checking with full context: {cname}")
                            deep = _llm_deep_verify(
                                company_name, cname, website_region, caddr,
                                page_text, co_result, directors, extra_context
                            )
                            if not deep.get("match"):
                                print(f"  [Deep verify rejected] {company_name} vs {cname} - {deep.get('reason')}")
                                write_audit("llm_decision", None, company_name,
                                            changes=f"Deep verify REJECTED '{cname}': {deep.get('reason', '')}",
                                            triggered_by="check_pspla",
                                            notes=f"Original confidence: {orig_confidence}")
                                continue
                            write_audit("llm_decision", None, company_name,
                                        changes=f"Deep verify CONFIRMED '{cname}': {deep.get('reason', '')}",
                                        triggered_by="check_pspla",
                                        notes=f"Original confidence: {orig_confidence} -> deep: {deep.get('confidence')}")
                            verification = deep
                            print(f"  [Deep verify confirmed] {cname} - {deep.get('reason')}")
                        verified_doc = cand
                        match_method = f"{match_method} (verified: {verification.get('confidence')})"
                        break
                    print(f"  [Match rejected] {company_name} vs {cname} - {verification.get('reason')}")

                if verified_doc is None and website_region:
                    # Fallback: try region-augmented searches (e.g. "Kapiti Coast Security"
                    # for "Coast Security" in Kapiti region) to find the correct company
                    # when the generic name is ambiguous.
                    region_words = [w for w in website_region.replace('/', ' ').split()
                                    if len(w) >= 4]
                    for rw in region_words:
                        augmented = f"{rw} {company_name}"
                        aug_docs, aug_found = pspla_search(augmented)
                        if aug_found > 0:
                            for cand in aug_docs[:3]:
                                cname = get_field(cand, "name_txt") or get_field(cand, "caseTitle_s") or ""
                                caddr = get_field(cand, "registeredOffice_txt") or get_field(cand, "townCity_txt") or ""
                                verification = verify_pspla_match(company_name, cname, website_region, caddr,
                                                                 _audit_name=company_name)
                                if verification.get("match"):
                                    verified_doc = cand
                                    match_method = f"region-augmented ({augmented}, verified: {verification.get('confidence')})"
                                    break
                        if verified_doc:
                            break

                if verified_doc is None:
                    return {"licensed": False, "matched_name": None, "license_type": None,
                            "match_method": "rejected: no candidate passed verification",
                            "pspla_address": None, "pspla_license_number": None,
                            "pspla_license_status": None, "pspla_license_expiry": None}

                # Upgrade: find the best-status doc for the same permit number
                permit_num = get_field(verified_doc, "permitNumber_txt")
                if permit_num:
                    matched = _best_doc_for_permit(docs, permit_num) or verified_doc
                else:
                    matched = verified_doc
            else:
                # Single company in results and full-name match — no verification needed.
                # Upgrade to best-status doc for that permit number (e.g. Active renewal).
                candidate = docs[0]
                permit_num = get_field(candidate, "permitNumber_txt")
                if permit_num:
                    matched = _best_doc_for_permit(docs, permit_num) or candidate
                else:
                    matched = candidate

            has_active = _get_status(matched) == "active"

            name_field = get_field(matched, "name_txt") or get_field(matched, "caseTitle_s") or company_name
            pspla_address = get_field(matched, "registeredOffice_txt") or get_field(matched, "townCity_txt")
            permit_number = get_field(matched, "permitNumber_txt")
            permit_status = get_field(matched, "permitStatus_s")
            permit_expiry = get_field(matched, "permitEndDate_s")
            license_type = "individual" if matched.get("isIndividual_b") else "company"

            # Check user corrections — block matches flagged as false positives
            blocked, block_reason = _is_pspla_match_blocked(company_name, name_field)
            if blocked:
                print(f"  [Correction applied] '{company_name}' -> '{name_field}' blocked: {block_reason}")
                return {"licensed": False, "matched_name": None, "license_type": None,
                        "match_method": "blocked by user correction",
                        "pspla_address": None, "pspla_license_number": None,
                        "pspla_license_status": None, "pspla_license_expiry": None}

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

        def _parse_co_result(lines, i, line):
            """Parse company number, NZBN, address and status from lines following a company name."""
            address_words = ["road", "street", "avenue", "drive", "place", "lane", "way", "rd ", "st ", "ave "]
            address = None
            company_number = None
            nzbn = None
            status = "registered"  # default
            for j in range(i + 1, min(i + 10, len(lines))):
                lj = lines[j]
                m = _re.match(r"\((\d{6,8})\)", lj)
                if m and not company_number:
                    company_number = m.group(1)
                nzbn_m = _re.search(r"NZBN:\s*(\d+)", lj)
                if nzbn_m and not nzbn:
                    nzbn = nzbn_m.group(1)
                # Status appears in the same line as the company number or a standalone line
                lj_lower = lj.lower()
                if "removed" in lj_lower:
                    status = "removed"
                elif "deregistered" in lj_lower:
                    status = "deregistered"
                if any(w in lj_lower for w in address_words) and len(lj) > 10 and not address:
                    address = lj
                if address and company_number:
                    break
            return {"name": line.title(), "address": address, "company_number": company_number,
                    "nzbn": nzbn, "status": status}

        def _find_match(lines, name_upper):
            for i, line in enumerate(lines):
                if name_upper in line:
                    return _parse_co_result(lines, i, line)
            return None

        def _find_all_co_results(lines):
            """Return all company entries found in the CO search result page."""
            results = []
            for i, line in enumerate(lines):
                # CO company names appear in ALL CAPS with 5+ chars and contain "LIMITED" or "LTD"
                if (line.isupper() and len(line) >= 5
                        and any(w in line for w in ["LIMITED", "LTD", "TRUST", "INCORPORATED"])):
                    results.append(_parse_co_result(lines, i, line))
            return results

        result = _find_match(lines, company_name_upper)

        # If the matched company is removed/deregistered, search more broadly for an active successor.
        # e.g. "Tarnix Security Limited" (Removed) → broader search "Tarnix" → finds "Tarnix Limited" (Registered)
        successor_result = None
        if result and result.get("status") in ("removed", "deregistered"):
            print(f"  [Companies Office] '{result['name']}' is {result['status']} — searching for active successor")
            # Build a shorter keyword from the first 1-2 distinctive words
            distinctive = [w for w in non_generic[:2] if len(w) >= 4]
            if distinctive:
                broad_term = " ".join(distinctive[:1])  # just the most distinctive word
                broad_params = {**params, "q": broad_term}
                try:
                    broad_resp = requests.get(url, params=broad_params, headers=co_headers, timeout=15)
                    broad_soup = BeautifulSoup(broad_resp.text, "html.parser")
                    broad_lines = [l.strip() for l in broad_soup.get_text(separator="\n", strip=True).split("\n") if l.strip()]
                    all_results = _find_all_co_results(broad_lines)
                    # Find active companies from the broader search that share distinctive words
                    for r in all_results:
                        if r.get("status") == "registered":
                            r_words = set(r["name"].lower().split())
                            if any(w.lower() in r_words for w in distinctive):
                                successor_result = r
                                print(f"  [Companies Office] Active successor found: {r['name']} ({r.get('company_number')})")
                                break
                except Exception as broad_e:
                    print(f"  [Companies Office broad search error] {broad_e}")

        # If no exact match at all, try Claude
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
            return {"name": None, "address": None, "company_number": None, "nzbn": None,
                    "status": None, "successor_name": None, "directors": []}

        # Fetch directors — prefer successor if original is removed
        active_result = successor_result if successor_result else result
        directors = []
        if active_result.get("company_number"):
            directors = _co_fetch_directors(active_result["company_number"], co_headers)
            if directors:
                print(f"  [Companies Office directors] {directors}")

        return {
            "name": result.get("name"),
            "address": result.get("address"),
            "company_number": result.get("company_number"),
            "nzbn": result.get("nzbn"),
            "status": result.get("status", "registered"),
            "successor_name": successor_result.get("name") if successor_result else None,
            "successor_number": successor_result.get("company_number") if successor_result else None,
            "successor_address": successor_result.get("address") if successor_result else None,
            "directors": directors,
        }

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
    results = google_search(query, num_results=10)
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


# ---------------------------------------------------------------------------
# NZSA member directory
# ---------------------------------------------------------------------------
_nzsa_cache = {"members": None, "fetched_at": 0}
NZSA_URL = "https://security.org.nz/public-info/find-a-member/"
NZSA_CACHE_TTL = 86400  # re-fetch at most once per day


def _decode_cf_email(encoded):
    """Decode a Cloudflare-obfuscated email address.
    encoded is the hex string after the # in /cdn-cgi/l/email-protection#..."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i+2], 16) ^ key) for i in range(2, len(encoded), 2))
    except Exception:
        return ""


def _fetch_nzsa_members():
    """Fetch and parse the full NZSA member directory.
    Returns a list of dicts with keys:
        name, locations, services, contact_name, phone, email, website,
        accredited (bool), grade (str or None)
    """
    import re as _re
    try:
        resp = requests.get(
            NZSA_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [NZSA fetch error] {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    members = []

    for label in soup.find_all("label", class_="accordion-header"):
        name = label.get_text(strip=True).replace("\u200b", "").strip()
        body = label.find_next_sibling("div", class_="accordion-body")
        if not name or not body:
            continue

        # Accreditation
        accred_div = body.find("div", class_="member-accred-manage")
        accredited = False
        grade = None
        if accred_div:
            h4 = accred_div.find("h4", class_="accredited")
            if h4:
                accredited = True
            p_text = accred_div.get_text(" ", strip=True)
            gm = _re.search(r"Grade:\s*(.+?)(?:Expiry|Security|$)", p_text)
            if gm:
                grade = gm.group(1).strip().rstrip(".")

        # Company overview
        overview = ""
        overview_h5 = body.find(lambda t: t.name in ("h4", "h5") and "Overview" in t.get_text())
        if overview_h5:
            p = overview_h5.find_next_sibling("p")
            if p:
                overview = p.get_text(strip=True)

        # Locations and services
        meta = body.find("div", class_="member-meta")
        locations, services = [], []
        if meta:
            boxes = meta.find_all("div", class_="box")
            for box in boxes:
                h4 = box.find("h4")
                if not h4:
                    continue
                items = [li.get_text(strip=True) for li in box.find_all("li")]
                if "Location" in h4.text:
                    locations = items
                elif "Service" in h4.text:
                    services = items

        # Contact
        contact_div = body.find("div", class_="member-contact")
        contact_name, phone, email, website = "", "", "", ""
        if contact_div:
            h4 = contact_div.find("h4")
            if h4:
                contact_name = h4.get_text(strip=True).replace("Contact:", "").strip()
            for p in contact_div.find_all("p"):
                txt = p.get_text(strip=True)
                if _re.search(r"\d{2,}", txt) and not email:
                    phone = _re.sub(r"[^\d\s\+\-\(\)]", "", txt).strip()
                a = p.find("a")
                if a:
                    href = a.get("href", "")
                    if "email-protection#" in href:
                        encoded = href.split("#")[-1]
                        email = _decode_cf_email(encoded)
                    elif href.startswith("http"):
                        website = href.rstrip("/")

        members.append({
            "name": name,
            "overview": overview,
            "locations": locations,
            "services": services,
            "contact_name": contact_name,
            "phone": phone,
            "email": email,
            "website": website,
            "accredited": accredited,
            "grade": grade,
        })

    return members


def _get_nzsa_members():
    """Return cached member list, refreshing if stale."""
    import time as _t
    if _nzsa_cache["members"] is None or (_t.time() - _nzsa_cache["fetched_at"]) > NZSA_CACHE_TTL:
        print("  [NZSA] Fetching member directory...")
        _nzsa_cache["members"] = _fetch_nzsa_members()
        _nzsa_cache["fetched_at"] = _t.time()
        print(f"  [NZSA] {len(_nzsa_cache['members'])} members loaded.")
    return _nzsa_cache["members"]


def _normalise_company_name(name):
    """Strip legal suffixes and punctuation for fuzzy matching."""
    import re as _re
    n = name.lower()
    n = _re.sub(r'\b(limited|ltd|nz|new zealand|inc|llp|lp|co)\b', ' ', n)
    n = _re.sub(r'[^a-z0-9 ]', ' ', n)
    return _re.sub(r'\s+', ' ', n).strip()


def check_nzsa(company_name, website=None):
    """Check if a company is listed as a NZSA member.
    Returns dict with keys:
        member (bool), member_name (str or None), accredited (bool), grade (str or None)
    """
    import re as _re

    members = _get_nzsa_members()
    if not members:
        return {"member": False, "member_name": None, "accredited": False, "grade": None}

    def _member_hit(m):
        return {
            "member": True,
            "member_name": m["name"],
            "accredited": m["accredited"],
            "grade": m["grade"],
            "contact_name": m.get("contact_name", ""),
            "phone": m.get("phone", ""),
            "email": m.get("email", ""),
            "overview": m.get("overview", ""),
        }

    query_norm = _normalise_company_name(company_name)
    query_words = set(query_norm.split())

    # Generic words that alone don't identify a company
    _GENERIC = {"security", "services", "solutions", "systems", "alarm", "alarms",
                "group", "install", "camera", "cctv", "surveillance", "protection",
                "management", "response", "patrol", "guard", "monitoring"}

    sig_words = query_words - _GENERIC

    best_score = 0
    best_member = None

    for m in members:
        m_norm = _normalise_company_name(m["name"])
        m_words = set(m_norm.split())

        # Exact normalised match
        if query_norm == m_norm:
            return _member_hit(m)

        # Website match — most reliable
        if website and m["website"]:
            q_dom = _re.sub(r'^https?://(www\.)?', '', website).rstrip("/").lower()
            m_dom = _re.sub(r'^https?://(www\.)?', '', m["website"]).rstrip("/").lower()
            if q_dom and m_dom and q_dom == m_dom:
                return _member_hit(m)

        # Word overlap scoring — weight significant words higher
        common = query_words & m_words
        sig_common = sig_words & m_words
        if not sig_common and not common:
            continue

        # Score: significant word hits * 3, generic word hits * 1
        # Penalise if many words don't match
        score = len(sig_common) * 3 + len(common - sig_common)
        # Require at least 1 significant word match
        if sig_common:
            best_score = max(best_score, score)
            if score >= best_score:
                best_score = score
                best_member = m

    # Require score >= 3 (at least one significant word match)
    if best_member and best_score >= 3:
        return _member_hit(best_member)

    return {"member": False, "member_name": None, "accredited": False, "grade": None,
            "contact_name": None, "phone": None, "email": None, "overview": None}


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
    "companies_office_number": None,
    "nzbn": None,
    "individual_license": None,
    "director_name": None,
    "facebook_url": None,
    "linkedin_url": None,
    "nzsa_member": None,
    "nzsa_member_name": None,
    "nzsa_accredited": None,
    "nzsa_grade": None,
    "nzsa_contact_name": None,
    "nzsa_phone": None,
    "nzsa_email": None,
    "nzsa_overview": None,
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
    page_text = info.get("_page_text", "")
    extra_context = {
        "facebook_snippet": info.get("_fb_snippet", ""),
        "linkedin_url": info.get("linkedin_url", ""),
        "nzsa_data": info.get("_nzsa_data"),
    }
    pspla_result = None
    for name in names_to_try:
        print(f"  [Checking PSPLA] {name}")
        res = check_pspla(name, website_region=website_region, page_text=page_text,
                          directors=info.get("director_names"), extra_context=extra_context)
        if res.get("matched_name"):
            matched = res["matched_name"]
            verification = verify_pspla_match(
                company_name, matched, website_region, res.get("pspla_address"),
                _audit_name=company_name
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

    # If CO found the exact legal name and PSPLA isn't licensed yet, retry PSPLA with the CO name.
    # This resolves cases where the trading name is too generic (e.g. "Coast Security" →
    # CO returns "KAPITI COAST SECURITY LIMITED" → exact PSPLA hit).
    co_registered_name = co_result.get("name")
    if co_registered_name and not pspla_result.get("licensed") and co_registered_name not in names_to_try:
        print(f"  [Checking PSPLA with CO name] {co_registered_name}")
        co_pspla_res = check_pspla(co_registered_name, website_region=website_region,
                                   page_text=page_text, co_result=co_result,
                                   directors=co_result.get("directors") or info.get("director_names") or [],
                                   extra_context=extra_context)
        # Only replace if CO search found an active licence, or original had no match at all.
        # Don't replace a known-expired result or we'll skip the individual licence check.
        if co_pspla_res.get("matched_name") and (co_pspla_res.get("licensed") or not pspla_result.get("matched_name")):
            pspla_result = co_pspla_res
            names_to_try.append(co_registered_name)

    # If the CO company is removed/deregistered and CO found an active successor, try the
    # successor name on PSPLA.  This catches sold businesses that re-registered under a new name
    # (e.g. "Tarnix Security Limited" removed → "Tarnix Limited" registered → Active PSPLA licence).
    co_successor_name = co_result.get("successor_name")
    if co_successor_name and not pspla_result.get("licensed") and co_successor_name not in names_to_try:
        print(f"  [CO company removed — trying successor] {co_successor_name}")
        successor_co = {
            "name": co_successor_name,
            "address": co_result.get("successor_address"),
            "company_number": co_result.get("successor_number"),
            "directors": co_result.get("directors") or [],
        }
        succ_pspla_res = check_pspla(co_successor_name, website_region=website_region,
                                     page_text=page_text, co_result=successor_co,
                                     directors=co_result.get("directors") or info.get("director_names") or [],
                                     extra_context=extra_context)
        if succ_pspla_res.get("matched_name") and (succ_pspla_res.get("licensed") or not pspla_result.get("matched_name")):
            print(f"  [Successor match] {co_successor_name} -> PSPLA: {succ_pspla_res.get('matched_name')}")
            pspla_result = succ_pspla_res
            names_to_try.append(co_successor_name)
            # Update CO result to reflect the active successor company
            co_result = {**co_result, **successor_co}

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

    if pspla_result.get("licensed"):
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
        if pspla_result.get("matched_name"):
            _co_status = (pspla_result.get("pspla_license_status") or "inactive").upper()
            reason_parts.append(f"Company license found for '{pspla_result.get('matched_name')}' but status is {_co_status} (match method: {pspla_result.get('match_method')}).")
            if pspla_result.get("pspla_license_number"):
                reason_parts.append(f"License #{pspla_result.get('pspla_license_number')}, status: {_co_status}, expires {pspla_result.get('pspla_license_expiry') or 'unknown'}.")
        else:
            reason_parts.append("No company license found on PSPLA.")
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

    # NZSA membership check
    print(f"  [Checking NZSA] {company_name}")
    nzsa_result = check_nzsa(company_name, website=website_url)
    if nzsa_result["member"]:
        print(f"  [NZSA] Member: {nzsa_result['member_name']}" + (" (Accredited)" if nzsa_result["accredited"] else ""))

    # Cross-check all sources when we have a PSPLA match and multiple data sources
    if pspla_result.get("matched_name") and (nzsa_result.get("member") or co_result.get("name") or extra_context.get("facebook_snippet")):
        print(f"  [Cross-check] Verifying source consistency for: {company_name}")
        cross_check = _llm_cross_check_sources(
            company_name, website_region, pspla_result, co_result, nzsa_result, extra_context
        )
        if not cross_check.get("consistent"):
            issues = cross_check.get("issues", [])
            print(f"  [Cross-check WARNING] Inconsistency detected: {issues}")
            write_audit("llm_decision", None, company_name,
                        changes=f"Source cross-check found inconsistency: {'; '.join(issues)}",
                        triggered_by="cross_check",
                        notes=cross_check.get("notes", ""))
            reason_parts.insert(0, f"[WARNING] Source inconsistency: {cross_check.get('notes', '')}.")
        else:
            print(f"  [Cross-check OK] Sources consistent ({cross_check.get('confidence','?')} confidence): {cross_check.get('notes', '')}")

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
        "companies_office_number": co_result.get("company_number"),
        "nzbn": co_result.get("nzbn"),
        "individual_license": individual_license_found,
        "director_name": ", ".join(directors),
        "facebook_url": info.get("facebook_url"),
        "linkedin_url": info.get("linkedin_url"),
        "nzsa_member": "true" if nzsa_result["member"] else "false",
        "nzsa_member_name": nzsa_result["member_name"],
        "nzsa_accredited": "true" if nzsa_result["accredited"] else "false",
        "nzsa_grade": nzsa_result["grade"],
        "nzsa_contact_name": nzsa_result.get("contact_name") or None,
        "nzsa_phone": nzsa_result.get("phone") or None,
        "nzsa_email": nzsa_result.get("email") or None,
        "nzsa_overview": nzsa_result.get("overview") or None,
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
        _session_new_companies.append({
            "company_name": company_name,
            "licensed": licensed_val,
            "region": website_region or fallback_region,
            "website": website_url,
            "email": info.get("email", ""),
        })
        write_audit("added", None, company_name,
                    changes=f"Source: {source_label} | Status: {status}",
                    triggered_by=source_label,
                    notes=website_url)
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
        page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(website_url)
        time.sleep(1)

        info = extract_company_info(website_url, page_text, result["snippet"])
        if not info or not info.get("company_name"):
            info = extract_from_fb_snippet(result["title"], result["snippet"], fb_url_norm, fallback_region)

        if not info or not info.get("company_name"):
            print("  [Skipped] Could not extract company name")
            return fb_total, fb_new

        info["_page_text"] = page_text

        if not info.get("email") and scraped_email:
            info["email"] = scraped_email
        if scraped_facebook:
            info["facebook_url"] = scraped_facebook
        if scraped_linkedin:
            info["linkedin_url"] = scraped_linkedin
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

            results = google_search(query, num_results=50)
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

            results = google_search(query, num_results=50)
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


def linkedin_url_exists(li_url):
    """Return True if a record with this linkedin_url already exists in the DB."""
    url = f"{SUPABASE_URL}/rest/v1/Companies?linkedin_url=eq.{requests.utils.quote(li_url)}&select=id&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(url, headers=headers)
        return bool(r.json())
    except:
        return False


_LINKEDIN_IMPORT_QUERIES = [
    'site:linkedin.com/company "security camera" "New Zealand"',
    'site:linkedin.com/company "CCTV installation" "New Zealand"',
    'site:linkedin.com/company "alarm installation" "New Zealand"',
    'site:linkedin.com/company "security systems" "New Zealand"',
    'site:linkedin.com/company "security alarm" "New Zealand"',
    'site:linkedin.com/company "CCTV" "New Zealand" security',
]


def write_audit(action, company_id, company_name, changes="", triggered_by="manual", notes=""):
    """Write an audit log entry to the AuditLog table in Supabase."""
    try:
        url = f"{SUPABASE_URL}/rest/v1/AuditLog"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "company_id": str(company_id) if company_id else None,
            "company_name": company_name,
            "changes": changes,
            "triggered_by": triggered_by,
            "notes": notes,
        }
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"  [Audit log error] {e}")


def send_search_email(search_type, started_iso, total_found, total_new, triggered_by, new_companies=None):
    """Send a notification email summarising the completed search run."""
    if not NOTIFY_EMAIL or not SMTP_USER or not SMTP_PASS or not SMTP_HOST:
        return  # email not configured
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    type_labels = {
        "full": "Full Search",
        "google-weekly": "Weekly Scan",
        "google-partial": "Partial Search",
        "facebook": "Facebook Search",
        "directories": "Directory Import (NZSA + LinkedIn)",
    }
    label = type_labels.get(search_type, search_type.title())

    try:
        started_dt = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        duration_mins = round((datetime.now(timezone.utc) - started_dt).total_seconds() / 60)
        duration_str = f"{duration_mins} min"
    except Exception:
        duration_str = "unknown"

    subject = f"PSPLA {label} complete -- {total_new} new companies added"

    # Build plain text body
    lines = [
        f"Search type:     {label}",
        f"Triggered by:    {triggered_by}",
        f"Duration:        {duration_str}",
        f"URLs/pages found: {total_found}",
        f"New companies:   {total_new}",
        "",
    ]

    if new_companies:
        lines.append(f"New companies added ({len(new_companies)}):")
        lines.append("-" * 50)
        for c in new_companies:
            status = "LICENSED" if c.get("licensed") else ("NOT LICENSED" if c.get("licensed") is False else "UNKNOWN")
            lines.append(f"  {c['company_name']}  [{status}]  {c.get('region','')}")
            if c.get("website"):
                lines.append(f"    Website: {c['website']}")
            if c.get("email"):
                lines.append(f"    Email:   {c['email']}")
    else:
        lines.append("No new companies were added in this run.")

    body_text = "\n".join(lines)

    # Build HTML body
    rows_html = ""
    if new_companies:
        for c in new_companies:
            status = "LICENSED" if c.get("licensed") else ("NOT LICENSED" if c.get("licensed") is False else "UNKNOWN")
            color = "#27ae60" if c.get("licensed") else "#e74c3c" if c.get("licensed") is False else "#888"
            ws = f'<a href="{c["website"]}">{c["website"]}</a>' if c.get("website") else ""
            rows_html += (
                f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{c['company_name']}</td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;color:{color}'><b>{status}</b></td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;color:#666'>{c.get('region','')}</td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{ws}</td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;color:#555'>{c.get('email','')}</td></tr>"
            )
        table_html = (
            "<table style='border-collapse:collapse;font-size:13px;width:100%'>"
            "<tr style='background:#f5f5f5'>"
            "<th style='padding:4px 8px;text-align:left'>Company</th>"
            "<th style='padding:4px 8px;text-align:left'>Status</th>"
            "<th style='padding:4px 8px;text-align:left'>Region</th>"
            "<th style='padding:4px 8px;text-align:left'>Website</th>"
            "<th style='padding:4px 8px;text-align:left'>Email</th>"
            "</tr>" + rows_html + "</table>"
        )
    else:
        table_html = "<p style='color:#888'>No new companies were added in this run.</p>"

    html = f"""<html><body style='font-family:sans-serif;color:#333'>
<h2 style='color:#2c3e50'>PSPLA {label} Complete</h2>
<table style='margin-bottom:16px'>
<tr><td style='padding:2px 12px 2px 0;color:#666'>Triggered by</td><td>{triggered_by}</td></tr>
<tr><td style='padding:2px 12px 2px 0;color:#666'>Duration</td><td>{duration_str}</td></tr>
<tr><td style='padding:2px 12px 2px 0;color:#666'>URLs/pages found</td><td>{total_found}</td></tr>
<tr><td style='padding:2px 12px 2px 0;color:#666'>New companies added</td><td><b>{total_new}</b></td></tr>
</table>
<h3 style='color:#2c3e50'>New Companies ({len(new_companies) if new_companies else 0})</h3>
{table_html}
</body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"  [Email sent] {subject}")
        write_audit("email", None, f"Notification sent",
                    changes=subject,
                    triggered_by=triggered_by,
                    notes=f"To: {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"  [Email error] {e}")


def apply_correction_and_recheck(company_id, company_name, old_pspla_name, website_region=None):
    """Re-run PSPLA check after a correction, update DB, generate lesson.
    Returns dict with keys: new_result, lesson, changed (bool), summary."""
    print(f"  [Auto-recheck] Running PSPLA for '{company_name}' after correction")

    new_result = check_pspla(company_name, website_region=website_region)

    # Generate and save the lesson from this mistake
    lesson = _generate_and_save_lesson(company_name, old_pspla_name, new_result)

    # Patch the DB record with new PSPLA result
    new_licensed = new_result.get("licensed")
    new_name = new_result.get("matched_name")
    patch = {
        "pspla_licensed": new_licensed,
        "pspla_name": new_name,
        "pspla_address": new_result.get("pspla_address"),
        "pspla_license_number": new_result.get("pspla_license_number"),
        "pspla_license_status": new_result.get("pspla_license_status"),
        "pspla_license_expiry": new_result.get("pspla_license_expiry"),
        "license_type": new_result.get("license_type"),
        "match_method": new_result.get("match_method"),
    }
    patch = {k: v for k, v in patch.items() if v is not None}
    patch["pspla_licensed"] = new_licensed  # always include even if False

    try:
        url = f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        requests.patch(url, headers=headers, json=patch, timeout=15)
        print(f"  [Auto-recheck] DB updated: licensed={new_licensed}, name={new_name}")
    except Exception as e:
        print(f"  [Auto-recheck DB error] {e}")

    changed = new_name != old_pspla_name or new_licensed is not None

    if new_name and new_name != old_pspla_name:
        summary = f"Found new match: {new_name} (licensed={new_licensed})"
    elif new_licensed is False and not new_name:
        summary = "No PSPLA match found — marked as Not Licensed"
    else:
        summary = f"Result unchanged: {new_name}"

    write_audit("updated", company_id, company_name,
                changes=f"Auto-recheck after correction: {summary}. Lesson: {lesson.get('rule_to_apply','')[:100]}",
                triggered_by="auto-recheck (correction)")

    return {
        "new_result": new_result,
        "lesson": lesson,
        "changed": changed,
        "summary": summary,
    }


def run_nzsa_import(found_urls=None, limit=None):
    """Import all NZSA members not already in the database.
    limit: stop after processing this many new candidates (for testing).
    Returns (total_found, total_new)."""
    if found_urls is None:
        found_urls = set()

    print("\n" + "=" * 60)
    print("  NZSA Directory Import")
    print("=" * 60)

    members = _get_nzsa_members()
    print(f"  {len(members)} NZSA members loaded")

    total_found = 0
    total_new = 0
    processed = 0

    for idx, m in enumerate(members):
        check_pause()
        if limit is not None and processed >= limit:
            print(f"  [Limit reached] Stopping at {limit} processed")
            break

        name = m.get("name", "").strip()
        website = m.get("website", "").strip()

        write_status("nzsa-import", f"member {idx+1}/{len(members)}", name,
                     idx + 1, 1, len(members), 1, total_found, total_new)

        # --- Member has a website ---
        if website and website.startswith("http"):
            root_domain = get_root_domain(website)
            if root_domain in found_urls or get_domain_record(root_domain):
                print(f"  [Already in DB] {name} ({root_domain})")
                continue
            if company_name_exists(name):
                print(f"  [Already in DB by name] {name}")
                found_urls.add(root_domain)
                continue
            found_urls.add(root_domain)
            total_found += 1
            processed += 1

            print(f"  [NZSA] {name} -> {website}")
            page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(website)
            time.sleep(1)

            info = extract_company_info(website, page_text, name)
            if not info:
                info = {}
            info["_page_text"] = page_text
            info["_nzsa_data"] = m  # Pass NZSA member record as LLM context
            if not info.get("company_name"):
                info["company_name"] = name
            if not info.get("region"):
                locs = m.get("locations") or []
                if locs:
                    info["region"] = locs[0]
            if not info.get("email") and scraped_email:
                info["email"] = scraped_email
            if not info.get("email") and m.get("email"):
                info["email"] = m["email"]
            if scraped_facebook:
                info["facebook_url"] = scraped_facebook
            if scraped_linkedin:
                info["linkedin_url"] = scraped_linkedin
            if not info.get("email"):
                found_email = find_email_via_google(root_domain)
                if found_email:
                    info["email"] = found_email

            fb_url = find_facebook_url(info["company_name"], page_text)
            if fb_url:
                info["facebook_url"] = fb_url
            li_url = scraped_linkedin or find_linkedin_url(info["company_name"], page_text)
            if li_url:
                info["linkedin_url"] = li_url

            region = info.get("region") or (m.get("locations") or [""])[0]
            if process_and_save_company(info, website, root_domain, "NZSA directory", region):
                total_new += 1

        else:
            # --- No website: search Google ---
            if company_name_exists(name):
                print(f"  [Already in DB by name] {name}")
                continue

            processed += 1
            print(f"  [NZSA, no website] {name} — searching Google...")
            query = f'"{name}" security New Zealand'
            results = google_search(query, num_results=5)
            time.sleep(1)

            if results is SERPAPI_EXHAUSTED:
                print("  [STOPPED] SerpAPI exhausted during NZSA import.")
                return total_found, total_new

            if not results:
                continue

            for result in results:
                url = result["link"]
                if any(domain in url for domain in SKIP_DOMAINS):
                    continue
                if is_directory_listing_url(url):
                    continue
                if "facebook.com" in url or "linkedin.com" in url:
                    continue
                root_domain = get_root_domain(url)
                if root_domain in found_urls or get_domain_record(root_domain):
                    continue
                found_urls.add(root_domain)
                total_found += 1

                page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(url)
                time.sleep(1)

                info = extract_company_info(url, page_text, result.get("snippet", ""))
                if not info:
                    info = {}
                info["_page_text"] = page_text
                if not info.get("company_name"):
                    info["company_name"] = name
                if not info.get("email") and scraped_email:
                    info["email"] = scraped_email
                if not info.get("email") and m.get("email"):
                    info["email"] = m["email"]
                if scraped_facebook:
                    info["facebook_url"] = scraped_facebook
                if scraped_linkedin:
                    info["linkedin_url"] = scraped_linkedin
                if not info.get("email"):
                    found_email = find_email_via_google(root_domain)
                    if found_email:
                        info["email"] = found_email

                fb_url = find_facebook_url(info["company_name"], page_text)
                if fb_url:
                    info["facebook_url"] = fb_url
                li_url = scraped_linkedin or find_linkedin_url(info["company_name"], page_text)
                if li_url:
                    info["linkedin_url"] = li_url

                region = info.get("region") or (m.get("locations") or [""])[0]
                if process_and_save_company(info, url, root_domain, "NZSA directory", region):
                    total_new += 1
                break  # only process first good result per member

    print(f"\n  NZSA import complete. Found: {total_found}, New: {total_new}")
    return total_found, total_new


def run_linkedin_import(found_urls=None, limit=None):
    """Search LinkedIn via Google for NZ security companies not already in the database.
    limit: stop after processing this many new candidates (for testing).
    Returns (total_found, total_new)."""
    import re as _lire
    if found_urls is None:
        found_urls = set()

    print("\n" + "=" * 60)
    print("  LinkedIn Directory Import")
    print("=" * 60)

    total_found = 0
    total_new = 0
    seen_li_urls = set()
    processed = 0

    for q_idx, query in enumerate(_LINKEDIN_IMPORT_QUERIES):
        check_pause()
        print(f"\n  Query: {query}")
        write_status("linkedin-import", f"query {q_idx+1}/{len(_LINKEDIN_IMPORT_QUERIES)}", query,
                     q_idx + 1, 1, len(_LINKEDIN_IMPORT_QUERIES), 1, total_found, total_new)

        results = google_search(query, num_results=50)
        time.sleep(1)

        if results is SERPAPI_EXHAUSTED:
            print("  [STOPPED] SerpAPI exhausted during LinkedIn import.")
            return total_found, total_new

        if not results:
            continue

        for result in results:
            check_pause()
            if limit is not None and processed >= limit:
                print(f"  [Limit reached] Stopping at {limit} processed")
                return total_found, total_new

            li_url = result["link"]
            if "linkedin.com/company/" not in li_url:
                continue

            # Normalise to www subdomain, strip query params
            li_norm = _lire.sub(r'https?://(?:[a-z]{2}\.)?linkedin\.com/company/',
                                'https://www.linkedin.com/company/', li_url)
            li_norm = li_norm.split("?")[0].rstrip("/")

            if li_norm in seen_li_urls:
                continue
            seen_li_urls.add(li_norm)

            if linkedin_url_exists(li_norm):
                print(f"  [Already in DB] {li_norm}")
                continue

            # Derive a guess at the company name from the URL slug
            slug = li_norm.split("/company/")[-1]
            company_name_guess = slug.replace("-", " ").title()

            if company_name_exists(company_name_guess):
                print(f"  [Already in DB by name] {company_name_guess}")
                continue

            processed += 1
            print(f"  [LinkedIn] {li_norm} -> {company_name_guess}")

            # Find their real website via Google
            site_query = f'"{company_name_guess}" security New Zealand -site:linkedin.com'
            website_results = google_search(site_query, num_results=5)
            time.sleep(1)

            if website_results is SERPAPI_EXHAUSTED:
                print("  [STOPPED] SerpAPI exhausted during LinkedIn import.")
                return total_found, total_new

            website_url = None
            for wr in (website_results or []):
                wu = wr["link"]
                if any(d in wu for d in SKIP_DOMAINS):
                    continue
                if is_directory_listing_url(wu):
                    continue
                if "linkedin.com" in wu or "facebook.com" in wu:
                    continue
                website_url = wu
                break

            if not website_url:
                print(f"  [No website found] Skipping {company_name_guess}")
                continue

            root_domain = get_root_domain(website_url)
            if root_domain in found_urls or get_domain_record(root_domain):
                print(f"  [Domain already in DB] {root_domain}")
                continue
            found_urls.add(root_domain)
            total_found += 1

            page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(website_url)
            time.sleep(1)

            info = extract_company_info(website_url, page_text, result.get("snippet", ""))
            if not info or not info.get("company_name"):
                info = {"company_name": company_name_guess}

            info["_page_text"] = page_text
            info["linkedin_url"] = li_norm
            if not info.get("email") and scraped_email:
                info["email"] = scraped_email
            if scraped_facebook:
                info["facebook_url"] = scraped_facebook
            if not info.get("email"):
                found_email = find_email_via_google(root_domain)
                if found_email:
                    info["email"] = found_email

            fb_url = find_facebook_url(info["company_name"], page_text)
            if fb_url:
                info["facebook_url"] = fb_url

            region = info.get("region", "")
            if process_and_save_company(info, website_url, root_domain, "LinkedIn directory", region):
                total_new += 1

    print(f"\n  LinkedIn import complete. Found: {total_found}, New: {total_new}")
    return total_found, total_new


def run_search(triggered_by="manual"):
    print("=" * 60)
    print("  PSPLA Security Camera Company Checker")
    print("=" * 60)
    started_iso = datetime.now(timezone.utc).isoformat()
    reset_session_log()

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

                    if is_directory_listing_url(url):
                        print(f"  [Skipped] Directory/listing page: {url}")
                        continue

                    if company_exists(url):
                        print(f"  [Already in DB] {url}")
                        continue

                    # Check if we already have this domain
                    root_domain = get_root_domain(url)
                    existing = get_domain_record(root_domain)
                    if existing:
                        print(f"  [Domain already in DB] {root_domain} ({existing.get('company_name')}) — skipping")
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

                    # Use mailto: email from HTML if Claude didn't find one in the text
                    if not info.get("email") and scraped_email:
                        info["email"] = scraped_email
                        print(f"  [Email from mailto] {scraped_email}")

                    print(f"  [Company] {info['company_name']}")

                    # Find Facebook page via Google search now that we know the name
                    fb_url = find_facebook_url(info["company_name"], page_text)
                    if fb_url:
                        info["facebook_url"] = fb_url
                        print(f"  [Facebook] {fb_url}")

                    # Find LinkedIn company page
                    li_url = scraped_linkedin or find_linkedin_url(info["company_name"], page_text)
                    if li_url:
                        info["linkedin_url"] = li_url
                        print(f"  [LinkedIn] {li_url}")

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

        # NZSA directory import
        nzsa_found, nzsa_new = run_nzsa_import(found_urls)
        total_found += nzsa_found
        total_new += nzsa_new

        # LinkedIn directory import
        li_found, li_new = run_linkedin_import(found_urls)
        total_found += li_found
        total_new += li_new

        clear_progress()
        append_history("full", started_iso, total_found, total_new, "completed", triggered_by)
        send_search_email("full", started_iso, total_found, total_new, triggered_by, get_session_log())

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
