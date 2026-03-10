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
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# LLM debug log — captures every prompt and response for inspection
LLM_LOG_FILE = os.path.join(BASE_DIR, "llm_debug.log")

def _llm_log(fn_name, prompt, response_text):
    """Append an LLM call prompt and response to llm_debug.log."""
    try:
        with open(LLM_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] FUNCTION: {fn_name}\n")
            f.write(f"{'─'*40} PROMPT {'─'*40}\n")
            f.write(prompt.strip() + "\n")
            f.write(f"{'─'*40} RESPONSE {'─'*39}\n")
            f.write(response_text.strip() + "\n")
    except Exception:
        pass


class _LoggingAnthropicClient:
    """Thin wrapper around the Anthropic client that logs every messages.create call."""
    def __init__(self, real_client):
        self._client = real_client
        self.messages = self

    def create(self, model, max_tokens, messages, **kwargs):
        # Extract the user prompt text for logging
        prompt_text = " | ".join(
            m.get("content", "") if isinstance(m.get("content"), str)
            else str(m.get("content", ""))
            for m in messages if m.get("role") == "user"
        )
        response = self._client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages, **kwargs
        )
        raw = response.content[0].text if response.content else ""
        # Accumulate token usage
        if hasattr(response, "usage") and response.usage:
            _accumulate_tokens(model, response.usage.input_tokens, response.usage.output_tokens)
        # Determine caller function name from the call stack
        import traceback
        stack = traceback.extract_stack()
        fn_name = next(
            (f.name for f in reversed(stack[:-1])
             if f.name not in ("create", "_llm_log", "<module>") and not f.name.startswith("_LoggingAnthropicClient")),
            "unknown"
        )
        _llm_log(fn_name, prompt_text, raw)
        return response


client = _LoggingAnthropicClient(client)

# Token usage tracking — written to file so dashboard process can read it
TOKEN_USAGE_FILE = os.path.join(BASE_DIR, "token_usage.json")

# Approximate cost per million tokens (USD) — update if Anthropic changes pricing
_TOKEN_COST = {
    "haiku":  {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus":   {"input": 15.00, "output": 75.00},
}


def _accumulate_tokens(model: str, input_tokens: int, output_tokens: int):
    """Add tokens to the persistent usage file so dashboard can read it cross-process."""
    try:
        try:
            with open(TOKEN_USAGE_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {"input": 0, "output": 0, "by_model": {}}
        data["input"]  = data.get("input", 0)  + input_tokens
        data["output"] = data.get("output", 0) + output_tokens
        key = "haiku" if "haiku" in model.lower() else "sonnet" if "sonnet" in model.lower() else "opus"
        if key not in data["by_model"]:
            data["by_model"][key] = {"input": 0, "output": 0}
        data["by_model"][key]["input"]  += input_tokens
        data["by_model"][key]["output"] += output_tokens
        with open(TOKEN_USAGE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def reset_token_usage():
    """Reset token counters — clears the file at the start of each search run."""
    try:
        with open(TOKEN_USAGE_FILE, "w") as f:
            json.dump({"input": 0, "output": 0, "by_model": {}}, f)
    except Exception:
        pass


def get_token_usage():
    """Return token totals and estimated USD cost — reads from file, works cross-process."""
    try:
        with open(TOKEN_USAGE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {"input": 0, "output": 0, "by_model": {}}
    cost = 0.0
    for model_key, counts in data.get("by_model", {}).items():
        rates = _TOKEN_COST.get(model_key, _TOKEN_COST["haiku"])
        cost += counts["input"]  / 1_000_000 * rates["input"]
        cost += counts["output"] / 1_000_000 * rates["output"]
    data["estimated_cost_usd"] = round(cost, 4)
    return data

# LLM health tracking — counts consecutive API failures across all LLM functions
_llm_consecutive_errors = 0
_LLM_ERROR_THRESHOLD = 3  # write an audit warning after this many consecutive failures


def _llm_error(fn_name, error):
    """Record an LLM API failure and warn via audit log when threshold is hit."""
    global _llm_consecutive_errors
    _llm_consecutive_errors += 1
    print(f"  [LLM unavailable] {fn_name}: {error}")
    if _llm_consecutive_errors == _LLM_ERROR_THRESHOLD:
        try:
            write_audit("llm_error", None, "SYSTEM",
                        changes=f"LLM API unavailable after {_llm_consecutive_errors} consecutive failures "
                                f"in {fn_name}: {error}. Matches requiring verification will be "
                                f"saved as low-confidence and flagged for review.",
                        triggered_by="system", notes="Check Anthropic API key / credit balance")
        except Exception:
            pass


def _llm_ok():
    """Reset the consecutive error counter after a successful LLM call."""
    global _llm_consecutive_errors
    _llm_consecutive_errors = 0


def get_llm_status():
    """Return current LLM health status for dashboard display."""
    return _llm_consecutive_errors


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
            err_lower = error_msg.lower()
            # "no results" is normal for narrow queries — don't log it as an error
            no_results = "no results" in err_lower or "hasn't returned any results" in err_lower
            if not no_results:
                print(f"  [SerpAPI error] {error_msg}")
            # Real quota exhaustion — search must stop
            if "run out" in err_lower or "out of searches" in err_lower or ("plan" in err_lower and "search" in err_lower):
                return SERPAPI_EXHAUSTED
            # Temporary rate limiting — back off and return empty (search continues)
            if "rate" in err_lower or "too many" in err_lower or "throttl" in err_lower:
                print("  [SerpAPI rate limit] Backing off 30s...")
                time.sleep(30)
        return results
    except Exception as e:
        print(f"  [Search error] {e}")
        return []


def get_google_business_profile(company_name, region=""):
    """Fetch Google Business Profile data via SerpAPI.

    SerpAPI returns knowledge_graph and local_results alongside organic results
    at no extra query cost. We run one targeted search and parse all three sections.

    Email: Google rarely exposes it in the knowledge panel, but we scan:
      1. knowledge_graph.email (set by business in their Google profile)
      2. local_results[].email
      3. Organic result snippets from this same search (no extra query)
    We reject any email from google.com/facebook.com/sentry.io domains.

    Returns dict: rating, reviews, phone, address, email  (all may be None)
    """
    import re as _re

    result = {"rating": None, "reviews": None, "phone": None, "address": None, "email": None}

    _EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    _BAD_EMAIL_DOMAINS = {"google", "facebook", "sentry", "example", "wix", "squarespace",
                          "wordpress", "godaddy", "namecheap", "cloudflare"}

    def _clean_email(raw):
        raw = raw.strip().lower().rstrip(".")
        domain_part = raw.split("@")[-1].split(".")[0]
        if domain_part in _BAD_EMAIL_DOMAINS:
            return None
        return raw

    region_suffix = (f" {region}" if region else "") + " New Zealand"

    # Some companies are registered as "Foo Limited" but trade as "Foo" — Google
    # indexes the trading name, so a search for "Foo Limited" won't trigger the
    # local panel. Build an extra query with legal suffixes stripped.
    import re as _re2
    _LEGAL_SUFFIXES = _re2.compile(
        r'\b(limited|ltd\.?|holdings|group|nz|new zealand|incorporated|inc\.?|pty)\b',
        _re2.IGNORECASE,
    )
    short_name = _LEGAL_SUFFIXES.sub('', company_name).strip().strip(',').strip()

    queries_to_try = [
        f'"{company_name}"{region_suffix}',
        f'{company_name}{region_suffix}',
    ]
    # If stripping the suffix produces a meaningfully different name, add it as
    # a third attempt (no extra SerpAPI cost unless the first two already succeeded)
    if short_name and short_name.lower() != company_name.lower():
        queries_to_try.append(f'{short_name}{region_suffix}')

    data = {}
    for query in queries_to_try:
        try:
            response = requests.get(
                "https://serpapi.com/search",
                params={
                    "api_key": SERPAPI_KEY,
                    "engine": "google",
                    "q": query,
                    "num": 5,
                    "gl": "nz",
                    "hl": "en",
                },
                timeout=15,
            )
            data = response.json()
        except Exception as e:
            print(f"  [Google profile error] {e}")
            return result

        if "error" in data:
            return result

        # If we got a knowledge panel or local results, use this response
        if data.get("knowledge_graph") or data.get("local_results"):
            print(f"  [Google profile] Found panel/local results with query: {query!r}")
            break

    # ── AI-suggested name variants (if still no panel) ────────────────────────
    if not data.get("knowledge_graph") and not data.get("local_results"):
        try:
            ai_prompt = (
                f'A Google Business Profile search for NZ company "{company_name}"'
                f'{(" in " + region) if region else ""} found no knowledge panel or local results.\n'
                f'Suggest up to 3 alternative trading names or abbreviations this business '
                f'might be listed under on Google Maps (e.g. without legal suffix, shortened, '
                f'or common trading name).\n'
                f'Return ONLY a JSON array of strings. Do not repeat: {queries_to_try}'
            )
            ai_msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=120,
                messages=[{"role": "user", "content": ai_prompt}]
            )
            raw_ai = ai_msg.content[0].text.strip()
            if "```" in raw_ai:
                raw_ai = raw_ai.split("```")[1]
                if raw_ai.startswith("json"):
                    raw_ai = raw_ai[4:]
            ai_terms = json.loads(raw_ai.strip())
            if isinstance(ai_terms, list):
                for ai_term in ai_terms[:3]:
                    if not isinstance(ai_term, str):
                        continue
                    print(f"  [Google profile] AI suggests: {ai_term!r}")
                    try:
                        ai_resp = requests.get(
                            "https://serpapi.com/search",
                            params={"api_key": SERPAPI_KEY, "engine": "google",
                                    "q": f'{ai_term}{region_suffix}',
                                    "num": 5, "gl": "nz", "hl": "en"},
                            timeout=15,
                        )
                        ai_data = ai_resp.json()
                        if ai_data.get("knowledge_graph") or ai_data.get("local_results"):
                            print(f"  [Google profile] Found panel via AI term: {ai_term!r}")
                            data = ai_data
                            break
                    except Exception:
                        pass
        except Exception as _ai_e:
            print(f"  [Google profile] AI fallback error: {_ai_e}")

    # If neither query produced a panel, data holds the last response (still useful
    # for organic snippet email scanning)

    # ── Knowledge Graph ───────────────────────────────────────────────────────
    kg = data.get("knowledge_graph", {})
    if kg:
        kg_title = kg.get("title", "")
        kg_keys = [k for k in kg.keys() if not k.startswith("@")]
        print(f"  [Google profile] KG title={kg_title!r} keys={kg_keys}")

        # Rating — try multiple field names SerpAPI uses
        for _rating_key in ("rating",):
            if kg.get(_rating_key) is not None:
                result["rating"] = str(kg[_rating_key])
                break

        # Reviews — SerpAPI may use several field names.
        # "review_count" is the integer; "reviews" is often a URL — try integer keys first.
        # Sanity cap: reject any digit string > 6 chars (no real business has 1M+ reviews
        # — it means we stripped digits out of a URL).
        for _rev_key in ("review_count", "reviews_count", "total_reviews", "reviews"):
            if kg.get(_rev_key) is not None:
                rev = kg[_rev_key]
                if isinstance(rev, dict):
                    rev = rev.get("value") or rev.get("count") or rev.get("total") or ""
                digits = ''.join(c for c in str(rev) if c.isdigit())
                if digits and len(digits) <= 6:  # sanity cap — reject URL-derived garbage
                    result["reviews"] = digits
                    break
        # Also check reviews_from_the_web list (each entry has .source and .votes)
        if not result["reviews"]:
            rfw = kg.get("reviews_from_the_web", [])
            if isinstance(rfw, list) and rfw:
                for src in rfw:
                    votes = src.get("votes") or src.get("count") or src.get("reviews") or ""
                    digits = ''.join(c for c in str(votes) if c.isdigit())
                    if digits:
                        result["reviews"] = digits
                        break

        phone = kg.get("phone") or kg.get("main_phone")
        if phone:
            result["phone"] = str(phone)
        addr = kg.get("address")
        if addr:
            result["address"] = str(addr)
        # Email — sometimes present when business has set it in Google Business Profile
        email = kg.get("email")
        if email:
            result["email"] = _clean_email(str(email))

    # ── Local Results (map pack) ──────────────────────────────────────────────
    local_results = data.get("local_results", [])
    if isinstance(local_results, dict):
        local_results = local_results.get("places", [])

    if local_results:
        print(f"  [Google profile] Local results: {[p.get('title','?') for p in local_results[:3]]}")

    name_words = [w for w in company_name.lower().split() if len(w) >= 4
                  and w not in {"limited", "security", "services", "solutions"}]

    for place in local_results[:3]:
        place_title = (place.get("title") or "").lower()
        if name_words and not any(w in place_title for w in name_words):
            print(f"  [Google profile] Local result {place.get('title')!r} skipped (name mismatch, want {name_words})")
            continue  # wrong business

        if not result["rating"] and place.get("rating") is not None:
            result["rating"] = str(place["rating"])
        if not result["reviews"]:
            for _rev_key in ("reviews", "reviews_original", "review_count"):
                if place.get(_rev_key) is not None:
                    rev = place[_rev_key]
                    if isinstance(rev, dict):
                        rev = rev.get("value") or rev.get("count") or rev.get("total") or ""
                    digits = ''.join(c for c in str(rev) if c.isdigit())
                    if digits:
                        result["reviews"] = digits
                        break
        if not result["phone"] and place.get("phone"):
            result["phone"] = str(place["phone"])
        if not result["address"] and place.get("address"):
            result["address"] = str(place["address"])
        if not result["email"] and place.get("email"):
            result["email"] = _clean_email(str(place["email"]))
        break

    # ── Organic result snippets — free email scan from this same search ───────
    # Google Business websites sometimes show email in their meta description
    # which appears in the snippet. Only use if not already found above.
    if not result["email"]:
        for item in data.get("organic_results", [])[:5]:
            for text in (item.get("snippet", ""), item.get("title", "")):
                m = _EMAIL_RE.search(text)
                if m:
                    candidate = _clean_email(m.group(0))
                    if candidate:
                        result["email"] = candidate
                        break
            if result["email"]:
                break

    if any(v for v in result.values()):
        print(f"  [Google profile] rating={result['rating']} reviews={result['reviews']} "
              f"phone={result['phone']} email={result['email']}")

    return result


# Cache: fb_url -> Google snippet text (populated by find_facebook_url, consumed by scrape_facebook_page)
_FB_SNIPPET_CACHE: dict = {}
_LI_SNIPPET_CACHE: dict = {}

# Hard-reject Facebook results whose snippet/title clearly indicate a non-NZ entity.
# Checked before any enrichment or DB write to prevent overseas companies entering the DB.
_FB_OVERSEAS_SIGNALS = [
    "united states", "united states of america", " usa ", "u.s.a.",
    "us-based", "u.s. based", " u.s. ", "(usa)", "(u.s.)",
    "co.uk", ".co.uk", "united kingdom", ".com.au", "australia",
]


def _snippet_is_overseas(snippet: str, title: str = "") -> bool:
    """Return True if the snippet or title contain hard overseas signals."""
    text = (snippet + " " + title).lower()
    return any(sig in text for sig in _FB_OVERSEAS_SIGNALS)


def _parse_fb_snippet(snippet: str) -> dict:
    """Extract structured data from a Google search snippet for a Facebook business page.

    Google's snippet for FB pages typically looks like:
        "436 followers · Home Security Company · +64 6 349 0999 · 203 Guyton St..."
    or  "Rating: 4.8 · 15 reviews · Security Monitoring"

    Returns dict with keys matching scrape_facebook_page output (all may be None).
    """
    import re as _re
    data = {
        "followers": None, "phone": None, "email": None,
        "address": None, "category": None, "rating": None,
    }
    if not snippet:
        return data

    # Followers/likes — "1,234 followers", "1.2K followers", "723 likes"
    m = _re.search(r'([\d,]+(?:\.\d+)?[KkMm]?)\s+(?:followers|likes)', snippet, _re.I)
    if m:
        data["followers"] = m.group(1).replace(",", "")

    # NZ phone number — +64 xxx or 0x xxx xxxx
    m = _re.search(r'(\+64[\d\s\-().]{6,18}|\b0\d[\d\s\-]{7,12})', snippet)
    if m:
        candidate = m.group(1).strip().rstrip(".")
        if _re.search(r'\d{6,}', candidate.replace(" ", "").replace("-", "")):
            data["phone"] = candidate

    # Email
    m = _re.search(r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', snippet)
    if m:
        email = m.group(1).lower()
        if "facebook" not in email:
            data["email"] = email

    # Rating — "4.8 out of 5", "100% recommend", "Rating: 4.8"
    for pat in [r'Rating[:\s]*([\d.]+)', r'([\d.]+)\s*out of\s*5', r'(\d+)%\s*recommend']:
        m = _re.search(pat, snippet, _re.I)
        if m:
            data["rating"] = m.group(1)
            break

    # Category — text segment between · separators that looks like a business type
    # e.g. "436 followers · Home Security Company · Open now"
    parts = [p.strip() for p in _re.split(r'[·•|]', snippet)]
    category_words = {
        "security", "guard", "alarm", "monitor", "cctv", "surveillance",
        "investigation", "investigator", "patrol", "company", "service",
        "solutions", "protection", "systems",
    }
    for part in parts:
        words = set(part.lower().split())
        if (3 <= len(part.split()) <= 6
                and words & category_words
                and not _re.search(r'\d{3,}', part)):
            data["category"] = part
            break

    # Description — text after "N were here." or "N likes ·" or the last long sentence
    # Snippet format: "Company. 723 likes · 3 were here. Description text here."
    desc = None
    m = _re.search(r'\d+\s+were\s+here\.\s*(.+)', snippet, _re.I | _re.DOTALL)
    if m:
        desc = m.group(1).strip()
    else:
        # Try: text after the last "· " block if it's long enough
        after_dots = [p.strip() for p in _re.split(r'[·•]', snippet) if len(p.strip()) > 40]
        if after_dots:
            desc = after_dots[-1]
    if desc and len(desc) > 20:
        data["description"] = desc[:400]

    return data


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
    _HARD_OVERSEAS = ["co.uk", ".uk", "united kingdom", ".com.au", "co.au",
                      "united states", " usa ", "u.s.a.", "us-based"]
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

        # Dedupe by URL, track score, NZ signal, and best snippet
        best_per_url = {}  # url -> (total_score, has_nz_signal, snippet)
        for url, snippet, title in all_candidates:
            ws = _word_score(url, snippet, name_words)
            nz = _nz_bonus(url, snippet, title)
            total = ws + nz
            has_nz = nz > 0
            if url not in best_per_url or total > best_per_url[url][0]:
                best_per_url[url] = (total, has_nz, snippet)

        if not best_per_url:
            return None

        ranked = sorted(best_per_url.items(), key=lambda x: -x[1][0])

        def _pick(url, entry):
            _FB_SNIPPET_CACHE[url] = entry[2]  # cache snippet for scrape_facebook_page
            return url

        # Strongly prefer results with a NZ signal — only return non-NZ as last
        # resort after an explicit NZ-targeted search also finds nothing
        nz_results = [(url, data) for url, data in ranked if data[1]]
        if nz_results:
            return _pick(*nz_results[0])

        # No NZ signal found — try one targeted search before giving up
        r_nz = google_search(f'site:facebook.com "{company_name}" "New Zealand"', num_results=10)
        if r_nz and r_nz is not SERPAPI_EXHAUSTED:
            nz_extra = _extract_fb_candidates(r_nz)
            nz_extra_signal = [(u, s, t) for u, s, t in nz_extra if _nz_bonus(u, s, t) > 0]
            if nz_extra_signal:
                best = sorted(nz_extra_signal,
                               key=lambda x: -(_word_score(x[0], x[1], name_words)
                                               + _nz_bonus(x[0], x[1], x[2])))
                winner_url, winner_snippet, _ = best[0][0], best[0][1], best[0][2]
                _FB_SNIPPET_CACHE[winner_url] = winner_snippet
                return winner_url

        # Nothing with NZ signal — try AI-suggested name variants before giving up
        try:
            ai_prompt = (
                f'A Facebook page search for NZ security company "{company_name}" found no results.\n'
                f'Suggest up to 3 alternative trading names or abbreviations this company '
                f'might use on Facebook (e.g. shortened name, without legal suffix, common alias).\n'
                + (f'Website context: {page_text[:400]}\n' if page_text else '')
                + f'Return ONLY a JSON array of strings. Do not repeat: {search_terms}'
            )
            ai_msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=120,
                messages=[{"role": "user", "content": ai_prompt}]
            )
            raw_ai = ai_msg.content[0].text.strip()
            if "```" in raw_ai:
                raw_ai = raw_ai.split("```")[1]
                if raw_ai.startswith("json"):
                    raw_ai = raw_ai[4:]
            ai_terms = json.loads(raw_ai.strip())
            if isinstance(ai_terms, list):
                for ai_term in ai_terms[:3]:
                    if not isinstance(ai_term, str) or ai_term in search_terms:
                        continue
                    print(f"  [Facebook] AI suggests: {ai_term!r}")
                    r_ai = google_search(f'site:facebook.com "{ai_term}"', num_results=20)
                    if r_ai and r_ai is not SERPAPI_EXHAUSTED:
                        ai_cands = _extract_fb_candidates(r_ai)
                        nz_ai = [(u, s, t) for u, s, t in ai_cands if _nz_bonus(u, s, t) > 0]
                        if nz_ai:
                            best = sorted(nz_ai,
                                          key=lambda x: -(_word_score(x[0], x[1], name_words)
                                                          + _nz_bonus(x[0], x[1], x[2])))
                            winner_url, winner_snippet = best[0][0], best[0][1]
                            _FB_SNIPPET_CACHE[winner_url] = winner_snippet
                            return winner_url
        except Exception as _ai_e:
            print(f"  [Facebook] AI fallback error: {_ai_e}")

        return None

    except Exception:
        pass
    return None


def scrape_linkedin_page(li_url, company_name=None):
    """Extract LinkedIn company page data.

    Tier 1: parse SerpAPI snippet cache — followers, industry, location, description.
    Tier 2: if cache empty + company_name, do a fresh SerpAPI search to warm cache.
    Tier 3: fetch the public LinkedIn page and parse structured data — website,
            headquarters, industry, size, founded. Fails gracefully if blocked.

    Returns dict: followers, description, industry, location, website, size, founded
    """
    import re as _re

    result = {
        "followers": None, "description": None, "industry": None,
        "location": None, "website": None, "size": None, "founded": None,
    }

    def _parse_li_snippet(snippet):
        out = {}
        # Followers: "1,234 followers on LinkedIn" or "1234 followers"
        m = _re.search(r'([\d,]+)\s+followers', snippet, _re.I)
        if m:
            out["followers"] = m.group(1).replace(",", "")
        # LinkedIn snippet structure: "Name | N followers on LinkedIn. Industry | Location. Description"
        # Split on " | " — first meaningful non-name parts tend to be industry and location
        parts = [p.strip() for p in _re.split(r'\s*\|\s*', snippet)]
        # Remove the company name part (usually first) and "N followers on LinkedIn" part
        clean = []
        for p in parts:
            if _re.search(r'\d+\s+followers', p, _re.I):
                continue
            if len(p) > 5:
                clean.append(p)
        # First clean part is usually industry
        if clean:
            candidate = clean[0].split(".")[0].strip()
            if len(candidate) < 60:
                out["industry"] = candidate
        # Second clean part or text after first "." in first clean part is often location
        if len(clean) >= 2:
            loc = clean[1].split(".")[0].strip()
            if len(loc) < 80:
                out["location"] = loc
        # Description: longest remaining part
        desc_parts = [p for p in clean[1:] if len(p) > 30]
        if desc_parts:
            out["description"] = max(desc_parts, key=len)[:300]
        return out

    # ── Tier 1 / 2: snippet ────────────────────────────────────────────────────
    cached = _LI_SNIPPET_CACHE.get(li_url, "")
    if not cached and company_name:
        try:
            results = google_search(f'site:linkedin.com/company "{company_name}"', num_results=5)
            if results and results is not SERPAPI_EXHAUSTED:
                _LI_RE = _re.compile(r'linkedin\.com/company/', _re.I)
                for r in results:
                    if _LI_RE.search(r.get("link", "")):
                        snippet = r.get("snippet", "")
                        if snippet:
                            _LI_SNIPPET_CACHE[li_url] = snippet
                            cached = snippet
                        break
        except Exception:
            pass

    if cached:
        parsed = _parse_li_snippet(cached)
        for k, v in parsed.items():
            if v:
                result[k] = v

    # ── Tier 3: fetch public LinkedIn page for structured details ──────────────
    missing = [k for k, v in result.items() if v is None]
    if missing:
        try:
            resp = requests.get(
                li_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-NZ,en;q=0.9",
                },
                timeout=12,
                allow_redirects=True,
            )
            if resp.status_code == 200 and "authwall" not in resp.url and len(resp.text) > 500:
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                def _after_label(label):
                    """Return the line immediately after a label line."""
                    label_l = label.lower()
                    for i, line in enumerate(lines):
                        if line.lower().strip().rstrip(":") == label_l and i + 1 < len(lines):
                            val = lines[i + 1].strip()
                            if val and val.lower() != label_l:
                                return val
                    return None

                if not result["website"]:
                    ws = _after_label("website")
                    if ws and "linkedin" not in ws.lower():
                        result["website"] = ws
                if not result["industry"]:
                    result["industry"] = _after_label("industry")
                if not result["size"]:
                    result["size"] = _after_label("company size")
                if not result["founded"]:
                    result["founded"] = _after_label("founded")
                if not result["location"]:
                    result["location"] = _after_label("headquarters")

                # Description: og:description meta tag is often available
                if not result["description"]:
                    og = soup.find("meta", property="og:description")
                    if og and og.get("content"):
                        content = og["content"].strip()
                        if len(content) > 30:
                            result["description"] = content[:300]
        except Exception:
            pass

    return result


_SERVICE_SLUGS = (
    "service", "services", "product", "products", "solution", "solutions",
    "what-we-do", "whatwedo", "what-we-offer", "offerings", "capabilities",
    "security", "alarm", "alarms", "cctv", "camera", "cameras", "monitoring",
    "surveillance", "about", "about-us",
)

def gather_service_text(website_url, homepage_text):
    """Return homepage_text plus text scraped from up to 3 service-related sub-pages.
    Fetches the homepage a second time (lightweight) just to parse internal links."""
    import re
    from urllib.parse import urlparse, urljoin, urlunparse

    try:
        base = urlparse(website_url)
        base_root = urlunparse(base._replace(path="", query="", fragment=""))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Always start from root homepage so we get the main nav links
        fetch_url = base_root if base_root else website_url
        resp = requests.get(fetch_url, headers=headers, timeout=8)
        if not resp.ok:
            return homepage_text
        soup = BeautifulSoup(resp.text, "html.parser")
        # Save href strings before any decompose (decompose orphans child tags)
        all_hrefs = [a.get("href", "") for a in soup.find_all("a", href=True) if a.get("href")]
        # Strip chrome and get root page text
        for tag in soup.find_all(["nav", "header", "footer", "script", "style", "noscript"]):
            tag.decompose()
        root_text = " ".join(soup.get_text(separator=" ", strip=True).split())[:3000]

        seen = {fetch_url.rstrip("/")}
        candidates = []
        for href_raw in all_hrefs:
            href = href_raw.split("?")[0].split("#")[0].rstrip("/")
            full = urljoin(fetch_url, href)
            parsed = urlparse(full)
            # Internal links only, same domain
            if parsed.netloc and parsed.netloc != base.netloc:
                continue
            if full.rstrip("/") in seen:
                continue
            path_lower = parsed.path.lower()
            if any(slug in path_lower for slug in _SERVICE_SLUGS):
                seen.add(full.rstrip("/"))
                candidates.append(full)
            if len(candidates) >= 6:
                break

        extra_texts = []
        for sub_url in candidates[:3]:
            try:
                r = requests.get(sub_url, headers=headers, timeout=8)
                if not r.ok:
                    continue
                s = BeautifulSoup(r.text, "html.parser")
                for tag in s.find_all(["nav", "header", "footer", "script", "style", "noscript"]):
                    tag.decompose()
                extra_texts.append(" ".join(s.get_text(separator=" ", strip=True).split())[:2000])
                print(f"  [Service page] scraped {sub_url}")
            except Exception:
                pass

        # Combine: root homepage text + any extra service pages scraped
        parts = [root_text]
        if extra_texts:
            parts.extend(extra_texts)
        return " ".join(parts)

    except Exception as e:
        print(f"  [gather_service_text error] {e}")
        return homepage_text


def llm_verify_associations(company_name, website_url, region,
                            linkedin_url=None, facebook_url=None,
                            co_name=None, co_address=None,
                            pspla_name=None, pspla_address=None, pspla_status=None,
                            nzsa_name=None,
                            google_address=None, google_phone=None,
                            fb_description=None):
    """Use Claude Haiku to sanity-check ALL gathered associations before saving.
    Reviews PSPLA, CO, NZSA, LinkedIn, Facebook, and Google profile data.
    Returns a dict of fields to reject (set to None), with reasons."""

    # Build the context block — only include fields we actually have
    lines = []
    if linkedin_url:
        lines.append(f"LinkedIn URL: {linkedin_url}")
    if facebook_url:
        lines.append(f"Facebook URL: {facebook_url}")
        if fb_description:
            lines.append(f"Facebook description: {fb_description[:200]}")
    if co_name:
        lines.append(f"Companies Office registered name: {co_name}")
        if co_address:
            lines.append(f"Companies Office address: {co_address}")
    if pspla_name:
        lines.append(f"PSPLA matched name: {pspla_name}")
        if pspla_address:
            lines.append(f"PSPLA address: {pspla_address}")
        if pspla_status:
            lines.append(f"PSPLA status: {pspla_status}")
    if nzsa_name:
        lines.append(f"NZSA member name: {nzsa_name}")
    if google_address:
        lines.append(f"Google Business address: {google_address}")
        if google_phone:
            lines.append(f"Google Business phone: {google_phone}")

    if not lines:
        return {}

    prompt = f"""You are doing a final sanity-check on all data gathered for a New Zealand security company record before it is saved to the database.

Company name: {company_name}
Website: {website_url or "unknown"}
Region: {region or "unknown"}

Data gathered automatically:
{chr(10).join(lines)}

For each piece of data, decide if it genuinely belongs to this company or is likely a false match from an automated search.

Rules:
- LinkedIn/Facebook URL slugs should share meaningful words with the company name. A completely unrelated slug is wrong.
- Companies Office name: minor variations (Ltd/Limited, (2008), punctuation) are fine. A completely different business is wrong.
- PSPLA name: should be a plausible variation of the company name or a known trading name. Totally unrelated is wrong.
- NZSA name: same rules as CO name.
- Google address: region should roughly match "{region or 'unknown'}". A different NZ city is suspicious but not necessarily wrong (company may operate nationally).
- When in doubt, TRUST it — only reject if you are confident it is a different company entirely.
- Do not reject just because of capitalisation, abbreviations, or minor word order differences.

Reply with ONLY valid JSON. List only the fields you are REJECTING. If everything looks correct return {{}}.
Valid rejection keys: linkedin_url, facebook_url, co_name, pspla_name, nzsa_name, google_profile
Example: {{"linkedin_url": "slug evolve-fire-protection shares no words with Alarmtech"}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        _llm_ok()
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        rejections = json.loads(raw.strip())
        if rejections:
            print(f"  [LLM verify] Rejected for {company_name}: {list(rejections.keys())}")
            for field, reason in rejections.items():
                print(f"    {field}: {reason}")
        return rejections
    except Exception as e:
        _llm_error("llm_verify_associations", e)
        return {}


def detect_services(page_text):
    """Use Claude Haiku to detect if a company's website mentions alarm systems,
    CCTV/cameras, or alarm monitoring services. Returns dict with three bool keys:
    has_alarm_systems, has_cctv_cameras, has_alarm_monitoring.
    Falls back to False for all on error or empty page_text."""
    if not page_text or len(page_text.strip()) < 50:
        return {"has_alarm_systems": False, "has_cctv_cameras": False, "has_alarm_monitoring": False}

    # Truncate to avoid huge token usage — Haiku handles this easily
    snippet = page_text[:10000]

    prompt = f"""You are analysing text from a New Zealand security company's website.
Determine whether the website mentions each of the following three service categories.
Use a broad interpretation — include synonyms and related terms.

SERVICE CATEGORIES AND THEIR SYNONYMS:
1. Alarm Systems — intruder alarms, burglar alarms, security alarms, alarm installation,
   alarm systems, house alarm, residential alarm, commercial alarm, alarm panels,
   motion detectors, door/window sensors, PIR sensors, alarm equipment.
2. CCTV / Cameras — CCTV, surveillance cameras, IP cameras, security cameras,
   video surveillance, camera systems, NVR, DVR, PTZ cameras, dashcam monitoring,
   remote video, video analytics.
3. Alarm Monitoring — 24/7 monitoring, alarm monitoring, monitoring centre, monitoring center,
   monitoring station, monitored alarm, central station monitoring, remote monitoring,
   emergency response, guard response, patrol response.

Website text to analyse:
---
{snippet}
---

Reply with ONLY valid JSON, no markdown fences:
{{"has_alarm_systems": true/false, "has_cctv_cameras": true/false, "has_alarm_monitoring": true/false}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        _llm_ok()
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return {
            "has_alarm_systems": bool(result.get("has_alarm_systems", False)),
            "has_cctv_cameras": bool(result.get("has_cctv_cameras", False)),
            "has_alarm_monitoring": bool(result.get("has_alarm_monitoring", False)),
        }
    except Exception as e:
        _llm_error("detect_services", e)
        return {"has_alarm_systems": False, "has_cctv_cameras": False, "has_alarm_monitoring": False}


def scrape_facebook_page(fb_url, company_name=None):
    """Gather public Facebook business page data using three tiers:

    1. Google/SerpAPI snippet cache — populated by find_facebook_url, no FB hit.
       Reliably yields: followers, phone, category, rating (from Google's index).
    2. og: meta tags from page HEAD only — survives login wall, fast, low detection risk.
       Reliably yields: description.
    3. Mobile site fallback (m.facebook.com/about/) — full parse if 1+2 leave gaps.
       May be blocked; always fails gracefully.

    All fields may be None — caller must handle gracefully.
    Returns dict: followers, phone, email, address, description, category, rating
    """
    import re as _re
    import time as _time

    result = {
        "followers": None, "phone": None, "email": None,
        "address": None, "description": None, "category": None,
        "rating": None,
    }

    # ── Tier 1: parse the Google snippet we already fetched ──────────────────
    # If cache is cold (e.g. re-check on existing URL) and company_name is known,
    # do a quick SerpAPI search to warm the cache — zero direct FB hits needed.
    cached_snippet = _FB_SNIPPET_CACHE.get(fb_url, "")
    if not cached_snippet and company_name:
        try:
            warm_results = google_search(f'site:facebook.com "{company_name}"', num_results=5)
            if warm_results and warm_results is not SERPAPI_EXHAUSTED:
                for r in warm_results:
                    link = r.get("link", "")
                    if "facebook.com" in link:
                        snippet = r.get("snippet", "")
                        if snippet:
                            _FB_SNIPPET_CACHE[fb_url] = snippet
                            cached_snippet = snippet
                        break
        except Exception:
            pass
    if cached_snippet:
        parsed = _parse_fb_snippet(cached_snippet)
        for k, v in parsed.items():
            if v:
                result[k] = v

    # ── Tier 2: fetch only the HTML <head> for og: meta tags ─────────────────
    # We stream just the first 8 KB — enough for the <head> block, avoids
    # downloading the full JS bundle and reduces fingerprinting exposure.
    if not result["description"]:
        try:
            head_resp = requests.get(
                fb_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html",
                    "Accept-Language": "en-NZ,en;q=0.9",
                },
                timeout=10,
                stream=True,
                allow_redirects=True,
            )
            # Read only first 8 KB — the <head> is always in there
            head_html = b""
            for chunk in head_resp.iter_content(chunk_size=1024):
                head_html += chunk
                if len(head_html) >= 8192 or b"</head>" in head_html:
                    break
            head_resp.close()
            head_text = head_html.decode("utf-8", errors="ignore")

            if "login" not in head_resp.url:
                for pat in [
                    r'<meta[^>]+property="og:description"[^>]+content="([^"]{20,500})"',
                    r'<meta[^>]+content="([^"]{20,500})"[^>]+property="og:description"',
                    r'<meta[^>]+name="description"[^>]+content="([^"]{20,500})"',
                ]:
                    m = _re.search(pat, head_text, _re.IGNORECASE)
                    if m:
                        desc = m.group(1).strip()
                        if len(desc) > 30 and "facebook" not in desc.lower()[:30]:
                            result["description"] = desc[:500]
                            break
        except Exception:
            pass

    # ── Tier 3: mobile site — only if fields still missing ───────────────────
    missing = [k for k, v in result.items() if v is None]
    if missing:
        slug = _re.sub(r"^https?://(www\.)?facebook\.com/", "", fb_url.rstrip("/"))
        _time.sleep(1)  # brief pause — reduces consecutive-request detection
        raw = ""
        for url in [f"https://m.facebook.com/{slug}/about/",
                    f"https://m.facebook.com/{slug}/"]:
            try:
                resp = requests.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/16.6 Mobile/15E148 Safari/604.1"
                        ),
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-NZ,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "DNT": "1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    timeout=12,
                    allow_redirects=True,
                )
                if (resp.status_code == 200
                        and "login" not in resp.url
                        and len(resp.text) > 800):
                    raw = resp.text
                    break
            except Exception:
                pass

        if raw:
            def _try(field, patterns, transform=None):
                if result[field]:
                    return  # already filled by tier 1 or 2
                for pat in patterns:
                    m = _re.search(pat, raw, _re.IGNORECASE)
                    if m:
                        val = (transform(m) if transform else m.group(1).strip())
                        if val:
                            result[field] = val
                            return

            _try("followers", [
                r'"follower_count"\s*:\s*(\d+)',
                r'"subscriber_count"\s*:\s*(\d+)',
                r'(\d[\d,]+)\s+followers',
            ], lambda m: m.group(1).replace(",", ""))

            _try("phone", [
                r'"label"\s*:\s*"Phone"[^}]{0,200}"text"\s*:\s*"([^"]+)"',
                r'(?:Phone|phone|tel)[:\s]*(\+?\d[\d\s\-().]{6,20})',
            ], lambda m: (m.group(1).strip()
                          if _re.search(r'\d{6,}', m.group(1).replace(" ", ""))
                          else None))

            _try("email", [
                r'"label"\s*:\s*"Email"[^}]{0,200}"text"\s*:\s*"([^"@\s]{1,60}@[^"@\s]{1,60})"',
                r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
            ], lambda m: (m.group(1).lower()
                          if not any(x in m.group(1) for x in ["facebook", "sentry"])
                          else None))

            _try("address", [
                r'"label"\s*:\s*"Address"[^}]{0,400}"text"\s*:\s*"([^"]{5,120})"',
            ])

            _try("description", [
                r'"description"\s*:\s*"([^"]{20,500})"',
            ], lambda m: (m.group(1).strip()
                          if "facebook" not in m.group(1).lower()[:30]
                          else None))

            _try("category", [
                r'"category_name"\s*:\s*"([^"]{3,60})"',
            ], lambda m: (m.group(1).strip()
                          if not _re.match(r'^[A-Z_]+$', m.group(1).strip())
                          else None))

            _try("rating", [
                r'"overall_star_rating"\s*:\s*([\d.]+)',
                r'([\d.]+)\s*out of\s*5',
                r'(\d+)\s*%\s*recommend',
            ])

    return result


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
        # AI-suggested name variants before giving up
        try:
            ai_prompt = (
                f'A LinkedIn search for NZ company "{company_name}" found no results.\n'
                f'Suggest up to 3 alternative names or abbreviations this company might '
                f'use on LinkedIn (e.g. without legal suffix, shortened, trading name).\n'
                + (f'Website context: {page_text[:400]}\n' if page_text else '')
                + f'Return ONLY a JSON array of strings.'
            )
            ai_msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=120,
                messages=[{"role": "user", "content": ai_prompt}]
            )
            raw_ai = ai_msg.content[0].text.strip()
            if "```" in raw_ai:
                raw_ai = raw_ai.split("```")[1]
                if raw_ai.startswith("json"):
                    raw_ai = raw_ai[4:]
            ai_terms = json.loads(raw_ai.strip())
            if isinstance(ai_terms, list):
                for ai_term in ai_terms[:3]:
                    if not isinstance(ai_term, str):
                        continue
                    print(f"  [LinkedIn] AI suggests: {ai_term!r}")
                    r_ai = google_search(
                        f'site:linkedin.com/company "{ai_term}" New Zealand', num_results=5)
                    if r_ai and r_ai is not SERPAPI_EXHAUSTED:
                        for r in r_ai:
                            link = r.get("link", "")
                            if not _LI_RE.search(link):
                                continue
                            c = _extract_candidate(link)
                            if c:
                                candidates.append((c, _score(r), r))
                    if candidates:
                        break
        except Exception as _ai_e:
            print(f"  [LinkedIn] AI fallback error: {_ai_e}")

    if not candidates:
        return None

    # Pick highest-scoring candidate; require at least one actual word match in the slug
    # (NZ hit alone is not enough — it would accept unrelated companies)
    candidates.sort(key=lambda x: -x[1])
    best_url, best_score, best_result = candidates[0]
    slug = best_url.lower().replace("-", " ").replace("_", " ").replace("/", " ")
    slug_word_hits = sum(1 for w in name_words if w in slug)
    if best_score >= 1 and slug_word_hits >= 1:
        snippet = best_result.get("snippet", "")
        if snippet:
            _LI_SNIPPET_CACHE[best_url] = snippet
        return best_url
    return None


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
        "rows": "25",
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
        _llm_ok()
        return result.get("suggested_names", [])
    except Exception as e:
        _llm_error("_llm_suggest_pspla_names", e)
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
        result = json.loads(raw.strip())
        _llm_ok()
        return result
    except Exception as e:
        _llm_error("_llm_deep_verify", e)
        # Graceful fallback: let the original verify_pspla_match result stand rather than
        # falsely rejecting the match because the deeper check API call failed.
        return {"match": True, "confidence": "low",
                "reason": f"LLM unavailable ({e}) — deep verify skipped, original match retained"}


def verify_pspla_match(website_company, pspla_company, website_region, pspla_address, _audit_name=None):
    """Use Claude to verify if a PSPLA match is genuinely the same company.
    _audit_name: if set, writes the LLM decision to the audit log."""
    import re as _re
    # Hard pre-check: every significant word in the website company name must appear
    # as an EXACT whole word in the PSPLA name.  Catches "coast" vs "coastal",
    # "guard" vs "guardian", etc. without a Claude call.
    _GENERIC = {'limited', 'security', 'services', 'solutions', 'systems', 'group',
                'new', 'zealand', 'national', 'management', 'alarm', 'alarms',
                'install', 'installer', 'surveillance', 'protection', 'electrical',
                'plumbing', 'construction', 'engineering', 'contracting', 'maintenance'}
    # Geographic qualifiers that can legitimately prefix a PSPLA name without indicating
    # a different company (e.g. "Southern Guardian Security" for "Guardian Security")
    _GEO_QUALIFIERS = {'north', 'south', 'east', 'west', 'central', 'upper', 'lower',
                       'greater', 'northern', 'southern', 'eastern', 'western', 'outer',
                       'inner', 'auckland', 'wellington', 'canterbury', 'otago', 'waikato',
                       'nelson', 'tasman', 'marlborough', 'northland', 'hawkes', 'manawatu'}
    company_sig = [w for w in _re.findall(r'[a-z]+', website_company.lower())
                   if len(w) >= 4 and w not in _GENERIC]
    pspla_words = _re.findall(r'[a-z]+', pspla_company.lower())
    pspla_exact = set(pspla_words)
    if company_sig:
        missing = [w for w in company_sig if w not in pspla_exact]
        if missing and len(missing) == len(company_sig):
            # Every distinctive word is absent — definitely not the same company
            return {"match": False, "confidence": "high",
                    "reason": f"Distinctive word(s) {missing} not found as exact words in PSPLA name"}

    # Reverse check: if PSPLA name has distinctive words as a PREFIX that don't appear
    # in the website company name, it's likely a different entity.
    # e.g. website="Livewire Electrical", PSPLA="Addz Livewire Electrical"
    # "addz" is a distinctive prefix → different company.
    # Exception: geographic qualifiers are allowed (e.g. "Southern Guardian Security")
    website_exact = set(_re.findall(r'[a-z]+', website_company.lower()))
    first_website_idx = next(
        (i for i, w in enumerate(pspla_words) if w in website_exact),
        len(pspla_words)
    )
    if first_website_idx > 0:
        prefix_words = pspla_words[:first_website_idx]
        distinctive_prefix = [w for w in prefix_words
                               if len(w) >= 4 and w not in _GENERIC and w not in _GEO_QUALIFIERS]
        if distinctive_prefix:
            return {"match": False, "confidence": "high",
                    "reason": f"PSPLA name has distinctive prefix word(s) {distinctive_prefix} not present in website company name — indicates a different entity"}

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
        _llm_ok()
        if _audit_name:
            verdict = "ACCEPTED" if result.get("match") else "REJECTED"
            write_audit("llm_decision", None, _audit_name,
                        changes=f"verify_pspla_match {verdict} '{pspla_company}' "
                                f"(confidence: {result.get('confidence','?')}): {result.get('reason','')}",
                        triggered_by="verify_pspla_match",
                        notes=f"PSPLA address: {pspla_address or 'unknown'} | region: {website_region or 'unknown'}")
        return result
    except Exception as e:
        _llm_error("verify_pspla_match", e)
        # Graceful fallback: the hard pre-check above already passed, so the distinctive words
        # DO appear in the PSPLA name.  Rather than falsely rejecting a genuine match because
        # the API is down, return a low-confidence acceptance flagged for manual review.
        return {"match": True, "confidence": "low",
                "reason": f"LLM unavailable ({e}) — pre-check passed, flagged for review"}


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
        result = json.loads(raw.strip())
        _llm_ok()
        return result
    except Exception as e:
        _llm_error("_llm_cross_check_sources", e)
        return {"consistent": True, "confidence": "low", "issues": [], "notes": f"cross-check unavailable: {e}"}


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

            def _best_doc_for_company(all_docs, verified_doc):
                """Return the best doc across ALL permits for the same company name.

                A company may have been licensed multiple times — each renewal/reissue gets a
                NEW permit number, so _best_doc_for_permit (same permit only) misses them.
                We match by exact company name, then pick Active > Expired > Withdrawn and
                within the same status the most-recent expiry date wins.

                Falls back to verified_doc if nothing better is found.
                """
                norm_name = (
                    get_field(verified_doc, "name_txt") or
                    get_field(verified_doc, "caseTitle_s") or ""
                ).strip().upper()
                if not norm_name:
                    return verified_doc
                same_company = [
                    d for d in all_docs
                    if (get_field(d, "name_txt") or get_field(d, "caseTitle_s") or "").strip().upper() == norm_name
                ]
                if not same_company:
                    return verified_doc
                best = sorted(same_company,
                              key=lambda d: (_STATUS_SORT.get(_get_status(d), 99), _date_sort_key(d)))[0]
                # Log if we upgraded to a better/newer permit
                best_permit = get_field(best, "permitNumber_txt")
                orig_permit = get_field(verified_doc, "permitNumber_txt")
                if best_permit and orig_permit and best_permit != orig_permit:
                    print(f"  [PSPLA] Upgraded from permit {orig_permit} "
                          f"({_get_status(verified_doc)}) → {best_permit} "
                          f"({_get_status(best)}) for '{norm_name}'")
                return best

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

            # Extra guard: even for a "full name" single-permit match, force verification
            # if the top PSPLA result has a distinctive prefix word that doesn't appear in
            # the company name.  Catches e.g. "Addz Livewire Electrical Limited" being
            # trusted for "Livewire Electrical" because Solr returned it as the only hit.
            if not needs_verification and docs:
                import re as _re_pfx
                _PFX_GENERIC = {'limited', 'security', 'services', 'solutions', 'systems',
                                'group', 'new', 'zealand', 'national', 'management',
                                'alarm', 'alarms', 'install', 'installer', 'surveillance',
                                'protection', 'electrical', 'plumbing', 'construction',
                                'engineering', 'contracting', 'maintenance'}
                _PFX_GEO = {'north', 'south', 'east', 'west', 'central', 'upper', 'lower',
                            'greater', 'northern', 'southern', 'eastern', 'western',
                            'auckland', 'wellington', 'canterbury', 'otago', 'waikato',
                            'nelson', 'tasman', 'northland', 'hawkes', 'manawatu'}
                top_name = (get_field(docs[0], "name_txt") or
                            get_field(docs[0], "caseTitle_s") or "").lower()
                top_words = _re_pfx.findall(r'[a-z]+', top_name)
                co_words = set(_re_pfx.findall(r'[a-z]+', company_name.lower()))
                first_co_idx = next(
                    (i for i, w in enumerate(top_words) if w in co_words),
                    len(top_words)
                )
                if first_co_idx > 0:
                    prefix_distinctive = [w for w in top_words[:first_co_idx]
                                          if len(w) >= 4
                                          and w not in _PFX_GENERIC
                                          and w not in _PFX_GEO]
                    if prefix_distinctive:
                        print(f"  [PSPLA] Forcing verification — '{top_name}' has distinctive "
                              f"prefix {prefix_distinctive} not in '{company_name}'")
                        needs_verification = True

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

                # Upgrade: find the best doc across ALL permits for the same company name.
                # A company can be reissued a new permit number on renewal — _best_doc_for_permit
                # (same permit only) would miss newer active permits. _best_doc_for_company
                # looks across all docs in this result set by exact name match.
                matched = _best_doc_for_company(docs, verified_doc)
            else:
                # Single company in results and full-name match — no verification needed.
                # Still upgrade in case there are multiple permit rows for this company.
                matched = _best_doc_for_company(docs, docs[0])

            has_active = _get_status(matched) == "active"

            name_field = get_field(matched, "name_txt") or get_field(matched, "caseTitle_s") or company_name
            pspla_address = get_field(matched, "registeredOffice_txt") or get_field(matched, "townCity_txt")
            permit_number = get_field(matched, "permitNumber_txt")
            permit_status = get_field(matched, "permitStatus_s")
            permit_expiry = get_field(matched, "permitEndDate_s")
            permit_start = get_field(matched, "permitStartDates_s")
            permit_type = get_field(matched, "permitTempOrPerm_s")
            license_type = "individual" if matched.get("isIndividual_b") else "company"

            # Build a human-readable list of granted license classes
            _class_fields = {
                "securityTechnician_s": "Security Technician",
                "securityConsultant_s": "Security Consultant",
                "monitoringOfficer_s": "Monitoring Officer",
                "propertyGuard_s": "Property Guard",
                "crowdController_s": "Crowd Controller",
                "personalGuard_s": "Personal Guard",
                "privateInvestigator_s": "Private Investigator",
                "repossessionAgent_s": "Repossession Agent",
            }
            granted_classes = [
                label for field, label in _class_fields.items()
                if (get_field(matched, field) or "").lower() == "granted"
            ]
            license_classes = ", ".join(granted_classes) if granted_classes else None

            # Check user corrections — block matches flagged as false positives
            blocked, block_reason = _is_pspla_match_blocked(company_name, name_field)
            if blocked:
                print(f"  [Correction applied] '{company_name}' -> '{name_field}' blocked: {block_reason}")
                return {"licensed": False, "matched_name": None, "license_type": None,
                        "match_method": "blocked by user correction",
                        "pspla_address": None, "pspla_license_number": None,
                        "pspla_license_status": None, "pspla_license_expiry": None,
                        "pspla_license_start": None, "pspla_permit_type": None,
                        "pspla_license_classes": None}

            return {
                "licensed": has_active,
                "matched_name": name_field,
                "license_type": license_type,
                "match_method": match_method,
                "pspla_address": pspla_address,
                "pspla_license_number": permit_number,
                "pspla_license_status": permit_status,
                "pspla_license_expiry": permit_expiry,
                "pspla_license_start": permit_start,
                "pspla_permit_type": permit_type,
                "pspla_license_classes": license_classes,
            }
        else:
            return {"licensed": False, "matched_name": None, "license_type": None, "match_method": "no match found", "pspla_address": None, "pspla_license_number": None, "pspla_license_status": None, "pspla_license_expiry": None}

    except Exception as e:
        print(f"  [PSPLA check error] {e}")
        return {"licensed": None, "matched_name": None, "license_type": None, "match_method": "error", "pspla_address": None}


def _co_fetch_directors(company_number, co_headers):
    """Fetch director names and website URL from a Companies Office company detail page.
    Returns (directors_list, website_or_None)."""
    import re as _re
    try:
        detail_url = f"https://app.companiesoffice.govt.nz/companies/app/ui/pages/companies/{company_number}"
        resp = requests.get(detail_url, headers=co_headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        directors = []
        website = None
        i = 0
        while i < len(lines):
            # Directors section
            if _re.search(r"Showing \d+ of \d+ director", lines[i], _re.IGNORECASE):
                for j in range(i + 1, min(i + 30, len(lines))):
                    line = lines[j]
                    if any(kw in line for kw in ["Shareholding", "Documents", "PPSR",
                                                  "Company record link", "Trading Name",
                                                  "Phone Number", "Email Address"]):
                        break
                    clean = line.replace(" ", "").replace("-", "").replace("'", "")
                    if (5 < len(line) < 60
                            and not any(c.isdigit() for c in line)
                            and clean.isalpha()):
                        name = " ".join(line.split()).title()
                        if name not in directors:
                            directors.append(name)
            # Website from NZBN section — label is "Website(s)" with URL on same or next line
            if "website" in lines[i].lower() and not website:
                for j in range(i, min(i + 3, len(lines))):
                    m = _re.search(r'https?://[^\s]+', lines[j])
                    if m:
                        website = m.group(0).rstrip('.,)')
                        break
            i += 1
        return directors, website
    except Exception:
        return [], None


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
            """Parse company number, NZBN, address, status and trading name from lines
            following a company name entry in CO search results."""
            address_words = ["road", "street", "avenue", "drive", "place", "lane", "way", "rd ", "st ", "ave "]
            address = None
            company_number = None
            nzbn = None
            status = "registered"  # default
            trading_name = None
            for j in range(i + 1, min(i + 15, len(lines))):
                lj = lines[j]
                m = _re.match(r"\((\d{6,8})\)", lj)
                if m and not company_number:
                    company_number = m.group(1)
                nzbn_m = _re.search(r"NZBN:\s*(\d+)", lj)
                if nzbn_m and not nzbn:
                    nzbn = nzbn_m.group(1)
                lj_lower = lj.lower()
                if "removed" in lj_lower:
                    status = "removed"
                elif "deregistered" in lj_lower:
                    status = "deregistered"
                if any(w in lj_lower for w in address_words) and len(lj) > 10 and not address:
                    address = lj
                # Trading name: "Trading as" label followed by the trading name on the next line
                if lj_lower.strip() in ("trading as", "trading as:") or lj_lower.startswith("trading as"):
                    # Trading name may be on same line after the label, or the next non-empty line
                    inline = _re.sub(r'^trading as[:\s]*', '', lj, flags=_re.IGNORECASE).strip()
                    if inline:
                        trading_name = inline.title()
                    elif j + 1 < len(lines):
                        trading_name = lines[j + 1].strip().title()
                if address and company_number:
                    break
            # Incorporation date
            incorporated = None
            for j in range(i + 1, min(i + 20, len(lines))):
                if "Incorporation Date" in lines[j]:
                    if j + 1 < len(lines):
                        candidate = lines[j + 1].strip()
                        if _re.match(r"\d{1,2} \w+ \d{4}", candidate):
                            incorporated = candidate
                    break
            return {"name": line.title(), "trading_name": trading_name,
                    "address": address, "company_number": company_number,
                    "nzbn": nzbn, "status": status, "incorporated": incorporated}

        def _find_match(lines, name_upper):
            """Find a CO result matching name_upper against registered name OR trading name."""
            for i, line in enumerate(lines):
                if name_upper in line.upper():
                    return _parse_co_result(lines, i, line)
            # Also check trading-as lines — company may be registered under a different name
            for i, line in enumerate(lines):
                lj_lower = line.lower().strip()
                if lj_lower in ("trading as", "trading as:") or lj_lower.startswith("trading as"):
                    # Get the trading name (inline or next line)
                    inline = _re.sub(r'^trading as[:\s]*', '', line, flags=_re.IGNORECASE).strip()
                    trading_name_line = inline if inline else (lines[i + 1].strip() if i + 1 < len(lines) else "")
                    if name_upper in trading_name_line.upper():
                        # Find the parent registered company (look back for ALL CAPS entry)
                        for k in range(i - 1, max(-1, i - 10), -1):
                            if (lines[k].isupper() and len(lines[k]) >= 5
                                    and any(w in lines[k] for w in ["LIMITED", "LTD", "TRUST", "INCORPORATED"])):
                                result = _parse_co_result(lines, k, lines[k])
                                # Override the trading name with the matched one
                                if not result.get("trading_name"):
                                    result["trading_name"] = trading_name_line.title()
                                print(f"  [Companies Office] Matched via trading name: {trading_name_line!r} → {lines[k].title()}")
                                return result
            return None

        def _find_all_co_results(lines):
            """Return all company entries found in the CO search result page."""
            results = []
            for i, line in enumerate(lines):
                if (line.isupper() and len(line) >= 5
                        and any(w in line for w in ["LIMITED", "LTD", "TRUST", "INCORPORATED"])):
                    results.append(_parse_co_result(lines, i, line))
            return results

        result = _find_match(lines, company_name_upper)

        # ── Fallback search terms when initial search finds no exact match ───
        # Rule-based variants first (free), then AI-suggested variants.
        if result is None:
            import re as _re

            def _rule_variants(name):
                """Generate cheap rule-based name variants to retry CO search.
                Returns list of (variant, match_with_term) tuples.
                match_with_term=False means the variant is only a broader search query;
                results must be matched against the original/normalized name, not the variant.
                """
                variants = []
                # Replace slash/hyphen with space: "24/Seven" → "24 Seven", "A-1" → "A 1"
                slashed = _re.sub(r'[/\-]', ' ', name).strip()
                slashed = _re.sub(r'\s{2,}', ' ', slashed)
                if slashed.lower() != name.lower():
                    variants.append((slashed, True))
                # Insert space before digit runs: "Code9" → "Code 9"
                spaced = _re.sub(r'([A-Za-z])(\d)', r'\1 \2', name)
                spaced = _re.sub(r'(\d)([A-Za-z])', r'\1 \2', spaced)
                if spaced.lower() != name.lower():
                    variants.append((spaced, True))
                # CamelCase split: "AlarmWatch" → "Alarm Watch"
                camel = _re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
                if camel.lower() != name.lower():
                    variants.append((camel, True))
                # Strip legal suffixes for a broader search
                stripped = _re.sub(
                    r'\b(limited|ltd\.?|holdings|group|nz|new zealand)\b', '', name,
                    flags=_re.IGNORECASE).strip().strip(',').strip()
                if stripped and stripped.lower() != name.lower():
                    variants.append((stripped, True))
                # Strip common industry words — used as a broader CO search query ONLY.
                # match_with_term=False: results must match the original name (or trading name),
                # NOT the stripped term — otherwise "24 Seven" matches "24 Seven Ccc Limited".
                _INDUSTRY = r'\b(electrical|security|services|solutions|systems|alarms|' \
                            r'plumbing|construction|engineering|contracting|technologies|' \
                            r'technology|communications|group|limited|ltd)\b'
                core = _re.sub(_INDUSTRY, '', slashed if slashed.lower() != name.lower() else name,
                               flags=_re.IGNORECASE).strip()
                core = _re.sub(r'\s{2,}', ' ', core).strip()
                existing = {v.lower() for v, _ in variants}
                if core and core.lower() not in existing and core.lower() != name.lower() and len(core) >= 3:
                    variants.append((core, False))  # query-only, don't match on stripped term
                return variants

            def _try_co_search(term, match_with_term=True):
                """Search CO with an alternative term, return best matching result or None.
                If match_with_term=False, only match against the original/normalized name —
                the term is used purely as a broader search query."""
                try:
                    alt_resp = requests.get(url, params={**params, "q": term},
                                            headers=co_headers, timeout=15)
                    alt_lines = [l.strip() for l in
                                 BeautifulSoup(alt_resp.text, "html.parser")
                                 .get_text(separator="\n", strip=True).split("\n") if l.strip()]
                    # Always try normalized original name first (catches t/a trading name lines)
                    # e.g. "24 SEVEN ELECTRICAL" matches trading name of LIGHTHOUSE SERVICES LIMITED
                    normalized_upper = _re.sub(r'[/\-]', ' ', company_name).strip().upper()
                    normalized_upper = _re.sub(r'\s{2,}', ' ', normalized_upper)
                    if normalized_upper and normalized_upper != company_name_upper:
                        hit = _find_match(alt_lines, normalized_upper)
                        if hit:
                            return hit, alt_lines
                    # Match with the search term itself (only when term is a real name variant)
                    if match_with_term:
                        hit = _find_match(alt_lines, term.upper())
                        if hit:
                            return hit, alt_lines
                    # Also try original name in case CO uses the original
                    hit = _find_match(alt_lines, company_name_upper)
                    if hit:
                        return hit, alt_lines
                    return None, alt_lines
                except Exception as _e:
                    print(f"  [Companies Office] Alt search error for {term!r}: {_e}")
                    return None, []

            def _co_addr_ok(found_addr, ref_addr):
                """Return True if found_addr is geographically compatible with ref_addr.
                Returns True if either is missing (can't validate). Rejects if no significant
                location words from ref_addr appear anywhere in found_addr."""
                if not found_addr or not ref_addr:
                    return True
                _generic = {"road", "street", "avenue", "drive", "place", "lane", "suite",
                            "level", "floor", "unit", "post", "box", "zealand", "limited"}
                ref_words = {w.lower() for w in _re.split(r'[\s,./\-]+', ref_addr)
                             if len(w) >= 4 and w.lower() not in _generic}
                if not ref_words:
                    return True
                found_lower = found_addr.lower()
                return any(w in found_lower for w in ref_words)

            tried_terms = [search_term]
            alt_lines_last = []

            # 1. Rule-based variants
            for variant, match_with_term in _rule_variants(company_name):
                if variant in tried_terms:
                    continue
                tried_terms.append(variant)
                print(f"  [Companies Office] No match — trying variant: {variant!r}")
                hit, alt_lines_last = _try_co_search(variant, match_with_term=match_with_term)
                if hit:
                    if pspla_address and not _co_addr_ok(hit.get("address"), pspla_address):
                        print(f"  [Companies Office] Address mismatch — skipping {hit['name']!r}"
                              f" ({hit.get('address')}) vs PSPLA {pspla_address!r}")
                        continue
                    result = hit
                    lines = alt_lines_last  # update lines for director fetch below
                    print(f"  [Companies Office] Found via variant {variant!r}: {hit['name']}")
                    break

            # 2. AI-suggested alternatives (only if still no match)
            if result is None:
                try:
                    ai_prompt = f"""A search for NZ Companies Office records for "{company_name}" found no match.
Suggest up to 4 alternative search terms to try (e.g. different spacing, abbreviations, trading names, without legal suffixes).
Return ONLY a JSON array of strings, e.g. ["Code 9", "Code Nine Limited"]
Do not include terms already tried: {tried_terms}"""
                    ai_msg = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        messages=[{"role": "user", "content": ai_prompt}]
                    )
                    raw_ai = ai_msg.content[0].text.strip()
                    if "```" in raw_ai:
                        raw_ai = raw_ai.split("```")[1]
                        if raw_ai.startswith("json"):
                            raw_ai = raw_ai[4:]
                    ai_terms = json.loads(raw_ai.strip())
                    if isinstance(ai_terms, list):
                        for ai_term in ai_terms[:4]:
                            if not isinstance(ai_term, str) or ai_term in tried_terms:
                                continue
                            tried_terms.append(ai_term)
                            print(f"  [Companies Office] AI suggests: {ai_term!r}")
                            hit, alt_lines_last = _try_co_search(ai_term)
                            if hit:
                                if pspla_address and not _co_addr_ok(hit.get("address"), pspla_address):
                                    print(f"  [Companies Office] Address mismatch — skipping {hit['name']!r}"
                                          f" ({hit.get('address')}) vs PSPLA {pspla_address!r}")
                                    continue
                                result = hit
                                lines = alt_lines_last
                                print(f"  [Companies Office] Found via AI term {ai_term!r}: {hit['name']}")
                                break
                except Exception as _ai_e:
                    print(f"  [Companies Office] AI fallback error: {_ai_e}")

            # 3. If we have candidates but still no result, let AI pick from them
            if result is None:
                all_candidates = [l for l in lines if l.isupper() and len(l) > 5]
                all_candidates += [l for l in alt_lines_last if l.isupper() and len(l) > 5]
                all_candidates = list(dict.fromkeys(all_candidates))[:20]  # dedupe
                if all_candidates and pspla_address:
                    pick_prompt = f"""From these NZ Companies Office results, which best matches "{company_name}" with address near "{pspla_address}"?

Companies found:
{chr(10).join(all_candidates)}

Return ONLY JSON: {{"name": "best match or null", "address": null}}"""
                    pick_msg = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=100,
                        messages=[{"role": "user", "content": pick_prompt}]
                    )
                    raw_pick = pick_msg.content[0].text.strip()
                    if "```" in raw_pick:
                        raw_pick = raw_pick.split("```")[1]
                        if raw_pick.startswith("json"):
                            raw_pick = raw_pick[4:]
                    result = json.loads(raw_pick.strip())

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

        if result is None:
            return {"name": None, "address": None, "company_number": None, "nzbn": None,
                    "status": None, "successor_name": None, "directors": []}

        # Fetch directors + website from detail page — prefer successor if original is removed
        active_result = successor_result if successor_result else result
        directors = []
        co_website = None
        if active_result.get("company_number"):
            directors, co_website = _co_fetch_directors(active_result["company_number"], co_headers)
            if directors:
                print(f"  [Companies Office directors] {directors}")
            if co_website:
                print(f"  [Companies Office website] {co_website}")

        # Use trading name as the display name if the registered name is very different
        display_name = result.get("trading_name") or result.get("name")

        return {
            "name": display_name,
            "registered_name": result.get("name"),
            "trading_name": result.get("trading_name"),
            "address": result.get("address"),
            "company_number": result.get("company_number"),
            "nzbn": result.get("nzbn"),
            "status": result.get("status", "registered"),
            "incorporated": result.get("incorporated"),
            "successor_name": successor_result.get("name") if successor_result else None,
            "successor_number": successor_result.get("company_number") if successor_result else None,
            "successor_address": successor_result.get("address") if successor_result else None,
            "directors": directors,
            "website": co_website,
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


def get_company_by_id(company_id):
    """Return the full DB record for a company by its ID."""
    url = f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}&select=*&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(url, headers=headers)
        data = r.json()
        return data[0] if data else None
    except:
        return None


def get_company_by_facebook_url(fb_url):
    """Return existing DB record with this Facebook URL, or None."""
    if not fb_url:
        return None
    norm = normalise_fb_url(fb_url)
    url = f"{SUPABASE_URL}/rest/v1/Companies?facebook_url=eq.{requests.utils.quote(norm)}&select=*&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        r = requests.get(url, headers=headers)
        data = r.json()
        return data[0] if data else None
    except:
        return None


def patch_company(company_id, updates):
    """PATCH only the specified fields on an existing company record."""
    if not updates or not company_id:
        return False
    url = f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        r = requests.patch(url, headers=headers, json=updates)
        return r.status_code in [200, 204]
    except Exception as e:
        print(f"  [Patch error] {e}")
        return False


def enrich_existing_record(company_id, existing_record, new_data, source_label):
    """Patch null/empty fields in an existing record with data from a new search result.
    Only patches fields that are currently null/empty in the existing record.
    Returns dict of field names that were actually patched.
    """
    if not company_id or not existing_record:
        return {}

    # Fields eligible for enrichment (only fill in if currently blank)
    enrichable = [
        "facebook_url", "fb_followers", "fb_phone", "fb_email", "fb_address",
        "fb_description", "fb_category", "fb_rating",
        "fb_alarm_systems", "fb_cctv_cameras", "fb_alarm_monitoring",
        "phone", "email", "linkedin_url",
        "nzsa_member", "nzsa_member_name", "nzsa_accredited", "nzsa_grade",
        "nzsa_contact_name", "nzsa_phone", "nzsa_email", "nzsa_overview",
        "has_alarm_systems", "has_cctv_cameras", "has_alarm_monitoring",
        "google_rating", "google_reviews", "google_phone", "google_address",
    ]

    updates = {}
    for field in enrichable:
        new_val = new_data.get(field)
        existing_val = existing_record.get(field)
        # Skip if new value is None, empty string, or False (for bools we don't want to overwrite True with False)
        if new_val is None or new_val == "" or new_val is False:
            continue
        # Only patch if existing field is empty/null
        if existing_val is None or existing_val == "" or str(existing_val).lower() in ("none", "null"):
            updates[field] = new_val

    # Region: append if new region not already listed
    new_region = new_data.get("region")
    if new_region:
        existing_region = existing_record.get("region") or ""
        existing_list = [r.strip().lower() for r in existing_region.split(",") if r.strip()]
        if new_region.strip().lower() not in existing_list:
            updates["region"] = (existing_region + ", " + new_region).strip(", ")

    if updates:
        updates["last_checked"] = datetime.now(timezone.utc).isoformat()
        if patch_company(company_id, updates):
            field_list = [k for k in updates.keys() if k != "last_checked"]
            print(f"  [Enriched] {existing_record.get('company_name')} — added: {', '.join(field_list)}")
            write_audit("updated", str(company_id), existing_record.get("company_name", ""),
                        changes=f"Enriched from {source_label}: {', '.join(field_list)}",
                        triggered_by="search")
        return updates
    else:
        print(f"  [No new data] {existing_record.get('company_name')} already has all available fields")
        return {}


def _llm_confirm_same_company(existing_name, existing_region, existing_website,
                               new_name, new_region, new_fb_url, new_snippet):
    """Use LLM (Haiku) to determine if a newly found entity is the same company as an existing DB record.
    Returns dict: {same_company: bool, confidence: str, reason: str}
    """
    prompt = f"""You are checking whether a newly found company is the same as an existing database record.

EXISTING RECORD:
- Name: {existing_name}
- Region: {existing_region or 'unknown'}
- Website: {existing_website or 'unknown'}

NEWLY FOUND:
- Name: {new_name}
- Region: {new_region or 'unknown'}
- Facebook URL: {new_fb_url or 'unknown'}
- Snippet: {new_snippet or 'none'}

Are these the same company? Consider name variations (Ltd/Limited, trading vs registered), region consistency, and business context.

Respond with JSON only:
{{"same_company": true/false, "confidence": "high/medium/low", "reason": "one sentence"}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [LLM same-company check error] {e}")
        return {"same_company": False, "confidence": "low", "reason": str(e)}


def _find_and_enrich_existing(company_name, region, fb_url, website_domain, snippet, enrich_data, source_label):
    """
    Check if a company matching these details already exists in the DB.
    If found, enrich it with enrich_data and return True.
    If not found, return False (caller should create a new record).

    Checks in order:
      1. By FB URL (definite match)
      2. By website domain (definite match)
      3. By exact company name (definite match)
      4. By keyword fuzzy search + LLM confirmation (uncertain match)
    """
    # Belt-and-suspenders: if the snippet indicates an overseas entity, strip the
    # facebook_url from enrich_data so we don't pollute an existing NZ record with
    # a foreign Facebook page URL even if the company name or domain matched.
    if enrich_data.get("facebook_url") and _snippet_is_overseas(snippet or ""):
        print(f"  [Overseas guard] Dropping facebook_url from enrich_data for '{company_name}'")
        enrich_data = {k: v for k, v in enrich_data.items()
                       if k not in ("facebook_url", "fb_followers", "fb_phone", "fb_email",
                                    "fb_address", "fb_description", "fb_category", "fb_rating")}
        fb_url = None  # don't attempt FB-URL match either

    # 1. FB URL match
    if fb_url:
        existing = get_company_by_facebook_url(fb_url)
        if existing:
            print(f"  [Match by FB URL] → {existing.get('company_name')}")
            enrich_existing_record(existing["id"], existing, enrich_data, source_label)
            return True

    # 2. Domain match
    if website_domain and "facebook.com" not in website_domain:
        existing = get_domain_record(website_domain)
        if existing:
            print(f"  [Match by domain] {website_domain} → {existing.get('company_name')}")
            enrich_existing_record(existing["id"], existing, enrich_data, source_label)
            return True

    # 3. Exact name match
    if company_name:
        existing_stub = get_company_by_name(company_name)
        if existing_stub:
            existing = get_company_by_id(existing_stub["id"])
            if existing:
                print(f"  [Match by name] {company_name}")
                enrich_existing_record(existing["id"], existing, enrich_data, source_label)
                return True

    # 4. Fuzzy name match → LLM confirmation
    if company_name:
        stop_words = {"security", "systems", "limited", "ltd", "nz", "new", "zealand",
                      "alarm", "alarms", "company", "services", "solutions", "group",
                      "protection", "surveillance", "monitoring", "camera", "cameras"}
        keywords = [w for w in company_name.split() if len(w) >= 4 and w.lower() not in stop_words]
        for keyword in keywords[:2]:  # try at most 2 keywords
            search_url = (f"{SUPABASE_URL}/rest/v1/Companies"
                          f"?company_name=ilike.{requests.utils.quote('%' + keyword + '%')}"
                          f"&select=id,company_name,region,website_url&limit=3")
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            try:
                r = requests.get(search_url, headers=headers)
                candidates = r.json() if r.ok else []
            except:
                candidates = []

            for candidate in candidates:
                cname = candidate.get("company_name", "")
                if cname.lower() == company_name.lower():
                    continue  # exact match already handled above
                llm_result = _llm_confirm_same_company(
                    cname, candidate.get("region", ""), candidate.get("website_url", ""),
                    company_name, region, fb_url, snippet
                )
                if llm_result.get("same_company") and llm_result.get("confidence") in ("high", "medium"):
                    print(f"  [LLM match] '{company_name}' → '{cname}' "
                          f"({llm_result['confidence']}: {llm_result.get('reason', '')})")
                    full_existing = get_company_by_id(candidate["id"])
                    if full_existing:
                        enrich_existing_record(full_existing["id"], full_existing, enrich_data, source_label)
                        return True
                else:
                    print(f"  [LLM: different] '{company_name}' vs '{cname}': {llm_result.get('reason', '')}")
            if candidates:
                break  # found candidates for first keyword, don't try second

    return False


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
        # Require ALL words present (AND) to avoid false matches on first name only
        words = [w for w in name.split() if len(w) >= 2]
        q = " AND ".join(f"name_txt:{w}" for w in words) if words else f"name_txt:({name})"
        params = {
            "rows": "5",
            "fl": "*, score",
            "sort": "score desc",
            "json.nl": "map",
            "q": q,
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
            # Verify the last name of the input appears in the matched name
            last_name = name.split()[-1].lower()
            if last_name not in name_field.lower():
                return {"found": False, "name": None}
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
    # Strip "trading as" abbreviations before anything else — t/a and t/as should
    # never be treated as matching tokens.
    n = _re.sub(r'\bt/as?\b', ' ', n)
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
    # "electrical", "plumbing", "construction" etc. are industry-generic — many unrelated
    # companies share these words, so they must NOT count as a significant match signal.
    _GENERIC = {"security", "services", "solutions", "systems", "alarm", "alarms",
                "group", "install", "camera", "cctv", "surveillance", "protection",
                "management", "response", "patrol", "guard", "monitoring",
                "electrical", "plumbing", "construction", "building", "engineering",
                "contracting", "contractors", "maintenance", "support", "technology",
                "technologies", "consulting", "consultants"}

    # Free/generic email providers — domain match against these is meaningless
    _FREE_EMAIL = {"gmail.com", "hotmail.com", "yahoo.com", "outlook.com",
                   "xtra.co.nz", "yahoo.co.nz", "icloud.com", "live.com",
                   "me.com", "msn.com"}

    def _email_domain(s):
        """Extract domain from an email address or URL."""
        s = (s or "").lower().strip()
        if "@" in s:
            return s.split("@")[-1]
        d = _re.sub(r'^https?://(www\.)?', '', s).rstrip("/")
        return d.split("/")[0] if d else ""

    # Require significant words to be at least 2 chars — single-letter fragments
    # (e.g. "t" from "t/a" after slash stripping) must never drive a match.
    sig_words = {w for w in (query_words - _GENERIC) if len(w) >= 2}

    best_score = 0
    best_member = None
    best_sig_common = set()

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
        score = len(sig_common) * 3 + len(common - sig_common)
        # Require at least 1 significant word match
        if sig_common:
            if score >= best_score:
                best_score = score
                best_member = m
                best_sig_common = sig_common

    # Require score >= 3 (at least one significant word match)
    if best_member and best_score >= 3:
        # Domain mismatch guard: if the company website domain and the NZSA member's
        # email/website domain are both company-specific and clearly different, reject.
        # This catches "Livewire Electrical" matching "Sefton Electrical" via shared
        # generic industry word when domains are completely unrelated.
        # Skip the check if the stored website is a directory/aggregator site — it's
        # not the company's own domain so a mismatch against it is meaningless.
        _DIRECTORY_DOMAINS = {
            "moneyhub.co.nz", "yellowpages.co.nz", "localist.co.nz", "finda.co.nz",
            "neighbourly.co.nz", "nzpages.co.nz", "truelocal.co.nz", "google.com",
            "facebook.com", "linkedin.com", "trademe.co.nz", "nowhereelse.co.nz",
            "aucklandnz.com", "wellingtonnz.com", "zomato.com", "yelp.com",
        }
        company_dom = _email_domain(website) if website else ""
        member_dom = (_email_domain(best_member.get("email") or "")
                      or _email_domain(best_member.get("website") or ""))

        # When the domain check is bypassed (directory/no website), word matching is
        # the only guard. Require either 2+ significant word hits OR a single word
        # that is long enough to be uniquely identifying (>= 6 chars).
        # This blocks short colour/adjective words like "red", "blue" from creating
        # false matches between unrelated companies.
        is_directory = (not company_dom or company_dom in _DIRECTORY_DOMAINS)
        if is_directory:
            strong_single = any(len(w) >= 6 for w in best_sig_common)
            if not (len(best_sig_common) >= 2 or strong_single):
                print(f"  [NZSA] Weak match rejected (directory website, sig={best_sig_common}): '{best_member['name']}'")
                return {"member": False, "member_name": None, "accredited": False, "grade": None,
                        "contact_name": None, "phone": None, "email": None, "overview": None}

        if (company_dom and member_dom
                and company_dom not in _FREE_EMAIL
                and company_dom not in _DIRECTORY_DOMAINS
                and member_dom not in _FREE_EMAIL
                and company_dom != member_dom):
            print(f"  [NZSA] Domain mismatch: company={company_dom}, member={member_dom} — rejecting '{best_member['name']}'")
            return {"member": False, "member_name": None, "accredited": False, "grade": None,
                    "contact_name": None, "phone": None, "email": None, "overview": None}
        return _member_hit(best_member)

    return {"member": False, "member_name": None, "accredited": False, "grade": None,
            "contact_name": None, "phone": None, "email": None, "overview": None}


PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pause.flag")
RUNNING_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "running.flag")
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_progress.json")
FB_PROGRESS_FILE = os.path.join(BASE_DIR, "facebook_progress.json")
DIR_PROGRESS_FILE = os.path.join(BASE_DIR, "directory_progress.json")
PARTIAL_PROGRESS_FILE = os.path.join(BASE_DIR, "partial_progress.json")
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
    "pspla_license_classes": None,
    "pspla_license_start": None,
    "pspla_permit_type": None,
    "co_status": None,
    "co_incorporated": None,
    "co_website": None,
    "email_source": None,
    "phone_source": None,
    "date_added": None,
    "fb_followers": None,
    "fb_phone": None,
    "fb_email": None,
    "fb_address": None,
    "fb_description": None,
    "fb_category": None,
    "fb_rating": None,
    "google_rating": None,
    "google_reviews": None,
    "google_phone": None,
    "google_address": None,
    "google_email": None,
    "linkedin_followers": None,
    "linkedin_description": None,
    "linkedin_industry": None,
    "linkedin_location": None,
    "linkedin_website": None,
    "linkedin_size": None,
    "root_domain": None,
    "source_url": None,
    "last_checked": None,
    "notes": None,
    "has_alarm_systems": None,
    "has_cctv_cameras": None,
    "has_alarm_monitoring": None,
    "fb_alarm_systems": None,
    "fb_cctv_cameras": None,
    "fb_alarm_monitoring": None,
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


# ── Facebook progress ──────────────────────────────────────────────────────────
def load_fb_progress():
    if os.path.exists(FB_PROGRESS_FILE):
        with open(FB_PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_regions": [], "nationwide_done": False, "total_found": 0, "total_new": 0}

def save_fb_progress(completed_regions, nationwide_done, total_found, total_new):
    with open(FB_PROGRESS_FILE, "w") as f:
        json.dump({"completed_regions": completed_regions, "nationwide_done": nationwide_done,
                   "total_found": total_found, "total_new": total_new}, f)

def clear_fb_progress():
    if os.path.exists(FB_PROGRESS_FILE):
        os.remove(FB_PROGRESS_FILE)


# ── Directory progress ─────────────────────────────────────────────────────────
def load_dir_progress():
    if os.path.exists(DIR_PROGRESS_FILE):
        with open(DIR_PROGRESS_FILE) as f:
            return json.load(f)
    return {"nzsa_last_idx": -1, "nzsa_done": False, "linkedin_done_indices": [], "linkedin_done": False}

def save_dir_progress(data):
    with open(DIR_PROGRESS_FILE, "w") as f:
        json.dump(data, f)

def clear_dir_progress():
    if os.path.exists(DIR_PROGRESS_FILE):
        os.remove(DIR_PROGRESS_FILE)


# ── Partial progress ───────────────────────────────────────────────────────────
def load_partial_progress():
    if os.path.exists(PARTIAL_PROGRESS_FILE):
        with open(PARTIAL_PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_regions": [], "google_done": False, "fb_done": False}

def save_partial_progress(data):
    with open(PARTIAL_PROGRESS_FILE, "w") as f:
        json.dump(data, f)

def clear_partial_progress():
    if os.path.exists(PARTIAL_PROGRESS_FILE):
        os.remove(PARTIAL_PROGRESS_FILE)


def get_all_progress():
    """Return progress summaries for all search types (for dashboard display)."""
    out = {}
    if os.path.exists(FB_PROGRESS_FILE):
        p = load_fb_progress()
        done = len(p.get("completed_regions", []))
        total = len(NZ_REGIONS) + 1  # regions + nationwide
        out["facebook"] = {"done": done, "total": total, "nationwide_done": p.get("nationwide_done", False)}
    else:
        out["facebook"] = None
    if os.path.exists(DIR_PROGRESS_FILE):
        p = load_dir_progress()
        out["directory"] = {
            "nzsa_done": p.get("nzsa_done", False),
            "nzsa_last_idx": p.get("nzsa_last_idx", -1),
            "linkedin_done": p.get("linkedin_done", False),
            "linkedin_queries_done": len(p.get("linkedin_done_indices", [])),
            "linkedin_total": len(_LINKEDIN_IMPORT_QUERIES),
        }
    else:
        out["directory"] = None
    if os.path.exists(PARTIAL_PROGRESS_FILE):
        p = load_partial_progress()
        out["partial"] = {
            "completed_regions": len(p.get("completed_regions", [])),
            "google_done": p.get("google_done", False),
            "fb_done": p.get("fb_done", False),
        }
    else:
        out["partial"] = None
    return out


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


def record_search_start(run_type, started_iso, triggered_by):
    """Write a 'running' sentinel to history at search start.
    If the process crashes without calling append_history(), this entry remains
    visible in the history so you can see the search started but never finished."""
    record = {
        "type": run_type,
        "started": started_iso,
        "finished": None,
        "duration_minutes": None,
        "total_found": 0,
        "total_new": 0,
        "status": "running",
        "triggered_by": triggered_by,
        "notes": "",
    }
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    # Remove any stale "running" sentinels from previous crashed sessions
    history = [h for h in history if h.get("status") != "running"]
    history.insert(0, record)
    history = history[:100]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"  [History start write error] {e}")


def append_history(run_type, started_iso, total_found, total_new, status="completed", triggered_by="manual", notes=""):
    """Append a run record to search_history.json (newest first, capped at 100).
    Replaces any 'running' sentinel written by record_search_start() for the same started_iso."""
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
        "notes": notes,
    }
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    # Remove the "running" sentinel for this run (same started_iso) and any stale ones
    history = [h for h in history if not (h.get("status") == "running")]
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

    # Scrape Facebook page for contact/profile data if we have a URL
    fb_page_data = {}
    if info.get("facebook_url"):
        print(f"  [Facebook scrape] {info['facebook_url']}")
        fb_page_data = scrape_facebook_page(info["facebook_url"], company_name=company_name)
        if any(v for v in fb_page_data.values()):
            print(f"  [Facebook data] followers={fb_page_data.get('followers')} "
                  f"phone={fb_page_data.get('phone')} email={fb_page_data.get('email')}")
        # Backfill email if we don't have one yet
        if not info.get("email") and fb_page_data.get("email"):
            info["email"] = fb_page_data["email"]
            info["_email_source"] = "facebook"
            print(f"  [Email from FB] {info['email']}")

    # Detect services from Facebook content (description + category + search snippet)
    fb_service_text = " ".join(filter(None, [
        fb_page_data.get("description"),
        fb_page_data.get("category"),
        info.get("_fb_snippet", ""),
    ]))
    fb_services = detect_services(fb_service_text) if fb_service_text.strip() else \
        {"has_alarm_systems": False, "has_cctv_cameras": False, "has_alarm_monitoring": False}

    # Fetch Google Business Profile (rating, reviews, phone, address from knowledge graph)
    print(f"  [Google profile] Looking up: {company_name}")
    google_profile = get_google_business_profile(company_name, website_region)
    # Backfill phone/email if we don't have one yet
    if not info.get("phone") and google_profile.get("phone"):
        info["phone"] = google_profile["phone"]
        info["_phone_source"] = "google"
        print(f"  [Phone from Google] {info['phone']}")
    if not info.get("email") and google_profile.get("email"):
        info["email"] = google_profile["email"]
        info["_email_source"] = "google"
        print(f"  [Email from Google] {info['email']}")

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
    # Build Facebook context: prefer scraped description over snippet
    fb_context_text = (fb_page_data.get("description")
                       or fb_page_data.get("category")
                       or info.get("_fb_snippet", ""))
    extra_context = {
        "facebook_snippet": fb_context_text,
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

    # Scrape LinkedIn page for followers/description if we have a URL
    li_page_data = {}
    if info.get("linkedin_url"):
        li_page_data = scrape_linkedin_page(info["linkedin_url"], company_name=company_name)
        if any(v for v in li_page_data.values()):
            print(f"  [LinkedIn data] followers={li_page_data.get('followers')} desc={bool(li_page_data.get('description'))}")

    # LLM pre-save review: sanity-check ALL gathered associations before saving
    rejections = llm_verify_associations(
        company_name, website_url, website_region,
        linkedin_url=info.get("linkedin_url"),
        facebook_url=info.get("facebook_url"),
        co_name=co_result.get("name"),
        co_address=co_result.get("address"),
        pspla_name=pspla_result.get("matched_name"),
        pspla_address=pspla_result.get("pspla_address"),
        pspla_status=pspla_result.get("pspla_license_status"),
        nzsa_name=nzsa_result.get("member_name") if nzsa_result.get("member") else None,
        google_address=google_profile.get("address"),
        google_phone=google_profile.get("phone"),
        fb_description=fb_page_data.get("description"),
    )
    if rejections.get("linkedin_url"):
        write_audit("llm_decision", None, company_name,
                    changes=f"LinkedIn rejected: {rejections['linkedin_url']}",
                    triggered_by="llm_verify_associations")
        info["linkedin_url"] = None
        li_page_data = {}
    if rejections.get("facebook_url"):
        write_audit("llm_decision", None, company_name,
                    changes=f"Facebook rejected: {rejections['facebook_url']}",
                    triggered_by="llm_verify_associations")
        info["facebook_url"] = None
        fb_page_data = {}
    if rejections.get("co_name"):
        write_audit("llm_decision", None, company_name,
                    changes=f"CO name rejected: {rejections['co_name']}",
                    triggered_by="llm_verify_associations")
        co_result = {}
    if rejections.get("pspla_name"):
        write_audit("llm_decision", None, company_name,
                    changes=f"PSPLA rejected: {rejections['pspla_name']}",
                    triggered_by="llm_verify_associations")
        pspla_result = {"licensed": False, "matched_name": None, "license_type": None,
                        "match_method": f"rejected by pre-save review: {rejections['pspla_name']}",
                        "pspla_address": None, "pspla_license_number": None,
                        "pspla_license_status": None, "pspla_license_expiry": None}
    if rejections.get("nzsa_name"):
        write_audit("llm_decision", None, company_name,
                    changes=f"NZSA rejected: {rejections['nzsa_name']}",
                    triggered_by="llm_verify_associations")
        nzsa_result = {"member": False, "member_name": None, "accredited": False,
                       "grade": None, "contact_name": None, "phone": None,
                       "email": None, "overview": None}
    if rejections.get("google_profile"):
        write_audit("llm_decision", None, company_name,
                    changes=f"Google profile rejected: {rejections['google_profile']}",
                    triggered_by="llm_verify_associations")
        google_profile = {}

    # Detect services mentioned on the company website (homepage + service sub-pages)
    service_text = gather_service_text(website_url, page_text) if website_url else page_text
    services = detect_services(service_text)
    detected = [k for k, v in services.items() if v]
    if detected:
        print(f"  [Services detected] {', '.join(detected)}")

    record = {
        "company_name": company_name,
        "website": website_url,
        "phone": info.get("phone"),
        "email": info.get("email"),
        "email_source": info.get("_email_source") or ("website" if info.get("email") else None),
        "phone_source": info.get("_phone_source") or ("website" if info.get("phone") else None),
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
        "companies_office_name": co_result.get("registered_name") or co_result.get("name"),
        "companies_office_address": co_result.get("address"),
        "companies_office_number": co_result.get("company_number"),
        "nzbn": co_result.get("nzbn"),
        "co_status": co_result.get("status"),
        "co_incorporated": co_result.get("incorporated"),
        "co_website": co_result.get("website"),
        "pspla_license_classes": pspla_result.get("pspla_license_classes"),
        "pspla_license_start": pspla_result.get("pspla_license_start"),
        "pspla_permit_type": pspla_result.get("pspla_permit_type"),
        "date_added": datetime.now(timezone.utc).isoformat(),
        "individual_license": individual_license_found,
        "director_name": ", ".join(directors),
        "facebook_url": info.get("facebook_url") or (
            info.get("_fb_url") if info.get("_fb_url") and "facebook.com" in (info.get("_fb_url") or "") else None
        ),
        "fb_followers": fb_page_data.get("followers"),
        "fb_phone": fb_page_data.get("phone"),
        "fb_email": fb_page_data.get("email"),
        "fb_address": fb_page_data.get("address"),
        "fb_description": fb_page_data.get("description"),
        "fb_category": fb_page_data.get("category"),
        "fb_rating": fb_page_data.get("rating"),
        "google_rating": google_profile.get("rating"),
        "google_reviews": google_profile.get("reviews"),
        "google_phone": google_profile.get("phone"),
        "google_address": google_profile.get("address"),
        "google_email": google_profile.get("email"),
        "linkedin_url": info.get("linkedin_url"),
        "linkedin_followers": li_page_data.get("followers"),
        "linkedin_description": li_page_data.get("description"),
        "linkedin_industry": li_page_data.get("industry"),
        "linkedin_location": li_page_data.get("location"),
        "linkedin_website": li_page_data.get("website"),
        "linkedin_size": li_page_data.get("size"),
        "nzsa_member": "true" if nzsa_result["member"] else "false",
        "nzsa_member_name": nzsa_result["member_name"],
        "nzsa_accredited": "true" if nzsa_result["accredited"] else "false",
        "nzsa_grade": nzsa_result["grade"],
        "nzsa_contact_name": nzsa_result.get("contact_name") or None,
        "nzsa_phone": nzsa_result.get("phone") or None,
        "nzsa_email": nzsa_result.get("email") or None,
        "nzsa_overview": nzsa_result.get("overview") or None,
        "root_domain": root_domain,
        "source_url": info.get("_fb_post_url") or info.get("_fb_url") or website_url,
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "notes": f"Found via: {source_label}",
        "has_alarm_systems":    services.get("has_alarm_systems"),
        "has_cctv_cameras":     services.get("has_cctv_cameras"),
        "has_alarm_monitoring": services.get("has_alarm_monitoring"),
        "fb_alarm_systems":     fb_services.get("has_alarm_systems"),
        "fb_cctv_cameras":      fb_services.get("has_cctv_cameras"),
        "fb_alarm_monitoring":  fb_services.get("has_alarm_monitoring"),
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


# Sub-paths that indicate a content URL rather than a company page root.
_FB_CONTENT_PATHS = {"posts", "photos", "videos", "events", "about",
                     "reviews", "community", "reels", "stories"}
# Sub-paths that are never a company page at all.
_FB_SKIP_SECTIONS = {"groups", "marketplace", "watch"}


def fb_page_url_from_result_link(link):
    """Normalise any Facebook result URL to a page home URL.

    - Strips query string and trailing slash.
    - Normalises m.facebook.com → www.facebook.com.
    - Hard-rejects groups/marketplace/watch — never company pages.
    - If the URL is a content URL (/posts/, /photos/ etc.), extracts the
      base page URL and returns it along with the original URL.
    - Returns (page_url, original_url_if_content_page) or (None, None).

    original_url_if_content_page is non-None only when the link was a
    content/post URL — callers should store it as the discovery source so
    the exact post that triggered company discovery is traceable.
    """
    import re as _re
    link = link.split("?")[0].rstrip("/")
    link = _re.sub(r"^https?://m\.facebook\.com", "https://www.facebook.com", link)

    if _re.search(r"facebook\.com/(" + "|".join(_FB_SKIP_SECTIONS) + r")/", link):
        return None, None

    # Numeric-only IDs are unusable
    if _re.match(r"https?://(www\.)?facebook\.com/\d+$", link):
        return None, None

    # Content sub-path → extract base page, keep original for traceability
    content_re = (r"(https?://(www\.)?facebook\.com"
                  r"/(?:(?:p|people)/)?[^/?#\s]+)"
                  r"/(" + "|".join(_FB_CONTENT_PATHS) + r")(?:/|$)")
    cm = _re.match(content_re, link)
    if cm:
        return cm.group(1), link   # (page_url, original_post_url)

    return link, None


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
    fb_url = result["link"]        # original result URL (may be a post/content URL)

    if "facebook.com" not in fb_url:
        return fb_total, fb_new

    # Normalise to page URL. If the result is a content/post URL (e.g. /posts/123),
    # extract the base page URL and remember the original so we can store it as the
    # discovery source — letting us trace exactly which post triggered the find.
    fb_url_norm, original_post_url = fb_page_url_from_result_link(fb_url)
    if not fb_url_norm:
        return fb_total, fb_new

    # Hard-filter results whose snippet/title clearly indicate a non-NZ entity.
    if _snippet_is_overseas(result.get("snippet", ""), result.get("title", "")):
        print(f"  [Skipped - overseas signal] {fb_url}")
        return fb_total, fb_new

    fb_url_norm = normalise_fb_url(fb_url_norm)
    if fb_url_norm in found_urls_all:
        return fb_total, fb_new
    found_urls_all.add(fb_url_norm)

    print(f"  [FB Page] {fb_url_norm}")
    fb_total += 1

    # Try to find this company's real website from the FB page
    website_url = extract_website_from_facebook(fb_url_norm)
    if not website_url or "facebook.com" in website_url:
        website_url = extract_website_from_snippet(result.get("snippet", ""))
    time.sleep(1)

    root_domain = None
    page_text = ""
    scraped_email = None
    scraped_facebook = None
    scraped_linkedin = None
    info = None

    if website_url and "facebook.com" not in website_url and website_url not in found_urls_all:
        found_urls_all.add(website_url)
        root_domain = get_root_domain(website_url)
        page_text, scraped_email, scraped_facebook, scraped_linkedin = scrape_website(website_url)
        time.sleep(1)
        info = extract_company_info(website_url, page_text, result["snippet"])

    if not info or not info.get("company_name"):
        info = extract_from_fb_snippet(result["title"], result["snippet"], fb_url_norm, fallback_region)

    if not info or not info.get("company_name"):
        print("  [Skipped] Could not extract company name")
        return fb_total, fb_new

    if not root_domain:
        root_domain = get_root_domain(fb_url_norm)
        website_url = fb_url_norm

    # Fill in any missing contact info
    if scraped_email and not info.get("email"):
        info["email"] = scraped_email
    if scraped_facebook:
        info["facebook_url"] = scraped_facebook
    if scraped_linkedin:
        info["linkedin_url"] = scraped_linkedin
    if not info.get("email") and root_domain and "facebook.com" not in root_domain:
        found_email = find_email_via_google(root_domain)
        if found_email:
            info["email"] = found_email

    info["_page_text"] = page_text
    info["_fb_snippet"] = result.get("snippet", "")
    info["_fb_url"] = fb_url_norm
    # original_post_url is set when Google returned a content/post URL (e.g. /posts/123).
    # Store it as the discovery source so the exact post is traceable.
    info["_fb_post_url"] = original_post_url
    if fb_url_norm and info["_fb_snippet"]:
        _FB_SNIPPET_CACHE[fb_url_norm] = info["_fb_snippet"]

    # Scrape FB page for contact/profile data (used for both enrichment and new records)
    fb_page_data = scrape_facebook_page(fb_url_norm, company_name=info.get("company_name", ""))
    if any(v for v in fb_page_data.values()):
        print(f"  [Facebook data] followers={fb_page_data.get('followers')} "
              f"phone={fb_page_data.get('phone')} email={fb_page_data.get('email')}")

    source_label = f"Facebook {term} {fallback_region}".strip()
    company_name = info.get("company_name", "")
    region = info.get("region") or fallback_region

    # Build enrichment data from everything we've gathered
    enrich_data = {
        "facebook_url": fb_url_norm,
        "fb_followers": fb_page_data.get("followers"),
        "fb_phone": fb_page_data.get("phone"),
        "fb_email": fb_page_data.get("email"),
        "fb_address": fb_page_data.get("address"),
        "fb_description": fb_page_data.get("description"),
        "fb_category": fb_page_data.get("category"),
        "fb_rating": fb_page_data.get("rating"),
        "phone": info.get("phone") or fb_page_data.get("phone"),
        "phone_source": ("website" if info.get("phone") else ("facebook" if fb_page_data.get("phone") else None)),
        "email": info.get("email") or fb_page_data.get("email"),
        "email_source": ("website" if info.get("email") else ("facebook" if fb_page_data.get("email") else None)),
        "region": region,
    }

    # Check if this company already exists → enrich instead of creating duplicate
    if _find_and_enrich_existing(company_name, region, fb_url_norm, root_domain,
                                  result.get("snippet", ""), enrich_data, source_label):
        return fb_total, fb_new

    # Not in DB — run full pipeline and save as new record
    if process_and_save_company(info, website_url, root_domain, source_label, fallback_region):
        fb_new += 1

    return fb_total, fb_new


def run_facebook_search(found_urls_all, regions=None, include_nationwide=True, fresh=False, track_progress=True):
    """Search Facebook for NZ security camera companies. Returns (total_found, total_new).
    - regions: subset of NZ_REGIONS to search; defaults to all.
    - include_nationwide: also run a 'New Zealand' wide pass to catch businesses
      that don't mention a specific town on their Facebook page.
    - fresh: if True, clear any saved FB progress before starting.
    - track_progress: if False (e.g. called from run_partial), skip FB progress file entirely."""
    search_regions = regions if regions is not None else NZ_REGIONS
    print("\n" + "=" * 60)
    print("  Facebook Search Pass")
    print("=" * 60)

    # Load/clear progress (only when running as standalone, not when called from partial)
    if track_progress:
        if fresh:
            clear_fb_progress()
        progress = load_fb_progress()
        completed_regions = progress.get("completed_regions", [])
        nationwide_done = progress.get("nationwide_done", False)
        fb_total = progress.get("total_found", 0)
        fb_new = progress.get("total_new", 0)
        if completed_regions:
            print(f"  Resuming — {len(completed_regions)}/{len(search_regions)} regions already done: {', '.join(completed_regions)}")
        if nationwide_done:
            print("  Nationwide pass already done — will skip")
    else:
        completed_regions = []
        nationwide_done = False
        fb_total = 0
        fb_new = 0

    for region in search_regions:
        if track_progress and region in completed_regions:
            print(f"  [Skipping] {region} — already done")
            continue
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

        if track_progress:
            completed_regions.append(region)
            save_fb_progress(completed_regions, nationwide_done, fb_total, fb_new)
            print(f"  [Progress saved] {region} done ({len(completed_regions)}/{len(search_regions)} regions)")

    # Nationwide pass — catches NZ businesses that don't mention a specific town
    if include_nationwide:
        if track_progress and nationwide_done:
            print("  [Skipping] Nationwide pass — already done")
        else:
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

            if track_progress:
                nationwide_done = True
                save_fb_progress(completed_regions, nationwide_done, fb_total, fb_new)
                print("  [Progress saved] Nationwide pass done")

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
        import traceback as _tb_email
        tb = _tb_email.format_exc()
        print(f"  [Email error] {e}")
        print(tb)
        try:
            write_audit("email", None, "Email send FAILED",
                        changes=f"Subject: {subject}  Error: {e}",
                        triggered_by=triggered_by,
                        notes=tb[:800])
        except Exception:
            pass


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


def run_nzsa_import(found_urls=None, limit=None, fresh=False):
    """Import all NZSA members not already in the database.
    limit: stop after processing this many new candidates (for testing).
    fresh: if True, clear any saved directory progress before starting.
    Returns (total_found, total_new)."""
    if found_urls is None:
        found_urls = set()

    print("\n" + "=" * 60)
    print("  NZSA Directory Import")
    print("=" * 60)

    members = _get_nzsa_members()
    print(f"  {len(members)} NZSA members loaded")

    if fresh:
        clear_dir_progress()
    dir_progress = load_dir_progress()
    if dir_progress.get("nzsa_done"):
        print("  [Skipping] NZSA import already completed in a previous run.")
        return 0, 0
    nzsa_last_idx = dir_progress.get("nzsa_last_idx", -1)
    if nzsa_last_idx >= 0:
        print(f"  Resuming from member index {nzsa_last_idx + 1} (skipping {nzsa_last_idx + 1} already processed)")

    total_found = 0
    total_new = 0
    processed = 0

    for idx, m in enumerate(members):
        check_pause()
        if idx <= nzsa_last_idx:
            continue
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
            existing_by_domain = get_domain_record(root_domain) if root_domain not in found_urls else None
            if root_domain in found_urls or existing_by_domain:
                if existing_by_domain:
                    print(f"  [Domain match] {name} ({root_domain}) — checking for enrichment")
                    nzsa_enrich = {
                        "nzsa_member": "true",
                        "nzsa_member_name": name,
                        "nzsa_accredited": "true" if m.get("accredited") else None,
                        "nzsa_grade": m.get("grade"),
                        "nzsa_contact_name": m.get("contact_name"),
                        "nzsa_phone": m.get("phone"),
                        "nzsa_email": m.get("email"),
                        "nzsa_overview": m.get("overview"),
                        "phone": m.get("phone"),
                        "email": m.get("email"),
                    }
                    enrich_existing_record(existing_by_domain["id"], existing_by_domain, nzsa_enrich, "NZSA directory")
                else:
                    print(f"  [Already in DB] {name} ({root_domain})")
                continue
            if company_name_exists(name):
                print(f"  [Already in DB by name] {name} — checking for enrichment")
                existing_stub = get_company_by_name(name)
                if existing_stub:
                    full_existing = get_company_by_id(existing_stub["id"])
                    if full_existing:
                        nzsa_enrich = {
                            "nzsa_member": "true",
                            "nzsa_member_name": name,
                            "nzsa_accredited": "true" if m.get("accredited") else None,
                            "nzsa_grade": m.get("grade"),
                            "nzsa_contact_name": m.get("contact_name"),
                            "nzsa_phone": m.get("phone"),
                            "nzsa_email": m.get("email"),
                            "nzsa_overview": m.get("overview"),
                            "phone": m.get("phone"),
                            "email": m.get("email"),
                            "region": (m.get("locations") or [""])[0],
                        }
                        enrich_existing_record(full_existing["id"], full_existing, nzsa_enrich, "NZSA directory")
                if root_domain:
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
                print(f"  [Already in DB by name] {name} — checking for enrichment")
                existing_stub = get_company_by_name(name)
                if existing_stub:
                    full_existing = get_company_by_id(existing_stub["id"])
                    if full_existing:
                        nzsa_enrich = {
                            "nzsa_member": "true",
                            "nzsa_member_name": name,
                            "nzsa_accredited": "true" if m.get("accredited") else None,
                            "nzsa_grade": m.get("grade"),
                            "nzsa_contact_name": m.get("contact_name"),
                            "nzsa_phone": m.get("phone"),
                            "nzsa_email": m.get("email"),
                            "nzsa_overview": m.get("overview"),
                            "phone": m.get("phone"),
                            "email": m.get("email"),
                            "region": (m.get("locations") or [""])[0],
                        }
                        enrich_existing_record(full_existing["id"], full_existing, nzsa_enrich, "NZSA directory")
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

        # Save progress every 10 members
        if (idx + 1) % 10 == 0:
            dir_progress["nzsa_last_idx"] = idx
            save_dir_progress(dir_progress)

    dir_progress["nzsa_done"] = True
    dir_progress["nzsa_last_idx"] = len(members) - 1
    save_dir_progress(dir_progress)

    print(f"\n  NZSA import complete. Found: {total_found}, New: {total_new}")
    return total_found, total_new


def run_linkedin_import(found_urls=None, limit=None, fresh=False):
    """Search LinkedIn via Google for NZ security companies not already in the database.
    limit: stop after processing this many new candidates (for testing).
    fresh: if True, clear any saved directory progress before starting.
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

    dir_progress = load_dir_progress()
    if dir_progress.get("linkedin_done"):
        print("  [Skipping] LinkedIn import already completed in a previous run.")
        return 0, 0
    linkedin_done_indices = set(dir_progress.get("linkedin_done_indices", []))
    if linkedin_done_indices:
        print(f"  Resuming — {len(linkedin_done_indices)}/{len(_LINKEDIN_IMPORT_QUERIES)} queries already done")

    for q_idx, query in enumerate(_LINKEDIN_IMPORT_QUERIES):
        if q_idx in linkedin_done_indices:
            print(f"  [Skipping] Query {q_idx+1} — already done")
            continue
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
                print(f"  [Already in DB by name] {company_name_guess} — adding LinkedIn URL")
                existing_stub = get_company_by_name(company_name_guess)
                if existing_stub:
                    full_existing = get_company_by_id(existing_stub["id"])
                    if full_existing:
                        enrich_existing_record(full_existing["id"], full_existing,
                                               {"linkedin_url": li_norm}, "LinkedIn directory")
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
            existing_by_domain = get_domain_record(root_domain) if root_domain not in found_urls else None
            if root_domain in found_urls or existing_by_domain:
                if existing_by_domain:
                    print(f"  [Domain match] {root_domain} — adding LinkedIn URL")
                    enrich_existing_record(existing_by_domain["id"], existing_by_domain,
                                           {"linkedin_url": li_norm}, "LinkedIn directory")
                else:
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

        linkedin_done_indices.add(q_idx)
        dir_progress["linkedin_done_indices"] = list(linkedin_done_indices)
        save_dir_progress(dir_progress)
        print(f"  [Progress saved] Query {q_idx+1}/{len(_LINKEDIN_IMPORT_QUERIES)} done")

    dir_progress["linkedin_done"] = True
    save_dir_progress(dir_progress)

    print(f"\n  LinkedIn import complete. Found: {total_found}, New: {total_new}")
    return total_found, total_new


def run_search(triggered_by="manual"):
    print("=" * 60)
    print("  PSPLA Security Camera Company Checker")
    print("=" * 60)
    started_iso = datetime.now(timezone.utc).isoformat()
    record_search_start("full", started_iso, triggered_by)
    reset_session_log()
    reset_token_usage()

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

    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        print(f"\n  [CRASH] Unhandled exception in run_search: {e}")
        print(tb)
        append_history("full", started_iso, total_found, total_new,
                       f"error: {type(e).__name__}: {e}", triggered_by,
                       notes=tb[:1500])
        raise

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
