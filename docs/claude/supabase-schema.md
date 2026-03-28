# Supabase Schema

## RLS (Row Level Security)
- **Companies**: `public_read_only` policy — anon can SELECT only. Service role bypasses RLS.
- **AuditLog**: RLS enabled, NO policies — fully blocked to anon. Service role only.
- Public site (anon key) can read Companies but not AuditLog or write anything.

## Companies Table Columns

**Identity:** `id`, `company_name`, `website`, `root_domain`, `source_url`, `address`, `region`, `email`, `phone`, `date_added`, `last_checked`, `notes`

**PSPLA:** `pspla_licensed` (bool), `pspla_name`, `pspla_license_number`, `pspla_license_status`, `pspla_license_expiry`, `pspla_license_classes`, `pspla_license_start`, `pspla_permit_type`, `license_type`, `match_method`, `match_reason`

**Companies Office:** `companies_office_name`, `companies_office_address`, `companies_office_number`, `nzbn`, `co_status`, `co_incorporated`, `director_name`, `individual_license`

**NZSA:** `nzsa_member`, `nzsa_member_name`, `nzsa_accredited`, `nzsa_grade`, `nzsa_contact_name`, `nzsa_phone`, `nzsa_email`, `nzsa_overview`

**Facebook:** `facebook_url`, `fb_followers`, `fb_phone`, `fb_email`, `fb_address`, `fb_description`, `fb_category`, `fb_rating`

**FB Services:** `fb_alarm_systems` (bool), `fb_cctv_cameras` (bool), `fb_alarm_monitoring` (bool)

**Website Services:** `has_alarm_systems` (bool), `has_cctv_cameras` (bool), `has_alarm_monitoring` (bool)

**Google:** `google_rating`, `google_reviews`, `google_phone`, `google_address`

**Other:** `linkedin_url`, `tagged`, `flagged`

**Boolean columns** are true PostgreSQL booleans. In JS use `svcYes(v)` helper.

## Other Tables
- `allowed_users` — id, email, name, added_by, added_at, active, last_login, last_provider, is_admin
- `login_audit` — id, email, provider, result, attempted_at, user_agent
- `challenge_signups` — Stripe challenge data (see club-fitness.md)
- `challenge_links` — saved payment links
- `challenge_mappings` — data cleaning maps
- `gemini_knowledge_bases` — AI call knowledge bases (id, name, content, voice_name, rag_enabled, elevenlabs_rag_mode)
- `gemini_call_history` — AI call logs (notes jsonb includes rag debug info)
- `rag_documents` — uploaded/imported documents for RAG (source_type: pdf/docx/txt/md/url/gdrive, gdrive_file_id for sync)
- `rag_chunks` — text chunks with vector embeddings (1536-dim, text-embedding-3-small)
- `rag_kb_documents` — junction table linking documents to knowledge bases
- `search_runs` — search history with `triggered_by_user` column
