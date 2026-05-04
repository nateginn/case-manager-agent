# Case Manager Agent — Project Memory

## What this project is
A local, HIPAA-compliant AI agent system for a chiropractic/PT/massage/acupuncture/shockwave clinic.
Processes incoming emails, drafts replies and internal Google Chat messages, routes referrals and
billing inquiries. Human-in-the-loop approval via a local FastAPI dashboard. No PHI leaves the machine.

## Current status: BUILD COMPLETE — Tasks B and C done, dashboard queue management added, ready for live pass

## Tech stack
- Python 3.11, FastAPI, ChromaDB, Ollama
- Google Workspace (Gmail + Google Chat)
- Windows 11, RTX 4090 (24GB VRAM)
- IDE: Windsurf

## Ollama models (all downloaded)
- qwen3:32b — primary model (ReferralAgent, BillingAgent)
- qwen3:4b — light model (ChatAgent, Orchestrator classifier)
- granite3.2-vision — OCR fallback for scanned fax PDFs
- bge-m3 — embeddings for ChromaDB
- glm-4.7-flash — keep, used by another project

## Project location
C:\Users\growy\Dev_projects\case-manager-agent\

## Agent architecture
- OrchestratorAgent — classifies emails, routes to specialists, polls Gmail
- ReferralAgent — parses referral emails + PDF fax attachments, drafts replies
- BillingAgent — handles insurance/billing inquiries, drafts replies
- ChatAgent — internal staff coordination, manages staged Google Chat messages
- All agents use phi_scrub() before storing anything in ChromaDB
- All outbound actions are DRAFT only until human approves in dashboard

## Tools
- GmailTool — OAuth2, fetch emails, create drafts, mark processed, list/delete drafts,
  fetch_thread() (added Task C), apply_label() (added Task B)
- PdfTool — PyPDF2 text extraction + Ollama structured field extraction + granite3.2-vision fallback
- GoogleChatTool — REST API sender (only function that actually sends)
- PromptEmrTool — stub, pending Prompt EMR API access

## Memory / storage
- ChromaDB: two collections — email_records and email_summaries (PHI-scrubbed)
- Staged Chat messages: memory/staged_chat_messages.json
- Embeddings: bge-m3 (needs to be wired in — not yet updated from default)

## Key design decisions
- DRAFT_MODE=True always — nothing sends without human approval
- phi_scrub() runs twice: before sending to Ollama AND on Ollama output
- phi_scrub() also applied to each thread history message body before prompt injection
- Gmail label "agent-processed" prevents re-processing across restarts
- Gmail label "agent-timed-out" marks emails that stalled Ollama inference
- Three-strategy JSON parse fallback in all agents (direct parse → regex → null skeleton)
- Ollama structured output format parameter should be added (not yet done)
- staged_chat_messages.json uses read-modify-write, not append, for valid JSON
- All staged chat entries have a stable UUID generated at write time

## Server Launch Command (Windows)
```
$env:PYTHONUTF8=1; python -m uvicorn main:app --host 127.0.0.1 --port 8000
```
(no --reload outside of active development)
Note: `PYTHONUTF8=1 python -m ...` bash syntax does NOT work in PowerShell — use `$env:` syntax above.

## HIPAA posture
- validate_hipaa_posture() runs on startup
- HIPAA_POSTURE.md documents all data flows
- No cloud LLM calls anywhere in codebase
- PHI never logged — only message IDs, timestamps, classifications

## Files of note
- main.py — FastAPI app, dashboard at GET /, polling thread, chat queue endpoints
- config.py — pydantic BaseSettings, loads from .env
- training/ingest_history.py — one-time historical email ingestion
- HIPAA_POSTURE.md — compliance documentation
- memory/staged_chat_messages.json — pending Chat messages
- training/audit_report.txt — generated after ingestion
- CLAUDE.md — Claude Code behavior rules (project root)
- ROUTINES_CONSIDERATION.md — future scheduling options

---

## Session: 2026-04-15

### Completed
- Google Cloud Console setup (OAuth, Gmail + Chat APIs)
- credentials.json saved to project root
- token.json created (4 scopes: gmail.readonly, gmail.compose, gmail.modify, chat.messages.create)
- Switched GoogleChatTool from webhooks to REST API
- Config updated: qwen3:32b, qwen3:4b, 4 Chat space IDs
- ChatAgent: needs_routing flow, _resolve_space, space_override
- Dashboard: routing buttons for Denver/Greeley
- First successful agent pass: classifications working, PDF extraction working

### Google Chat Spaces
- GOOGLE_CHAT_SPACE_DENVER=AAQAKvzY_ug
- GOOGLE_CHAT_SPACE_GREELEY=AAQA8BLovsk
- GOOGLE_CHAT_SPACE_BENEFITS=AAQAHYMLMyI
- GOOGLE_CHAT_SPACE_BILLING=AAQA1Xh_bR4

---

## Session: 2026-04-16

### Bugs Fixed (all 78/78 tests passing)
1. FIXED: /agent/run blocks uvicorn — now uses BackgroundTasks +
   _poll_lock + _job_state dict + GET /agent/status endpoint
2. FIXED: Duplicate processing — threading.Lock() + is_processed()
   double-check in process_email() via new GmailTool.is_processed()
3. FIXED: stage_chat_message() missing status field — consolidated into
   shared utils.stage_chat_message(), always writes "status": "pending"
4. FIXED: save_summary() never called — now called in BillingAgent and
   ReferralAgent after successful draft creation, wrapped in try/except
5. NOTE: thread_id leakage (Bug 4 from prior session) — not confirmed,
   monitor during beta

### History Ingest Completed
- Command: python -m training.ingest_history --live --max-emails 50 --audit
- Note: --days flag does not exist in CLI; use --max-emails to cap
- Note: must run as module (python -m training.ingest_history) not direct
- Note: requires PYTHONUTF8=1 env var on Windows to avoid cp1252 error on
  ✓ character in validate_hipaa_posture()
- Result: 49/50 ingested, 1 skipped (transient WinError 10060 network timeout)
- email_summaries collection: 100 documents (was 0 before)
- Classification split: billing 47%, referral 35%, internal 14%, unknown 4%
- Summary quality: good — PHI-aware, specific enough for few-shot use

### Beta Testing Pass 1 + Pass 2 Completed
- Pass 1: 38 emails processed, crashed (Ollama stall + thread pool exhaustion)
- Pass 2: 50/50 completed cleanly (5h 28m, one 1hr Ollama stall mid-pass)
- Grand total: 88 emails processed
- email_records: 64 documents
- email_summaries: 100 documents
- staged_chat_messages.json: 66 entries (5 Pass 1, 61 Pass 2)
  - All 66 have "status" field (Bug 3 confirmed fixed)
  - 2 pre-existing Pass 1 duplicates remain (pre-fix, expected)
  - 0 new duplicates in Pass 2 (Bug 2 confirmed fixed)

### Pass 2 Classification Breakdown (50 emails)
- billing: 25 (50%)
- internal: 9 (18%)
- unknown: 9 (18%)
- referral: 7 (14%)

---

## Session: 2026-05-03

### Task A — SKIPPED (resolved without code change)
- Unfilled placeholder tokens in drafts were a non-issue — case manager's
  email signature already contains clinic name, phone, title, etc.
- No config.py or prompt changes needed.

### Task B — COMPLETE (Ollama request timeout)
- Added request_timeout=180 to all ollama.chat() calls
- On timeout: logs email ID + agent name at ERROR level, applies
  Gmail label "agent-timed-out", returns graceful fallback, continues pass
- GmailTool.apply_label() implemented: creates label if missing, caches
  label ID after first lookup, applies via messages.modify

### Task C — COMPLETE (Thread context fetching)
- GmailTool.fetch_thread(thread_id, current_message_id="") added
  - Calls users.threads.get with format="full"
  - Sorts by internalDate (oldest-first)
  - Reuses existing _extract_parts() for body decoding — no duplication
  - Excludes current message, returns [] for single-message threads
  - Wraps in try/except, logs warning and returns [] on any failure
- orchestrator.py: 5 lines added in process_email() after classification,
  fetches thread history and attaches to email dict as thread_history
- billing_agent.py + referral_agent.py: both updated to read
  thread_history from email dict, apply phi_scrub() to each message body,
  inject formatted history block before current email in prompt
  (--- Prior Conversation History --- / --- End History --- / Current Email:)
  Empty history produces identical prompt to before — no regression risk
- 78/78 tests still passing after Task C

### Dashboard Queue Management — COMPLETE
- Problem: 66 stale staged chat messages from beta passes (including
  duplicates) had no way to be cleared from the UI
- Added to main.py:
  - _write_staged(entries) — shared file rewrite helper
  - _backfill_uuids(entries) — assigns UUIDs to entries missing one on load
  - _read_all_staged() — calls _backfill_uuids after reading
  - IdsRequest model — {"ids": [...]} body for selected-entry endpoints
  - DELETE /chat-queue/selected — removes entries by ID list
  - DELETE /chat-queue/duplicates — keeps newest per message-text group
  - DELETE /chat-queue/all — overwrites with []
  - PATCH /chat-queue/reject-selected — sets status: "rejected" by ID list
- Dashboard UI additions:
  - Per-row checkboxes with Select All (supports indeterminate state)
  - Duplicate rows highlighted amber (#fffbeb / #fef3c7 on hover)
  - Toolbar: Clear Duplicates (live count), Clear Selected, Reject Selected,
    Clear All (with "Are you sure?" confirmation)
  - Auto-refresh after every bulk action
  - dupKey() + findDuplicateIds() client-side duplicate detection
- All staged chat entries now have stable UUIDs generated at write time
  in utils.stage_chat_message()

### CLAUDE.md Created
- Added to project root
- Rules: work autonomously on all project files; only ask before deleting
  files/folders or modifying anything outside the project directory
- Includes server launch command and pointer to memory.md

### Known Issues / Open Bugs
1. OPEN: Thread pool exhaustion — dashboard 30s auto-refresh competes with
   long LLM inference threads. Fix: async dashboard endpoints
2. OPEN: SSL WRONG_VERSION_NUMBER errors — dashboard hitting HTTPS routes
   on plain HTTP server. Non-blocking but noisy in logs.

### Planned Next Steps (in order)

#### Immediate
1. Clear stale queue entries using new dashboard bulk tools (Clear Duplicates,
   then Clear Selected for remaining April 14/16 entries)
2. Run a small live pass (10–15 emails) to evaluate draft quality with
   thread context now in place
   - Remove agent-processed label from a handful of emails that have
     real back-and-forth thread history before triggering the pass
   - Launch: ollama serve (separate terminal), then server launch command above
   - Trigger: POST /agent/run or dashboard button

#### SOP Work (Claude chat session, not Claude Code)
3. After live pass, pull 3–4 representative billing and referral email
   chains from Gmail drafts
4. Walk through each with Claude in chat — describe how you'd actually respond
5. Claude builds structured SOP.md covering tone, structure, required fields,
   escalation triggers, and response templates for billing and referral
6. SOP becomes both staff training material AND prompt instructions injected
   into BillingAgent and ReferralAgent system prompts

#### Future / Backlog
- Async dashboard endpoints to fix thread pool exhaustion
- Ollama format parameter for native JSON schema enforcement
- ChromaDB embedding function: switch to bge-m3
- Windows Task Scheduler .bat file for scheduled /agent/run triggers
- API key auth on /agent/run (prerequisite for any external trigger)
- PromptEmrTool: implement when Prompt EMR API access is available
- See ROUTINES_CONSIDERATION.md for Claude Code Routines scheduling option
