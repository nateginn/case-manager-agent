# Case Manager Agent — Project Memory

## What this project is
A local, HIPAA-compliant AI agent system for a chiropractic/PT/massage/acupuncture/shockwave clinic.
Processes incoming emails, drafts replies and internal Google Chat messages, routes referrals and
billing inquiries. Human-in-the-loop approval via a local FastAPI dashboard. No PHI leaves the machine.

## Current status: CLAIRE LIVE — hardened 2026-07-03 (learned sender lists, retries, atomic state, log rotation); changes UNCOMMITTED — see "Current state / handoff" at the bottom of this file

## Tech stack
- Python 3.11, FastAPI, ChromaDB, Ollama
- Google Workspace (Gmail + Google Chat)
- Windows 11, RTX 4090 (24GB VRAM)
- IDE: Windsurf

## Ollama models (all downloaded)
- glm-4.7-flash:latest — **active model for Claire** (OLLAMA_MODEL + OLLAMA_LIGHT_MODEL, use `think=False`)
- qwen3:32b — primary model (ReferralAgent, BillingAgent)
- qwen3:4b — light model (ChatAgent, Orchestrator classifier)
- granite3.2-vision — OCR fallback for scanned fax PDFs
- bge-m3 — embeddings for ChromaDB

## Project location
D:\Dev\dual-agent-core\case-manager-agent-dev\  (own git repo; remote github.com/nateginn/case-manager-agent)

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
$env:PYTHONUTF8=1; .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```
Do NOT redirect stdout to a log file — logging now goes to `logs/app.log` via a
rotating loguru sink (10 MB × 10, zipped), configured in `config.setup_logging()`.
For a detached launch from Git Bash:
```
PYTHONUTF8=1 REQUESTS_CA_BUNDLE="$(pwd)/windows_cacerts.pem" nohup .venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 >/dev/null 2>&1 &
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

---

## Sessions: 2026-05 through 2026-06-25 — Claire Two-Way Email Assistant

### Summary
Jarvis was renamed **Claire** throughout the codebase. A complete two-way Gmail→Chat
assistant now runs live in production. Major architectural additions, several bugs
found and fixed across multiple sessions.

### Agent rename: Jarvis → Claire
- `agents/jarvis_agent.py` → `agents/claire_agent.py`
- All env vars changed from `JARVIS_*` to `CLAIRE_*`
- State file: `memory/claire_state.json`
- Codename "claire" used everywhere in logs and Chat messages

### Claire architecture (agents/claire_agent.py)
- Runs on a 45-second poll loop (`CLAIRE_POLL_INTERVAL_SECONDS=45`)
- Two phases per cycle: `_poll_chat_replies()` then `_scan_new_emails()`
- State persisted to `memory/claire_state.json`; guarded by `threading.Lock`
- Two Google accounts:
  - `casemanager.art@gmail.com` (token.json) — reads Gmail inbox
  - `cm.assistant.art@gmail.com` (assistant_token.json) — sends/reads Chat alerts

### Batch notification queue (major feature)
- `CLAIRE_NOTIFICATION_BATCH_SIZE=10` — first 10 notifications sent immediately
- Remaining threads serialized to `state["notification_queue"]`
- Queue drains ONLY via explicit "next 10" / "next batch" / "more" user command
- `queued_thread_ids` set built from queue each cycle — prevents re-scanning queued threads
- Summary card: "📬 Showing 10 of 36 emails. Say *next 10* to see 10 more."

### Self-message filtering (prevents Claire triggering herself)
- `_processed_chat_names`: set of message resource names sent by Claire this session
- `_CLAIRE_MSG_PREFIXES`: tuple of known prefixes for cross-restart protection
- `_send_tracked()`: wrapper that adds returned message name to `_processed_chat_names`
- `is_own` check: sender_email == bot email OR sender_type == BOT OR startswith prefix
- All status/system messages sent via `_send_tracked()` (not raw `send_message_full`)

### Junk email detection (_is_junk)
Three-layer approach (fastest/most reliable first):
1. **Automated sender check**: `_NOREPLY_RE` regex on sender — always skip, never classify
2. **Trusted domain pre-filter**: `_TRUSTED_SENDER_RE` — @marrick.com, @movedocs.com,
   @healthsps.com, @medrisknet.com, @medhub.health, @provepartners.com, @zocdoc.com,
   @marrickbilling.com → immediately "work", no LLM call
3. **Work subject pre-filter**: `_WORK_SUBJECT_RE` — PAV, EOB, authoriz, scheduling,
   patient, billing, insurance, referral, DOB, DOI, urgent, script, dates, etc. → "work"
4. **GLM fallback** (ambiguous only): prompt asks work vs junk, `think=False`,
   `num_predict=5` — defaults to "work" when uncertain ("only reply junk if clearly confident")

### seen_junk_threads — prevents junk re-classification each cycle
- `state["seen_junk_threads"]`: list of thread_ids classified as junk
- Loaded each cycle; threads in set are silently skipped (no LLM call)
- When new junk found, thread_id added to set; persisted after scan
- **"skip" on junk batch card** removes those thread_ids from `seen_junk_threads`
  so they re-enter the work queue next cycle
- Wiped by "new day" command

### New Day command ("new day" / "clean start" / "fresh start" / "start fresh")
- Calls `google_chat_tool.delete_all_messages()` (paginates, 0.15s delay per delete)
- Wipes state to `{"emails": {}, "junk_batches": {}, "notification_queue": [], "seen_junk_threads": []}`
- Clears `_processed_chat_names` in memory
- Sends "🌅 Fresh start — N messages cleared. Claire is watching your inbox."
- Next cycle delivers fresh first 10 from inbox

### cleanup_chat.py — standalone bulk-delete script
- `python cleanup_chat.py --dry-run` to preview
- `python cleanup_chat.py` to actually delete
- Defaults to CLAIRE_ALERT_SPACE_ID; Denver/Greeley return 403 (assistant not a member — expected)
- Uses `assistant_token.json` (CHAT_ONLY_SCOPES)

### Bugs found and fixed
1. **Queue auto-drain bug**: `_scan_new_emails` only checked `state["emails"]` to skip threads,
   not `state["notification_queue"]`. Queued threads re-scanned every 45s, delivering
   10 more cards automatically. Fixed by building `queued_thread_ids` set from queue
   at scan time and skipping any thread_id in that set.

2. **Self-triggering "next 10" bug**: Claire's own "📬 Showing 10 of 25..." message
   matched the "more"/"next 10" keywords in `_poll_chat_replies`, triggering automatic
   batch delivery. Fixed via `_send_tracked()` + `_CLAIRE_MSG_PREFIXES` prefix check.

3. **Junk re-classification trickle**: Junk emails never tracked in state, so GLM
   re-evaluated them every 45 seconds. If it flipped to "work", a new card appeared.
   Fixed by `seen_junk_threads` in state (see above).

4. **Junk false positives**: Work emails from clinic senders misclassified as junk
   by GLM. Fixed by adding trusted domain + work subject keyword pre-filters and
   strengthening the GLM prompt to "default to work when uncertain".

### GLM model notes
- All Claire LLM calls use `think=False` — GLM 4.7 Flash returns content in `response` field
- Classification calls: `num_predict=5` (single-word answer)
- Summary calls: `num_predict=120`
- Suggested action calls: `num_predict=10`
- Do NOT use `thinking=False` (wrong param) — use `think=False` in generate() options

### Current .env for Claire
```ini
OLLAMA_MODEL=glm-4.7-flash:latest
OLLAMA_LIGHT_MODEL=glm-4.7-flash:latest
CLAIRE_ENABLED=true
CLAIRE_POLL_INTERVAL_SECONDS=45
CLAIRE_ALERT_SPACE_ID=AAQABJJCGpA
CLAIRE_ASSISTANT_TOKEN_PATH=assistant_token.json
CLAIRE_REPLY_TIMEOUT_HOURS=4
CLAIRE_NUDGE_DAYS=2
CLAIRE_NOTIFICATION_BATCH_SIZE=10
```

### Known issues / open items
1. **Gmail API overhead**: fetches 50 full email details every 45 seconds regardless
   of inbox changes (~4000+ calls/hour). A message-ID cache or Gmail history API
   would reduce this significantly. Not urgent but worth doing.
2. **Junk batch "skip" flow**: When user skips a junk batch, those threads re-enter
   normal classification next cycle. With the improved prompt they should pass as
   "work". But they won't be queued — they'll be sent immediately in next cycle's
   batch (up to 10 slots available). This is correct behavior.
3. **seen_junk_threads grows indefinitely**: Until "new day" is run, the list grows
   one entry per unique junk thread. No practical problem for a clinic inbox size.
   Could be pruned against current inbox if needed.

### Planned next steps
1. **Tune trusted domain list**: As new work email senders are identified (law firms,
   medical groups, insurers), add their domains to `_TRUSTED_SENDER_RE` in claire_agent.py
2. **Tune work subject regex**: Add subjects that GLM keeps misclassifying
3. **Gmail history API**: Replace full 50-email fetch with incremental `history.list`
   using a saved `historyId` — would eliminate ~95% of Gmail API calls
4. **"not junk" command**: Let user reply to a junk batch with "not junk" as an
   alternative to "skip" — clearer semantics, same effect
5. **Reply timeout handling**: Threads in "waiting" status that never get a reply
   currently expire after CLAIRE_REPLY_TIMEOUT_HOURS. Consider adding a "remind me
   tomorrow" command that extends the timeout.

---

## Session: 2026-07-03 — Accuracy + Reliability hardening (Claude Code)

### What changed
1. **Learned sender lists** (`agents/sender_lists.py`, data in
   `memory/claire_learned_senders.json`, human-editable JSON):
   - "trash all" on a junk batch → senders blocklisted (address always;
     domain too unless free-mail like gmail.com)
   - "skip" → senders allowlisted (addresses only — weaker signal)
   - Consulted FIRST in `_is_junk()` (before trusted-domain regex and GLM);
     loaded once per scan cycle. Latest decision wins; allow beats block at
     equal specificity. Claire confirms learning in her reply.
2. **Self-message filtering by message ID**: Claire now persists the resource
   names of every Chat message she sends (`sent_message_names` in
   claire_state.json, capped 500) and seeds the in-memory set on startup.
   Prefix check (`_CLAIRE_MSG_PREFIXES`) is now a last-resort fallback used
   only when the API returns no sender identity — user messages starting
   with "Done —" etc. are no longer swallowed after a restart. The
   `"→ Reply:"` / `"💡 Suggested:"` substring checks were removed.
3. **Bounded retries** (`utils.retry_call`, transient = 429/5xx/timeouts):
   - Gmail tool: list/get/modify/trash wrapped (retries=2, backoff)
   - Chat tool: send/reply/list wrapped (retries=2)
   - Ollama: classify retries=2, Claire light-model calls retries=1
4. **Bounded re-attempts for failed emails** (`memory/timeout_retries.json`):
   - Classification failure: retried up to MAX_TIMEOUT_RETRIES=3 with a
     15-min cooldown between polls, then parked (agent-processed + label)
   - Agent-stage failure: previously parked forever after ONE failure — now
     returns `error_will_retry` and is retried up to 3 attempts too
   - Success clears bookkeeping and removes the agent-timed-out label
   - Manual reset: `POST /timeouts/reset`
5. **Atomic state writes** (`utils.atomic_write_json`, temp+os.replace):
   claire_state.json, staged_chat_messages.json, timeout_retries.json —
   a crash mid-write can no longer corrupt state.
6. **Log rotation** (`config.setup_logging()`): loguru sink at logs/app.log,
   10 MB rotation, keep 10, zip compression, diagnose=False (HIPAA — no
   local-var dumps). Uvicorn std-logging intercepted into the same sink
   (`log_config=None`). Stop redirecting stdout to uvicorn*.log.
7. `assistant_token.json` added to .gitignore; `GmailTool.remove_label()` added.

### Tests
- 101 passing (was 84): new tests/test_retry.py, tests/test_sender_lists.py
- conftest.py now isolates timeout_retries.json to tmp_path (autouse fixture)
- test_agent_exception_sets_error_status updated for retry semantics

### Verification still to do live (needs Ollama + real spaces)
- Reply "trash all"/"skip" to a junk batch → check claire_learned_senders.json
- Restart server mid-conversation, send a message starting with "Done —" →
  Claire should respond, not swallow it
- Confirm logs/app.log rotates (server must be started WITHOUT shell
  redirection to old uvicorn.log files)

---

## Current state / handoff — 2026-07-03 21:10 (for the next Claude Code instance)

### Server
- RUNNING detached on 127.0.0.1:8000 (started 2026-07-03 ~21:03, PID 56704 at
  launch — verify with `netstat -ano | findstr :8000`). Health endpoint OK,
  Claire active (45s cycle), Ollama reachable, draft mode on.
- Logs: `logs/app.log` (rotating). The old `uvicorn*.log` files in the repo
  root are dead — no longer written to; safe to delete after user confirms.

### Uncommitted work (IMPORTANT)
All of the 2026-07-03 hardening (see session entry above) is UNCOMMITTED in
this repo. Modified: .gitignore, README.md, config.py, main.py, utils.py,
agents/orchestrator.py, tools/gmail_tool.py, tools/google_chat_tool.py,
tests/conftest.py, tests/test_classification.py, memory.md.
Untracked (never committed — includes the whole Claire feature, predating
this session): agents/claire_agent.py, agents/jarvis_agent.py,
agents/monitoring_agent.py, agents/sender_lists.py, tools/auth_assistant.py,
CLAUDE.md, ROUTINES_CONSIDERATION.md, cleanup_chat.py, tests/test_retry.py,
tests/test_sender_lists.py, .claude/, test_ollama.py, test_thread.py, and
memory/*.json runtime state files (claire_state, jarvis_state, sent_alerts,
staged_chat_messages, learn_report, timeout_retries — consider gitignoring
the runtime ones rather than committing them).
User wants to verify live behavior before committing — ASK before committing.

### Verification checklist (pending live confirmation by user)
1. Junk learning: reply "trash all" to a junk batch card → senders should
   appear in memory/claire_learned_senders.json blocklist and Claire's reply
   should mention "Learned". Reply "skip" → allowlist.
2. Self-message fix: after any restart, a user message starting with "Done —"
   or "📬" must get a response (previously swallowed by prefix filter).
3. Retry/park: memory/timeout_retries.json tracks failed emails (max 3
   attempts, 15-min cooldown). Manual reset: POST /timeouts/reset.
4. Test suite: 101 passing as of 2026-07-03 (`.venv\Scripts\python.exe -m pytest tests/`).

### Known follow-ups (not started, in priority order from the 2026-07-03 review)
1. Gmail History API incremental sync (biggest efficiency win, ~95% fewer API calls)
2. Remove legacy agents/jarvis_agent.py (509 lines, superseded by Claire) +
   memory/jarvis_state.json — DELETION, so ask user first
3. Integration tests with mocked Google APIs; PHI scrub gaps (patient names, MRNs)

## 2026-07-06 (late) — Visit-inquiry expansion: MedHub / ProvePartners / multi-patient

Built in the DEV copy only (`case-manager-agent-dev/`); NOT yet deployed to
production (`D:/Dev/case-manager-agent/`).

**Inbox mining first**: categorized 185 inbox emails from the last 3 weeks
(script: `mine_inbox.py`, read-only). Top categories: Marrick auth lifecycle
(~33), visit/scheduling inquiries (~23, incl. ~11 MedHub/ProvePartners
templated forms), billing/EOB (~20), attorney lien/balance (~15), PAV (~13).
User picked MedHub/ProvePartners extension as the next build.

**Changes (agents/visit_inquiry.py + tests/test_visit_inquiry.py)**:
- Classifier prompt broadened: treatment-status updates (date of last visit /
  next appointment / visits-to-date), scheduled-confirmation asks, and care
  coordination / attendance confirmation forms now count as visit inquiries.
  Billing/records/auth-paperwork still excluded.
- Multi-patient: classifier returns `patient_names` (list, capped at 4 by
  `_MAX_PATIENTS`); `try_handle` looks all of them up in ONE Playwright EMR
  session; reply has a per-patient section; patients not found in the EMR get
  "We have no record of this patient at our clinic." `result["patient_name"]`
  is now a comma-joined display string (claire_agent unchanged).
- Reply template gained a summary block per patient answering MedHub's exact
  ask: "Date of last visit / Next scheduled appointment / Visits completed to
  date" (completed = stage contains "complete"; falls back to count if the
  EMR gave no stages). Last/next chosen by parsed date, not list order.
- classify body window 2000→3000 chars, num_predict 100→200.

**Validation**: 117 unit tests passing (+8 new). Live dry-run of the
classifier against 23 real inbox emails (`scratch_dryrun_classify.py`,
read-only): 23/23 correct, incl. 3 patients extracted from a MedHub
companions email and 2 from a ProvePartners combined form; all 9 negative
cases (PAV, EOB, lien, records, credentialing, auth cancellation, reduction
offer) rejected. Both medhub.health and provepartners.com are already
trusted sender domains, so these emails reach the pipeline.

**Deploy note**: copy CODE ONLY (agents/visit_inquiry.py,
tests/test_visit_inquiry.py) to production — never memory/*.json, token.json,
assistant_token.json, prompt_emr_session.json, .env (production runtime state
is live and ahead). Production restart requires user OK.

## 2026-07-06 (later) — Marrick PAV auto-forward to billing (dev only, NOT deployed)

User Q&A that shaped this: (a) visit-line columns stay as-is (no provider/
facility/visit# re-added); (b) PAV forwards are DRAFTS for approval, not
auto-send — Claire still never sends email; (c) a "visit check <patient>"
Chat command was confirmed feasible and user wants it, but PAV was built
first — the command is the NEXT build.

**New: agents/pav_request.py** — deterministic (no LLM): sender @marrick.com
+ \bPAV\b or "patient account verification" in subject/body[:2000]. Creates a
forward draft to settings.CLAIRE_BILLING_FORWARD_TO (Brittney,
brittneymccarty.abc@gmail.com, greeting name CLAIRE_BILLING_FORWARD_NAME
"Brit") asking her to reply-all in the same email string when the PAV is
returned; carries original body + ALL attachments (incl. harmless inline
signature images); threads into the same Gmail conversation. Chat card:
"📄 Marrick PAV request auto-handled". Kill switch:
CLAIRE_PAV_FORWARD_ENABLED (default true). Dedup: state["pav_requests"]
per thread, mirrors visit_inquiries. Failure → falls through to normal
notification with a "_pav_note".

**GmailTool additions**: attachment_parts (filename/mime_type/attachment_id)
now captured on every fetched email; fetch_message_attachments(email) →
[{filename, mime_type, data}]; create_draft(attachments=[...]) builds
multipart/mixed. attachment_filenames still present (referral_agent etc.
unchanged).

**Validation**: 129 tests passing (+12 new in tests/test_pav_request.py).
Detector dry-run over all 185 mined emails: 11/11 real PAV requests fired,
0 false positives (38 other Marrick emails + 146 non-Marrick all skipped).

**Deploy set for both 2026-07-06 features (code-only)**: agents/visit_inquiry.py,
agents/pav_request.py, agents/claire_agent.py, tools/gmail_tool.py, config.py,
tests/test_visit_inquiry.py, tests/test_pav_request.py. Restart needs user OK.

## 2026-07-07 — Flexible patient-name matching + DOB confirmation (dev only, NOT deployed)

Motivating failure: email asked about "Ariadne Alejandra Orozco Mendoza";
EMR chart is "Ariadne Orozco Mendoza" (no second given name) → old
search_patient typed the full name verbatim, got 0 results, and blindly
clicked `.first` when results DID exist.

**tools/prompt_emr_browser_tool.py**:
- `_name_variants(name)` — progressive query shortening: full name → drop
  2nd given name → drop 2nd given + 1st surname → "first last-token".
  Never single-token.
- `_score_name_match(emr_name, email_name)` — fraction of EMR-card tokens
  found in the email name; hard 0.0 if EMR first name absent from email
  name (surname overlap can never select). Threshold `_MATCH_THRESHOLD`=0.67.
- `search_patient(patient_name, dob="")` — scores ALL result cards (no more
  blind `.first`); if >1 candidate AND email provided a DOB, opens each
  best-first and keeps the one whose profile DOB matches
  (`_read_profile_dob`, regex over body for DOB:/Date of Birth:); if DOB
  confirms nobody, falls back to best name match. Result dict now includes
  "dob". `get_patient_visits` gained `dob=` passthrough.

**agents/visit_inquiry.py**: classifier prompt now also extracts
`patient_dobs` (aligned with patient_names, verbatim from email);
`_parse_patient_dobs` normalizes/pads. `try_handle` zips names+dobs into
`get_patient_visits(name, dob=dob)`.

**utils.py**: new `normalize_dob(raw) -> "MM/DD/YYYY" | ""` shared by both
files (handles ISO, US slash/dash/dot, 2-digit years, month names).

**Validation**: 148 tests passing (+19). Live classifier dry-run: DOBs
correctly extracted+normalized from MedHub ISO format, ProvePartners
US format, matlin inline, and subject-line DOBs (4/4 emails). Live EMR
search 2026-07-07 00:26: query "Ariadne Alejandra Orozco Mendoza" resolved
via variant 2 to chart "Ariadne Orozco Mendoza" (acct 1005472-ARR) and the
profile DOB read back 09/18/2007 matching the email — PASS (~11s).

Session cache note: dev's prompt_emr_session.json expired ~00:18 and was
refreshed via headed login (user clicked human-verify) at 00:25. PRODUCTION
has its OWN session file — if production Chat notes say EMR lookup failed,
rerun `test_prompt_emr_login.py --headed` from D:/Dev/case-manager-agent/.

**Deploy set now also includes**: tools/prompt_emr_browser_tool.py, utils.py,
tests/test_prompt_emr_matching.py (on top of the 2026-07-06 list).
