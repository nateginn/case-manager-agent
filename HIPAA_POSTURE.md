# HIPAA Posture Documentation

> This document describes the data handling posture of `case-manager-agent` as it
> relates to HIPAA Privacy and Security Rule requirements. It is intended for the
> compliance officer, system administrator, and any auditor reviewing the deployment.
>
> **This project is a technical scaffold.** Consult your compliance officer and
> perform a formal risk analysis before connecting to live patient data or production
> systems. This document describes what the code does, not a legal compliance certification.

---

## 1. What is stored locally

### ChromaDB summaries (`./chroma_db/`)

The only patient-adjacent data written to disk is in two ChromaDB collections:

| Collection | What is stored | PHI content |
|---|---|---|
| `email_records` | Full JSON records from processed emails (body text + structured fields) | **Present** — stored only during active processing; not scrubbed |
| `email_summaries` | 2–3 sentence LLM-generated summaries of processed emails | **Scrubbed** — `phi_scrub()` is applied before storage AND to the LLM output |

**`email_summaries` PHI scrubbing pipeline** (`training/ingest_history.py`):
1. Source text is passed through `phi_scrub()` before reaching the LLM (redacts SSNs, labelled DOBs, labelled insurance IDs, phone numbers).
2. The LLM is instructed via system prompt to produce summaries that contain **no patient names, DOBs, or insurance IDs**.
3. The LLM output is passed through `phi_scrub()` a second time before being written to ChromaDB.

### Staged Google Chat messages (`memory/staged_chat_messages.json`)

Staged notifications contain patient names, DOBs, and provider names. This file:
- Stays on-device only (never transmitted unless manually approved via the dashboard).
- Should be treated as PHI and included in your facility's media handling/disposal policy.
- Contains a `"sent": false` flag until a human approves the message via the dashboard.

### OAuth token (`token.json`)

Contains a Google OAuth 2.0 access and refresh token for the clinic's Gmail account. This is not PHI but is a sensitive credential. It should be:
- Excluded from version control (add to `.gitignore`).
- Protected with filesystem-level access controls (readable only by the service account user).
- Rotated or revoked via `GmailTool.delete_local_cache(revoke_token=True)` when rotating credentials.

---

## 2. What is never stored

| Data | Why it is never stored |
|---|---|
| Full email bodies | Fetched from Gmail API into memory only; never written to disk or ChromaDB |
| Raw PDF bytes | Processed via `io.BytesIO` in memory; never written to a temp file |
| Attachment bytes | Fetched per-attachment into memory; discarded after text extraction |
| LLM prompt inputs | Passed to local Ollama in-process; not persisted anywhere |
| LLM raw output | Used transiently; only the PHI-scrubbed summary is persisted |
| Patient names in logs | Removed during the HIPAA hardening pass; logs contain only message IDs, classifications, and counts |
| Email subjects in logs | Removed (subjects can contain patient-identifying information) |
| PDF filenames in logs | Removed (filenames such as `John_Smith_Referral.pdf` are PHI) |

---

## 3. What leaves the machine

**Nothing, under normal operation.**

| Destination | Data sent | Notes |
|---|---|---|
| Google Gmail API | OAuth token refresh requests (no email content in auth calls); draft creation (email body sent to Gmail API under your Workspace agreement) | Only in response to human approval in the dashboard |
| Google Chat API | Staged notification text (patient name, DOB, provider) | **Only when a human clicks "Approve & Send"** in the dashboard |
| Local Ollama | Email body text, PDF text, referral fields | Stays on-machine; Ollama listens on `localhost:11434` by default |
| ChromaDB | PHI-scrubbed summaries only | Stays on-machine; `./chroma_db/` is a local directory |

> `validate_hipaa_posture()` (called on every startup) checks that `OLLAMA_HOST`
> resolves to a local or RFC-1918 private address and warns if a cloud LLM API
> key is present in the environment.

---

## 4. Google OAuth scopes

| Scope | Why it is needed |
|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | Fetch unread emails and attachment metadata |
| `https://www.googleapis.com/auth/gmail.compose` | Create Gmail drafts |
| `https://www.googleapis.com/auth/gmail.modify` | Apply the `agent-processed` label to skip already-processed emails on re-poll |

**Not requested:**
- `gmail.delete` — the agent never deletes emails.
- `gmail.admin` / any admin scope — the agent operates as a single delegated user only.
- Any Google Drive, Docs, Sheets, or Calendar scope.

---

## 5. How to revoke access

### Revoke via the application

```python
from tools.gmail_tool import GmailTool

gmail = GmailTool()
gmail.delete_local_cache(revoke_token=True)
# Calls Google's token revocation endpoint then deletes token.json
```

### Revoke manually via Google Account

1. Go to **myaccount.google.com → Security → Third-party apps with account access**.
2. Find the OAuth client named after your Cloud project.
3. Click **Remove access**.

This invalidates all tokens issued to this application. The local `token.json` can then be deleted manually.

### Revoke via Google Cloud Console

1. Go to **console.cloud.google.com → APIs & Services → Credentials**.
2. Delete or disable the OAuth 2.0 client ID used by this application.
3. All tokens issued under that client ID are immediately invalidated.

---

## 6. Minimum access checklist

Before connecting to a production environment:

- [ ] `DRAFT_MODE=true` in `.env` (verified by `validate_hipaa_posture()` at startup).
- [ ] `OLLAMA_HOST` is `localhost:11434` or a private network address.
- [ ] No cloud LLM API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) in the environment.
- [ ] `token.json` is excluded from version control (`.gitignore`).
- [ ] `token.json` file permissions: readable only by the OS user running the agent.
- [ ] `./chroma_db/` directory permissions: readable/writable only by the OS user running the agent.
- [ ] `memory/staged_chat_messages.json` is on an encrypted volume or included in your PHI data-at-rest controls.
- [ ] A Business Associate Agreement (BAA) is in place with Google for Gmail/Workspace use.
- [ ] Your Ollama model (`llama3:70b-instruct-q4_K_M` recommended) is deployed on hardware under your physical control.
- [ ] Loguru log output is routed to a SIEM or access-controlled log store.

---

## 7. Incident response

If you believe PHI may have been logged or transmitted outside the expected boundaries:

1. Stop the agent: `kill $(lsof -ti :8000)` or stop the systemd/docker service.
2. Revoke the OAuth token: `GmailTool().delete_local_cache(revoke_token=True)`.
3. Review Loguru log output for any unexpected content.
4. Review `memory/staged_chat_messages.json` for unsent messages.
5. Notify your Privacy Officer within the timeframe required by your HIPAA incident response policy (typically 60 days for breach notification).
