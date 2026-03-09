# PSPLA Checker — Full Project Context for Claude Code

## What This Project Does
Automated tool that finds NZ security companies on the internet, checks whether each one holds a current PSPLA (Private Security Personnel Licensing Authority) licence, and stores results in Supabase. A Flask dashboard lets the user browse results, trigger searches, review AI decisions, and correct errors.

Owned and operated by Wade. The goal is to build a comprehensive list of every NZ private security company and whether they are licenced.

---

## Tech Stack
- **Python** — Flask dashboard + searcher engine
- **Supabase** — PostgreSQL database, accessed via REST API with `apikey` header
- **Anthropic Claude API** — Haiku for fast tasks, Sonnet for deep verification
- **SerpAPI** — Google search wrapper (all web searches go through this, paid per query)
- **APScheduler** — scheduled runs inside dashboard.py process
- **Windows 11** host, launched via `.bat` file, system tray icon via `tray.py`
- **BeautifulSoup** — HTML scraping

---

## File Structure

| File | Purpose |
|------|---------|
| `searcher.py` | Core engine — all search, scrape, match, verify, save logic (~3500+ lines) |
| `dashboard.py` | Flask web UI + scheduler + all API endpoints |
| `run_weekly.py` | Entry point: full Google search pass (all regions × all terms) |
| `run_facebook.py` | Entry point: Facebook-only search pass |
| `run_directories.py` | Entry point: NZSA + LinkedIn directory import |
| `run_partial.py` | Entry point: user-configured partial search (reads `partial_config.json`) |
| `tray.py` | Windows system tray icon — start/stop the dashboard bat process |
| `generate_static.py` | Generates offline static HTML snapshot of dashboard |
| `review.py` | Standalone correction/review tool |
| `search_terms.json` | Google + Facebook search terms, editable from dashboard Terms tab |
| `partial_config.json` | Written by dashboard Partial Search panel, read by run_partial.py |
| `corrections.json` | Structured false-positive log — blocks bad matches in future runs |
| `lessons.json` | LLM-generated lessons from past corrections — injected into verify prompts |
| `search_history.json` | Log of completed search run summaries |
| `search_status.json` | Live status polled by dashboard every 3s during a search |
| `search_log.txt` | Live terminal stdout during a search, shown in dashboard terminal preview |
| `running.flag` | Created at search start, deleted on completion/stop/dashboard startup |
| `pause.flag` | Created on pause, deleted on resume/completion/dashboard startup |
| `.env` | `ANTHROPIC_API_KEY`, `SERPAPI_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `SMTP_*`, `NOTIFY_EMAIL` |

---

## Full Search Pipeline (process_and_save_company)

Every company found — whether via Google, Facebook, or a directory — passes through the same pipeline in `process_and_save_company()`:

### 1. Dedup check
- `get_company_by_name()` — if company name already in DB, just append the new region and return
- `company_name_exists()` — final dedup guard at the end before save

### 2. Google Business Profile — `get_google_business_profile(company_name, region)`
One targeted SerpAPI search per company (`"Company Name" region New Zealand`). Parses `knowledge_graph` and `local_results` sections from the SerpAPI JSON response — these are returned alongside organic results at no extra quota cost. Extracts rating, reviews count, phone, address. Phone is backfilled to company's main phone field if not found elsewhere. Results stored in `google_rating`, `google_reviews`, `google_phone`, `google_address`.

### 3. Facebook page scrape (if `facebook_url` is known)
`scrape_facebook_page(fb_url)` — three-tier approach:
- **Tier 1** (no FB hit): parse Google/SerpAPI snippet from `_FB_SNIPPET_CACHE` — populated when `find_facebook_url` picks the winner URL. Extracts followers, phone, category, rating from Google's snippet text.
- **Tier 2** (minimal FB hit): stream only first 8 KB of the page HEAD to extract `og:description` meta tag. Survives login wall.
- **Tier 3** (mobile fallback): `m.facebook.com/slug/about/` with iPhone UA + 1s delay. Only runs if fields still missing after tiers 1+2. Parses JSON patterns for phone, email, address, category.
- FB email is backfilled to company email if none found elsewhere
- FB description/category fed into `extra_context` for LLM prompts

### 3. PSPLA check — `check_pspla(company_name, website_region, page_text, co_result, directors, extra_context)`
Four strategies tried in order, stopping at first hit:

**Strategy 1 — Name variations:**
`generate_name_variations()` produces: original, hyphen→space, CamelCase split, compound word split at pos 2&3 (e.g. "Onguard" → "On guard"), spaces removed. Tries each variant. Prefers Active licence; falls back to expired if nothing active found.

**Strategy 2 — Keyword AND search:**
`extract_keywords()` strips stop words (limited, ltd, nz, security, solutions, etc.) and joins first 3 meaningful keywords with `AND`. E.g. "Tarnix Security Limited" → `tarnix AND limited` → tries on Solr.

**Strategy 3 — Single keyword:**
First meaningful keyword with 6+ chars. Broad fallback for unusual names.

**Strategy 4 — LLM-suggested names:**
`_llm_suggest_pspla_names()` — Claude Haiku is given ALL available context (website name, region, CO registered name, CO address, directors, website text snippet, Facebook description, LinkedIn URL, NZSA member name) and asked to suggest up to 5 PSPLA search terms. Each suggestion is tried on Solr. Writes `llm_decision` audit entry when a suggestion finds results.

**After finding results:**
- If multiple companies in results (multiple permit numbers) OR match was by keyword/variant → `verify_pspla_match()` called for each candidate
- If single company + full-name match → trusted without verification
- Region-boosted sorting: PSPLA names containing website region words ranked higher
- Best-status doc selected: Active > Expired > Withdrawn, then most recent expiry date

### 4. PSPLA verification — `verify_pspla_match(website_company, pspla_company, website_region, pspla_address, _audit_name)`

**Step A — Hard pre-check (no LLM):**
Every significant word (4+ chars, not in generic stop list) from the website name must appear as an exact whole word in the PSPLA name. If ALL distinctive words are missing → instant reject (high confidence). Example: "Coast Security" vs "Coastal Security" → "coast" is not a whole word in "coastal" → rejected.

**Step B — Lessons injection:**
`_get_relevant_lessons()` finds up to 5 lessons from `lessons.json` whose `keywords_to_watch` appear in either company name. Lessons are injected as rules into the prompt.

**Step C — Claude Sonnet verify:**
Prompt gives: website name, region, PSPLA name, PSPLA address, examples of correct/incorrect matches, and the injected lessons. Returns `{"match": bool, "confidence": "high/medium/low", "reason": "..."}`. Every call writes to AuditLog (action=`llm_decision`).

**Step D — Deep verify (low or medium confidence):**
If Sonnet returns low or medium confidence AND extra context is available (page text, CO result, directors, FB/LinkedIn/NZSA data) → `_llm_deep_verify()` is called. Uses Claude Sonnet with ALL available data in a comprehensive prompt. Can confirm or override the initial result. Writes audit entries for both confirm and reject outcomes.

**Graceful degradation:**
If Claude API is unavailable, `verify_pspla_match` returns `{match: True, confidence: "low", reason: "LLM unavailable — pre-check passed, flagged for review"}` instead of False. `_llm_deep_verify` also passes through on failure. Counter `_llm_consecutive_errors` writes an audit warning at threshold=3.

### 5. Companies Office check — `check_companies_office(company_name)`
- Google search: `"company_name" site:companies.govt.nz`
- `_parse_co_result()` extracts: registered name, company number, NZBN, address, directors (from director lines), CO status (Registered/Removed/Deregistered from line near NZBN), incorporation date
- **Sold/re-registered detection**: if status is Removed → broader Google search using first distinctive keyword → looks for an active related company → tries its name on PSPLA as a successor

### 6. NZSA check — `check_nzsa(company_name)`
- Scrapes `security.org.nz` member directory
- Returns: member (bool), member_name, accredited (bool), grade, contact_name, phone, email, overview

### 7. Cross-check — `_llm_cross_check_sources()`
- Only runs if 2+ named sources are available (PSPLA name, CO name, NZSA name) OR Facebook description is available
- Claude Haiku cross-checks all source names, addresses, regions for consistency
- Returns: `{consistent: bool, confidence, issues: [], notes}`
- Inconsistency logged to terminal (currently informational only — does not block save)

### 8. Individual licence check
- Searches PSPLA for each director name individually
- Sets `individual_license_found` and `license_type`

### 9. Save — `save_to_supabase(record)`
- HTTP POST/PATCH to Supabase REST API
- Uses `RECORD_TEMPLATE` as the base dict (all columns defaulted to None)
- `check_schema()` must pass before any search — verifies all template keys exist as columns

---

## The Lessons System

**Purpose:** Prevent the same false-positive match from ever happening again.

**Flow:**
1. User finds a wrong PSPLA match in the dashboard and submits a correction note
2. `parse_and_save_correction()` uses Claude Haiku to classify the error type and extract the blocked PSPLA name → saved to `corrections.json`
3. `_generate_and_save_lesson()` asks Claude Haiku WHY the false match happened and writes a rule to prevent it → saved to `lessons.json` with `pattern_name`, `what_went_wrong`, `rule_to_apply`, `keywords_to_watch`
4. On the next run, `_get_relevant_lessons()` finds relevant lessons by keyword overlap and injects them into the `verify_pspla_match` prompt as "Learned rules from past corrections"
5. `_is_pspla_match_blocked()` is also checked directly — if a company→PSPLA pair is in `corrections.json` as `false_pspla_match`, that specific combination is immediately blocked without any LLM call

**Cache:** `_lessons_cache` and `_corrections_cache` are module-level caches. `_invalidate_lessons_cache()` and `invalidate_corrections_cache()` are called after saves.

---

## The Corrections System

`corrections.json` — list of dicts, each with:
- `type`: `false_pspla_match` | `not_security_company` | `wrong_data` | `other`
- `blocked_pspla_name`: (for false_pspla_match) the PSPLA name to never match to this company again
- `company_name`, `company_id`, `raw` (original user text), `summary`, `timestamp`

`_is_pspla_match_blocked(company_name, pspla_name)` — checked inside `check_pspla` before accepting any match. Uses substring matching to handle Ltd/Limited variants.

---

## AI / LLM Functions Summary

| Function | Model | Purpose | When called |
|----------|-------|---------|-------------|
| `extract_company_info()` | Haiku | Extract name, region, legal name, other names from scraped page text | Every new URL |
| `_llm_suggest_pspla_names()` | Haiku | Suggest PSPLA search terms when strategies 1-3 fail | Strategy 4 in check_pspla |
| `verify_pspla_match()` | Sonnet | Decide if PSPLA result is the same company as the website | Any keyword/variant/LLM match |
| `_llm_deep_verify()` | Sonnet | Full-context verification for low/medium confidence matches | When verify returns low or medium |
| `_llm_cross_check_sources()` | Haiku | Cross-check PSPLA, CO, NZSA, FB names for consistency | After NZSA check, if 2+ sources |
| `extract_from_fb_snippet()` | Haiku | Extract company name/region from a Facebook search snippet | Facebook search path |
| `parse_and_save_correction()` | Haiku | Parse user correction text into structured JSON | Dashboard correction form |
| `_generate_and_save_lesson()` | Haiku | Generate a rule from a false-positive correction | After correction is saved |

**All LLM calls:**
- Use `client.messages.create()` (Anthropic SDK)
- Return raw JSON — code strips markdown fences before `json.loads()`
- Call `_llm_ok()` on success, `_llm_error()` on exception
- Have a graceful fallback so search continues if API is unavailable
- `verify_pspla_match` and check_pspla strategy 4 write to AuditLog on every call

---

## Audit Log

Table: `AuditLog` in Supabase.
Columns: `id`, `timestamp`, `action`, `company_name`, `field_name`, `old_value`, `new_value`, `changes`, `triggered_by`, `notes`

Action types:
- `added` — new company saved
- `updated` — field changed (e.g. recheck updated pspla_licensed)
- `deleted` — record deleted
- `email` — notification email sent
- `correction` — user submitted a correction
- `llm_decision` — every LLM call result (verify_pspla_match, deep verify, strategy 4, cross-check)
- `llm_error` — LLM API failure warning (written at threshold=3 consecutive failures)

Dashboard "AI Matching Decisions" button in each company's detail row fetches `llm_decision` entries for that company and shows them colour-coded: green=accepted, red=rejected, purple=strategy4, orange=inconsistency.

---

## Supabase — Companies Table Columns

### Identity
`id`, `company_name`, `website_url`, `root_domain`, `source_url`, `address`, `region`, `email`, `phone`, `date_added`, `last_checked`, `notes`

### PSPLA
`pspla_licensed` (bool), `pspla_name`, `pspla_license_number`, `pspla_license_status`, `pspla_license_expiry`, `pspla_license_classes`, `pspla_license_start`, `pspla_permit_type`, `license_type` (individual/company), `match_method`, `match_reason`

### Companies Office
`companies_office_name`, `companies_office_address`, `companies_office_number`, `nzbn`, `co_status` (registered/removed), `co_incorporated`, `director_name`, `individual_license`

### NZSA
`nzsa_member`, `nzsa_member_name`, `nzsa_accredited`, `nzsa_grade`, `nzsa_contact_name`, `nzsa_phone`, `nzsa_email`, `nzsa_overview`

### Facebook
`facebook_url`, `fb_followers`, `fb_phone`, `fb_email`, `fb_address`, `fb_description`, `fb_category`, `fb_rating`

### Google Business Profile
`google_rating`, `google_reviews`, `google_phone`, `google_address`

### Other social
`linkedin_url`

### User
`tagged`, `flagged`

---

## External APIs

| Service | URL / details | Key |
|---------|--------------|-----|
| Anthropic | claude-haiku-4-5-20251001 (fast/cheap), claude-sonnet-4-6 (deep verify) | `ANTHROPIC_API_KEY` |
| SerpAPI | `serpapi.com` — ALL Google searches go through this, costs money per query | `SERPAPI_KEY` |
| Supabase | `SUPABASE_URL/rest/v1/Companies` and `/AuditLog` | `SUPABASE_KEY` |
| PSPLA Solr | `https://forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select` — no auth needed | none |
| NZSA | `security.org.nz` member directory — scraped | none |
| Companies Office | Searched via Google (`site:companies.govt.nz`), not direct API | none |
| SMTP | Email notification after each run | `SMTP_HOST/PORT/USER/PASS`, `NOTIFY_EMAIL` |

---

## Key Design Decisions / Patterns

- **`RECORD_TEMPLATE`** — dict of all DB column names with `None` defaults. Every record starts as a copy. Ensures no missing keys ever reach Supabase.
- **`check_schema()`** — runs at startup of every search script. Verifies all RECORD_TEMPLATE keys exist in Supabase. Aborts if missing. Add new columns here after adding to RECORD_TEMPLATE.
- **`running.flag` / `pause.flag`** — file-based IPC. Dashboard deletes both on startup (prevents false "already running" after crash). Stop button also deletes them.
- **`__main__` guards** on all `run_*.py` files — prevents accidental full search execution when imported (e.g. `python -c "import run_directories"` once triggered a real search accidentally).
- **Orphan process kill** — Stop button uses PowerShell WMI to scan for `python*` processes whose command line contains script names (with and without `.py` extension).
- **`_FB_SNIPPET_CACHE`** — module-level dict. `find_facebook_url` populates it with the Google snippet for the winning FB URL. `scrape_facebook_page` reads it as tier 1. `_process_fb_result` also populates it for companies found via the Facebook search path.
- **Sold company detection** — `_parse_co_result` reads CO status ("Removed"/"Deregistered") from text near the NZBN line. `check_companies_office` then does a broader successor keyword search and returns `successor_name`, `successor_number`. `process_and_save_company` retries PSPLA with the successor name.
- **Region-boosted PSPLA sorting** — when multiple PSPLA results, candidates whose names contain website region words are ranked first before verification.
- **Best permit doc selection** — when Solr returns multiple docs for the same permit (e.g. renewal rows), `_best_doc_for_permit` picks the one with best status (Active > Expired) and most recent expiry date.
- **Version control** — git repo. Dashboard has Rollback button (`git stash`). Commit after every significant change session.

---

## Scheduled Searches (APScheduler in dashboard.py)

- **Weekly** (Sunday 2am NZT): Full Google search — `run_weekly.py --scheduled`
- **Monthly** (1st, 3am NZT): Facebook pass — `run_facebook.py --scheduled`
- **Monthly** (1st, 4am NZT): Directory import (NZSA + LinkedIn) — `run_directories.py --scheduled`

---

## Dashboard — Key Endpoints

| Route | What it does |
|-------|-------------|
| `/` | Main table — all companies, filterable |
| `/search-status` | Polled every 3s — returns live progress + `llm_warning` if LLM errors ≥ 3 |
| `/start-search` | Launches `run_weekly.py` subprocess |
| `/start-facebook` | Launches `run_facebook.py` subprocess |
| `/start-directories` | Launches `run_directories.py` subprocess |
| `/start-partial` | Writes `partial_config.json`, launches `run_partial.py` |
| `/stop-search` | Kills subprocess + orphan processes via PowerShell WMI |
| `/pause-search` / `/resume-search` | Creates/removes `pause.flag` |
| `/recheck-pspla` | Re-runs full PSPLA pipeline for one company (builds extra_context from DB row) |
| `/lookup-facebook` | Re-runs `find_facebook_url` + `scrape_facebook_page` for one company |
| `/recheck-nzsa` | Re-runs NZSA check for one company |
| `/company-ai-decisions` | Returns all `llm_decision` AuditLog entries for one company |
| `/rollback` | Runs `git stash` |
| `/search-terms` | GET/POST the `search_terms.json` file |
| `/save-edit` | Save inline record edit, writes AuditLog |
| `/save-correction` | Save correction → parse → generate lesson → invalidate caches |
| `/export-csv` | Download all companies as CSV |

---

## Common Tasks for Claude

**Add a new Supabase column:**
1. Add key to `RECORD_TEMPLATE` in `searcher.py` with `None` default
2. Populate it in the record dict inside `process_and_save_company`
3. Add to the detail row HTML in `dashboard.py`
4. User adds the column in Supabase manually (text type unless otherwise needed)
5. `check_schema()` will auto-detect it — no change needed there

**Change search terms or regions:**
- Edit `search_terms.json` via dashboard Terms tab OR edit `_DEFAULT_GOOGLE_TERMS` / `_DEFAULT_FACEBOOK_TERMS` / `NZ_REGIONS` in `searcher.py`

**Debug a specific company:**
Call individual pipeline functions directly in a test script:
```python
from searcher import check_pspla, check_companies_office, check_nzsa, scrape_facebook_page
co = check_companies_office("Company Name")
pspla = check_pspla("Company Name", website_region="Auckland", co_result=co)
```

**Roll back a bad change:**
`git stash` or dashboard Rollback button, or `git revert <hash>` for a specific commit.

**Test a new column is saving:**
Run `check_schema()` — prints any missing columns. Then run a test company through `process_and_save_company`.

**If LLM keeps rejecting good matches:**
Check `lessons.json` for overly aggressive rules. Check `corrections.json` for blocked pairs. Add a targeted example to the `verify_pspla_match` prompt examples section.
