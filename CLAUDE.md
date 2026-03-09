# PSPLA Checker — Project Context for Claude Code

## What This Project Does
Automated tool that finds NZ security companies on the internet, checks whether each one holds a current PSPLA (Private Security Personnel Licensing Authority) licence, and stores the results in a Supabase database. The dashboard lets the user browse results, trigger searches, review AI decisions, and correct errors.

The project is owned and operated by Wade (the user). It monitors the NZ private security industry for compliance.

---

## Tech Stack
- **Python** (Flask dashboard + searcher engine)
- **Supabase** (PostgreSQL database — hosted, accessed via REST API)
- **Anthropic Claude API** (claude-haiku-4-5 for LLM matching/verification)
- **SerpAPI** (Google search wrapper — used for all web searches)
- **APScheduler** (scheduled weekly/monthly runs inside dashboard.py)
- **Windows 11** host, run via `.bat` file, system tray icon via `tray.py`

---

## File Structure

| File | Purpose |
|------|---------|
| `searcher.py` | Core engine — all search, scrape, match, verify, save logic (~3500 lines) |
| `dashboard.py` | Flask web app — UI, scheduling, API endpoints, status polling (~1200 lines) |
| `run_weekly.py` | Entry point for full Google search pass (all regions × all terms) |
| `run_facebook.py` | Entry point for Facebook-only search pass |
| `run_directories.py` | Entry point for NZSA + LinkedIn directory imports |
| `run_partial.py` | Entry point for user-configured partial searches (reads `partial_config.json`) |
| `tray.py` | Windows system tray icon — starts/stops the dashboard bat process |
| `generate_static.py` | Generates a static HTML snapshot of the dashboard for offline viewing |
| `review.py` | Standalone review/correction tool |
| `search_terms.json` | Google and Facebook search terms (editable from dashboard) |
| `partial_config.json` | Written by dashboard Partial Search panel, read by run_partial.py |
| `corrections.json` | Manual corrections log (PSPLA false positives etc.) |
| `lessons.json` | LLM-generated matching lessons from past corrections |
| `search_history.json` | Log of completed search runs |
| `search_status.json` | Live status file polled by dashboard during a search |
| `search_log.txt` | Live terminal output during a search |
| `running.flag` | Exists while a search is running — deleted on dashboard startup and stop |
| `pause.flag` | Exists while a search is paused |
| `.env` | API keys (ANTHROPIC_API_KEY, SERPAPI_KEY, SUPABASE_URL, SUPABASE_KEY, SMTP_*) |

---

## How a Search Works (Pipeline)

1. **Google search** via SerpAPI (`google_search()`) — queries like `"security company Wellington New Zealand"`
2. **Filter URLs** — skip SKIP_DOMAINS (social media, govt, directories), skip already-known domains
3. **Scrape website** (`scrape_website()`) — extract page text, email, Facebook URL, LinkedIn URL
4. **Extract company info** (`extract_company_info()`) — Claude LLM extracts company name, region, legal name, other names from page text
5. **Companies Office check** (`check_companies_office()`) — Google search for the company on companies.govt.nz; extracts name, number, NZBN, address, directors, CO status (registered/removed), incorporation date. If CO shows "Removed", does a successor search to find the active re-registered entity.
6. **PSPLA check** (`check_pspla()`) — queries the PSPLA Solr API at `https://forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select`. Tries multiple name strategies: direct, LLM-suggested variants, director names. Extracts licence number, status, expiry, licence classes (Property Guard, Monitoring Officer, etc.), start date, permit type.
7. **LLM verification** (`verify_pspla_match()`) — Claude Haiku decides if the PSPLA match is genuine. Falls back to low-confidence acceptance (not rejection) if API is unavailable.
8. **NZSA check** (`check_nzsa()`) — scrapes the NZSA member directory for matching membership/accreditation
9. **Cross-check** (`_llm_cross_check_sources()`) — if multiple sources found, Claude Haiku checks consistency across PSPLA name, CO name, NZSA name, Facebook description
10. **Facebook page scrape** (`scrape_facebook_page()`) — three-tier: (1) parse Google snippet already fetched, (2) stream page HEAD for og:description, (3) mobile site fallback. Gets followers, phone, email, address, category, rating.
11. **Save to Supabase** (`save_to_supabase()`) — upserts record into Companies table

---

## Supabase Database — Companies Table Columns

### Identity
`id`, `company_name`, `website_url`, `root_domain`, `source_url`, `address`, `region`, `email`, `phone`, `date_added`, `last_checked`, `notes`

### PSPLA
`pspla_licensed` (bool), `pspla_name`, `pspla_license_number`, `pspla_license_status`, `pspla_license_expiry`, `pspla_license_classes`, `pspla_license_start`, `pspla_permit_type`, `license_type`, `match_method`, `match_reason`

### Companies Office
`companies_office_name`, `companies_office_address`, `companies_office_number`, `nzbn`, `co_status`, `co_incorporated`, `director_name`, `individual_license`

### NZSA
`nzsa_member`, `nzsa_member_name`, `nzsa_accredited`, `nzsa_grade`, `nzsa_contact_name`, `nzsa_phone`, `nzsa_email`, `nzsa_overview`

### Facebook (scraped)
`facebook_url`, `fb_followers`, `fb_phone`, `fb_email`, `fb_address`, `fb_description`, `fb_category`, `fb_rating`

### Other
`linkedin_url`, `tagged` (manual tag), `flagged` (manual flag)

### Audit / Corrections
AuditLog table: `id`, `timestamp`, `action` (added/updated/deleted/email/correction/llm_decision/llm_error), `company_name`, `field_name`, `old_value`, `new_value`, `changes`, `triggered_by`, `notes`

---

## External APIs

| Service | Used For | Key env var |
|---------|----------|-------------|
| Anthropic Claude API | Company info extraction, PSPLA name suggestion, match verification, cross-check | `ANTHROPIC_API_KEY` |
| SerpAPI | All Google searches (finding companies, CO lookup, FB URL finding, email finding) | `SERPAPI_KEY` |
| Supabase | Database read/write | `SUPABASE_URL`, `SUPABASE_KEY` |
| PSPLA Solr | Direct licence lookup — `forms.justice.govt.nz/forms/publicSolrProxy/solr/PSPLA/select` | none |
| NZSA website | Member directory scrape — `security.org.nz` | none |
| SMTP | Email notification after each search run | `SMTP_HOST/PORT/USER/PASS`, `NOTIFY_EMAIL` |

---

## Key Design Decisions / Patterns

- **`RECORD_TEMPLATE`** in searcher.py — dict of all DB columns with `None` defaults. Every record is built by copying this then filling fields. Ensures no missing keys.
- **`check_schema()`** — verifies all RECORD_TEMPLATE keys exist in Supabase before any search starts. If columns are missing, search aborts.
- **`running.flag` / `pause.flag`** — file-based IPC between search subprocess and dashboard. Dashboard deletes both on startup to prevent stale state after a crash.
- **LLM graceful degradation** — if Claude API is down, `verify_pspla_match` returns `{match: True, confidence: "low"}` (not False) so searches keep running. Counter `_llm_consecutive_errors` triggers audit warning at threshold 3.
- **Sold/re-registered company detection** — `_parse_co_result` detects "Removed" CO status. `check_companies_office` then does a broader successor search with first keyword. Successor name is tried on PSPLA separately.
- **CO status parsing** — looks for the NZBN line in CO search results, then scans nearby lines for "Registered", "Removed", "Deregistered" and "Incorporation Date".
- **Facebook snippet cache** (`_FB_SNIPPET_CACHE`) — populated by `find_facebook_url` when it picks the winner URL. Consumed by `scrape_facebook_page` as tier 1 (no FB hit needed).
- **`__main__` guards** on run_*.py files — prevents accidental full search execution when imported for syntax checking.
- **Orphan process kill** — dashboard Stop button uses PowerShell WMI scan for `python*` processes whose command line contains known script names (including bare names without .py).
- **AI decision audit trail** — every LLM call in `verify_pspla_match`, `_llm_deep_verify`, `check_pspla` (strategy 4) writes to AuditLog with action=`llm_decision`. Dashboard shows per-company "AI Matching Decisions" section.
- **Version control** — project is a git repo. Dashboard has a rollback button. All significant changes are committed.

---

## Dashboard Endpoints (Flask)

| Route | Purpose |
|-------|---------|
| `/` | Main dashboard table — all companies |
| `/search-status` | Polled every 3s during search for live progress |
| `/start-search` | Launches run_weekly.py as subprocess |
| `/start-facebook` | Launches run_facebook.py |
| `/start-directories` | Launches run_directories.py |
| `/start-partial` | Writes partial_config.json then launches run_partial.py |
| `/stop-search` | Kills search subprocess + orphan processes |
| `/pause-search` / `/resume-search` | Creates/removes pause.flag |
| `/recheck-pspla` | Re-runs PSPLA check for one company |
| `/lookup-facebook` | Re-runs Facebook URL search for one company |
| `/recheck-nzsa` | Re-runs NZSA check for one company |
| `/company-ai-decisions` | Returns AuditLog entries for one company |
| `/rollback` | Runs `git stash` to revert uncommitted changes |
| `/search-terms` | GET/POST search terms JSON |
| `/save-edit` | Save inline record edit |
| `/save-correction` | Save correction + generate LLM lesson |
| `/export-csv` | Download all companies as CSV |

---

## Scheduled Searches (APScheduler inside dashboard.py)

- **Weekly** (Sunday 2am): Full Google search — `run_weekly.py --scheduled`
- **Monthly** (1st of month, 3am): Facebook search — `run_facebook.py --scheduled`
- **Monthly** (1st of month, 4am): Directory import — `run_directories.py --scheduled`

---

## Common Tasks for Claude

- **Add a new Supabase column**: (1) Add to `RECORD_TEMPLATE` in searcher.py, (2) populate it in `process_and_save_company` record dict, (3) add display in dashboard.py detail row template, (4) add to `check_schema()` expected columns list if it should be validated at startup.
- **Change search regions or terms**: Edit `search_terms.json` via dashboard Terms tab, or edit `NZ_REGIONS` list in searcher.py.
- **Debug a specific company**: Use `process_and_save_company` dry-run pattern — call the individual check functions manually with the company's website URL.
- **Roll back a bad change**: `git stash` or use the dashboard Rollback button, or `git revert <hash>`.
- **Check if columns exist in Supabase**: Run `check_schema()` — it prints missing columns.
