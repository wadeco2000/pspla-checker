# PSPLA Checker

Automated tool that finds NZ security companies, checks PSPLA licences, stores results in Supabase. Flask dashboard for browsing/searching. Public GitHub Pages site encrypted with StatiCrypt. Owned by Wade.

## Tech Stack
Python (Flask), Supabase (PostgreSQL REST API), Anthropic Claude API (Haiku + Sonnet), SerpAPI, GitHub Actions, Azure App Service, BeautifulSoup, StatiCrypt, Dropbox API, Google Drive API, ElevenLabs Conversational AI, OpenAI (embeddings)

## Key Files
| File | Purpose |
|------|---------|
| `searcher.py` | Core search/scrape/match/verify engine (~6000 lines) |
| `dashboard.py` | Flask web UI + API endpoints (~8400 lines) |
| `run_weekly.py` | Weekly light scan entry point |
| `run_facebook.py` | Facebook search entry point |
| `run_directories.py` | NZSA + LinkedIn directory import |
| `run_partial.py` | User-configured partial search |
| `run_facebook_fix.py` | Enrich Facebook-only entries |
| `generate_static.py` | Public GitHub Pages site generator |
| `blueprints/gemini.py` | Gemini AI phone calls (Flask blueprint) |
| `gemini_call_server.py` | FastAPI Twilio-Gemini audio bridge (separate Azure app) |
| `corrections.json` | Blocked false-positive matches |
| `lessons.json` | LLM-generated verification rules |
| `running.flag` / `pause.flag` | File-based search IPC |
| `search_start.json` | Search type + start time for conflict detection |

## Environment Variables
`ANTHROPIC_API_KEY`, `SERPAPI_KEY`, `SUPABASE_URL`, `SUPABASE_KEY` (anon, public site only), `SUPABASE_SERVICE_KEY` (service role, dashboard+searcher), `PAGES_PASSWORD`, `EXPORT_PASSWORD`, `GITHUB_PAT`, `GITHUB_REPO`, `DROPBOX_TOKEN`, `SMTP_*`, `NOTIFY_EMAIL`, `STRIPE_*`, `BOOKAFY_API_KEY`, `BOOKAFY_STAFF_EMAIL`, `CF_SMTP_*`, `TWILIO_*`, `GEMINI_*`, `ACTUATE_API_TOKEN`, `OPENAI_API_KEY` (embeddings), `ELEVENLABS_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (Drive API)

**Critical:** `SUPABASE_SERVICE_KEY` bypasses RLS. `SUPABASE_KEY` is anon (RLS-restricted). Never swap these.

## Key Design Patterns
- **`RECORD_TEMPLATE`** — single source of truth for all DB columns
- **`check_schema()`** — verifies all columns exist at startup
- **`running.flag` / `pause.flag`** — file-based IPC, cleared on dashboard startup
- **`_LoggingAnthropicClient`** — intercepts all LLM calls, logs to `llm_debug.log`
- **CSV exports** — always use `sorted(companies[0].keys())`, never hardcoded
- **Blueprint pattern** — env vars read at request time with `os.getenv()` (Azure sets vars after boot)
- **Partners navbar** — dropdown groups Actuate, Shelly, Club Fitness (permission-gated)

## Deploy Process
- **Dashboard:** Include `[deploy]` in commit message to trigger Azure deploy. Omit for code-only pushes.
- **Workflow:** `.github/workflows/deploy-dashboard.yml` — condition: `contains(commit.message, '[deploy]')`
- **Azure:** `pspla-checker`, Linux, NZ North. Startup: `gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 1 dashboard:app`
- **`--preload` removed** — boot takes ~33s without it vs 6+ min with it at 17k lines
- **Health check:** `GET /health` returns 200 (in `_AUTH_SKIP`)
- **Custom domain:** `www.psplachecker.co.nz` (redirects from `.azurewebsites.net`)
- **Public site:** Run `generate_static.py` or click Publish Live. Deploys to `gh-pages` branch.
- **Call server:** zip + `az webapp deploy` (see `docs/claude/gemini.md`)
- **Deploy monitor:** `deploy_poll.bat` — 3-phase poll (Actions -> Azure -> custom domain)

## Common Tasks

**Add a new Supabase column:**
1. Add key to `RECORD_TEMPLATE` in `searcher.py`
2. Populate in `process_and_save_company`
3. Add to detail row HTML in `dashboard.py`
4. User adds column in Supabase manually
5. `check_schema()` auto-detects it

**If LLM keeps rejecting good matches:** Check `lessons.json` for aggressive rules, `corrections.json` for blocked pairs.

**Deploy:** Include `[deploy]` in final commit message only. No `[deploy]` for WIP commits.

## Detailed Reference Docs
For full details on specific areas, read these files:
- `docs/claude/search-pipeline.md` — Full 12-step search pipeline
- `docs/claude/endpoints.md` — All dashboard API endpoints
- `docs/claude/supabase-schema.md` — Table columns, RLS details
- `docs/claude/actuate.md` — Actuate camera AI page
- `docs/claude/club-fitness.md` — Club Fitness challenges page
- `docs/claude/gemini.md` — Gemini AI phone calls + call server
- `docs/claude/features.md` — Corrections, backups, search queue, NZSA report, LinkedIn, mobile, etc.

## AI Functions
| Function | Model | Purpose |
|----------|-------|---------|
| `extract_company_info()` | Haiku | Extract name/region from scraped text |
| `verify_pspla_match()` | Sonnet | Verify PSPLA match is same company |
| `_llm_deep_verify()` | Sonnet | Full-context verification |
| `_llm_suggest_pspla_names()` | Haiku | Suggest PSPLA search terms |
| `llm_verify_associations()` | Haiku | Review associations before save |
| `detect_services()` | Haiku | Detect alarm/CCTV/monitoring |
| `rag_test_ask()` | Haiku | Simulate phone agent response from RAG chunks |

## Known Issues
- Rapid successive deploys can cause Azure 409 Conflict — wait 1-2 min then manual deploy
- Always verify `wc -l dashboard.py` before committing — local truncation observed during heavy editing
