# Gemini AI Phone Calls

AI-powered outbound phone call system using Google Gemini 3.1 Flash Live + Twilio telephony. First Flask blueprint.

## Architecture
```
Dashboard (blueprints/gemini.py) -> POST /api/gemini/make-call
    -> FastAPI Call Server (gemini_call_server.py on separate Azure Web App)
        -> Twilio creates outbound call
        -> Twilio connects WebSocket to /media-stream/{call_id}
        -> Call server bridges audio: Twilio (mulaw 8kHz) <-> Gemini Live (PCM 16/24kHz)
        -> Live transcript broadcast via /ws/transcript/{call_id}
```

## Files
- `blueprints/__init__.py` — Package init
- `blueprints/gemini.py` — Flask blueprint: page, API endpoints, template
- `gemini_call_server.py` — FastAPI WebSocket server: Twilio<->Gemini audio bridge

## Azure Infrastructure
- Dashboard: `pspla-checker` (existing)
- Call server: `gemini-call-server` (separate App Service)
- Call server URL: `gemini-call-server-dqd4b6a8dtdpezcx.newzealandnorth-01.azurewebsites.net`
- WebSockets MUST be enabled on call server

## Environment Variables
| Variable | Where | Purpose |
|----------|-------|---------|
| `TWILIO_ACCOUNT_SID` | Both | Twilio account |
| `TWILIO_AUTH_TOKEN` | Both | Twilio auth |
| `TWILIO_PHONE_NUMBER` | Both | NZ calling number |
| `GEMINI_API_KEY` | Call server | Google AI API key |
| `GEMINI_CALL_SERVER_URL` | Dashboard | URL of FastAPI server |
| `GEMINI_CALL_SERVER_SECRET` | Both | Shared API auth secret |
| `GEMINI_CALL_SERVER_SELF_URL` | Call server | Its own public URL (for TwiML) |
| `OPENAI_API_KEY` | Both | OpenAI embeddings for RAG |
| `ELEVENLABS_API_KEY` | Call server | ElevenLabs Conversational AI |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Dashboard | Google Drive API service account (full JSON key) |

## Blueprint Pattern
- Defined in `blueprints/gemini.py`, registered in `dashboard.py`
- Shared helpers injected at registration: `_is_admin`, `_has_permission`, etc.
- Env vars read at REQUEST time with `os.getenv()` (not import time — Azure sets vars after boot)
- Permission group: `gemini` (opt-in, default False)

## Audio Bridge (critical path)
- Twilio: mulaw 8kHz mono, base64 -> Convert: `audioop.ulaw2lin()` -> `ratecv(8k->16k)` -> Gemini PCM
- Gemini: PCM 24kHz -> Convert: `ratecv(24k->8k)` -> `lin2ulaw()` -> base64 -> Twilio
- Input buffered into 20ms chunks (640 bytes at 16kHz)
- `audioop` built-in on Python 3.11 (Azure), use `audioop-lts` on 3.13+

## Gemini Live API Specifics
- Model: `gemini-3.1-flash-live-preview`
- Config uses typed objects: `types.LiveConnectConfig`, `types.SpeechConfig`
- `session.receive()` ends after `turn_complete` — MUST re-enter in `while True` loop
- `send_client_content()` crashes — do NOT use for initial greeting
- Pre-connecting times out during ring — create session in WebSocket handler
- Known 5-10s cold start on first response (Google issue)
- Interruptions: when `sc.interrupted` is true, send Twilio `clear` event

## Supabase Tables
- `gemini_knowledge_bases` — id, name, content, voice_name, rag_enabled, elevenlabs_rag_mode, created_at, updated_at (RLS)
- `gemini_call_history` — id, call_sid, call_id, to_number, from_number, knowledge_base_id, status, duration_seconds, transcript (jsonb), recording_url, started_at, ended_at, triggered_by, notes (RLS)
- `rag_documents` — id, title, source_type (pdf/docx/txt/md/url/gdrive), source_url, original_filename, gdrive_file_id, file_size_bytes, char_count, chunk_count, raw_text, status, error_message, created_at
- `rag_chunks` — id, document_id (FK), chunk_index, content, token_count, embedding (vector 1536)
- `rag_kb_documents` — id, knowledge_base_id, document_id, created_at (junction table)
- `match_rag_chunks` — Supabase RPC function: vector similarity search filtered by kb_id via junction table

## Deploying Call Server
```bash
cd /c/Users/WadeAdmin/pspla-checker
python -c "import zipfile; z=zipfile.ZipFile('gemini-deploy.zip','w'); z.write('gemini_call_server.py'); z.write('requirements.txt'); z.close()"
az webapp deploy --name gemini-call-server --resource-group gemini-call-server_group --src-path gemini-deploy.zip --type zip
```

## CSP Note
`connect-src` MUST include `https://*.azurewebsites.net wss://*.azurewebsites.net` for transcript WebSocket.

## Call Settings (persisted in localStorage)
- AI Provider: Gemini, OpenAI, ElevenLabs
- Voice: per-provider voice selection
- ElevenLabs Agent: agent selector + prompt source (agent vs knowledge base)
- RAG Source: ElevenLabs KB vs in-house document library
- Language: en, en-NZ, en-AU, etc. (adds accent hints to Gemini system prompt)
- Strict Mode: restricts AI to only reference document content
- End of Speech Sensitivity: low/default/high (all providers)
- Start of Speech Sensitivity, Silence Duration: Gemini only
- Thinking Level, Include AI Thoughts: disabled (not supported by Gemini Live API)

## RAG (Retrieval-Augmented Generation)
- **Document Library**: upload PDF/DOCX/TXT/URL or import from Google Drive
- **Google Drive**: service account auth, supports single doc or folder import, sync button to re-fetch
- **Processing pipeline**: extract text → chunk (2000 chars, 400 overlap) → embed (OpenAI text-embedding-3-small) → store in Supabase
- **Search**: vector similarity via `match_rag_chunks` RPC, threshold 0.3
- **Pre-computed context**: injected into system prompt at call start
- **Multi-turn RAG**: mid-call search on each caller utterance (fire-and-forget via asyncio.create_task)
- **Test Search**: UI to test RAG queries without making calls, with "Ask AI" to simulate agent response
- **View Chunks**: UI to inspect extracted text from documents

## Call Server Version
- `SERVER_VERSION` auto-computed from file mtime at startup
- Displayed in navbar via `/health` endpoint fetch
- `/health` returns `{ok, active_calls, version}`

## Debug Panel
Sections: Call Server, Gemini API, Twilio, ElevenLabs (quota/agents/calls), Supabase, Recent Errors
- ElevenLabs section shows subscription tier, character usage, agent count, 7-day call count
- Errors log WebSocket close codes, which side disconnected, RAG search results
