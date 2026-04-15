# case-manager-agent

A local-LLM-powered medical case management assistant for chiropractic,
physical therapy, massage, acupuncture, and shockwave clinics. Automates
referral intake, billing triage, and staff notifications — all inference
runs on your own hardware via Ollama. No patient data ever leaves your server.

---

## HIPAA Posture

| Control | Implementation |
|---|---|
| **No PHI to external APIs** | All LLM inference runs through [Ollama](https://ollama.com) on your own hardware. The only outbound calls are to Google's APIs (Gmail, Google Chat) under your Workspace agreement — and only after human approval in the dashboard. |
| **PHI-scrubbed vector store** | [ChromaDB](https://www.trychroma.com) stores only regex-scrubbed, LLM-summarised email descriptions — never full bodies, raw PDFs, or attachment bytes. |
| **Human-in-the-loop** | `DRAFT_MODE=true` (default) means every outbound email and Google Chat message waits for approval in the dashboard before sending. |
| **Local inference only** | `validate_hipaa_posture()` runs at every startup and warns if `OLLAMA_HOST` points outside localhost or a private network, or if cloud LLM API keys are present. |
| **Minimal OAuth scopes** | Gmail: `readonly` + `compose` + `modify` (for labelling). No delete, admin, or Drive scopes. |
| **No temp files** | PDF bytes are processed with `io.BytesIO` — nothing is written to disk during attachment handling. |
| **Audit logging** | [Loguru](https://loguru.readthedocs.io) writes structured logs locally. Email subjects, PDF filenames, and body content are never logged. |

See [HIPAA_POSTURE.md](HIPAA_POSTURE.md) for a full data-flow audit, OAuth scope justifications, and the access-revocation procedure.

> This project is a technical scaffold. Consult your compliance officer and perform
> a formal risk analysis before connecting to live patient data or production systems.

---

## Architecture

```
main.py  (FastAPI + approval dashboard at http://localhost:8000)
  └── OrchestratorAgent
        ├── ReferralAgent   → GmailTool, PdfTool, EmailStore
        ├── BillingAgent    → GmailTool, EmailStore
        └── ChatAgent       → staged-message queue manager

memory/EmailStore      ←→  ChromaDB  (./chroma_db — local disk)
tools/PromptEmrTool         (stub — implement for your EMR system)
training/HistoryIngester    (one-time bulk Gmail ingestion)
```

---

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) installed and running locally
- A Google Cloud project with **Gmail API** enabled (and optionally Google Chat API)
- OAuth 2.0 desktop-app credentials JSON from Google Cloud Console
- 16 GB VRAM (for the recommended model) **or** 8 GB VRAM / CPU fallback

---

## Setup

### 1. Install Ollama and pull the model

```bash
# Install Ollama (macOS/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# Windows: download the installer from https://ollama.com/download

# Recommended model — best quality/VRAM balance (requires ~10 GB VRAM, fits in 16 GB)
ollama pull llama3:70b-instruct-q4_K_M

# Lighter alternative for 8 GB VRAM or CPU-only machines
ollama pull llama3:8b-instruct
```

Set `OLLAMA_MODEL` in `.env` to match the model you pulled:

```ini
OLLAMA_MODEL=llama3:70b-instruct-q4_K_M   # recommended for 16 GB VRAM
# OLLAMA_MODEL=llama3:8b-instruct          # fallback for 8 GB VRAM / CPU
```

### 2. Clone and install Python dependencies

```bash
git clone <your-repo-url>
cd case-manager-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env — minimum required fields are marked below
```

| Variable | Required | Description |
|---|---|---|
| `OLLAMA_HOST` | No | Ollama address (default: `localhost:11434`) |
| `OLLAMA_MODEL` | No | Model name (default: `llama3:70b`) |
| `GOOGLE_CREDENTIALS_PATH` | **Yes** | Absolute path to your OAuth 2.0 client secrets JSON |
| `GMAIL_USER_EMAIL` | **Yes** | Gmail address the agent reads/drafts on behalf of |
| `GOOGLE_CHAT_WEBHOOK_RECEPTIONIST` | No | Incoming webhook URL for the receptionist space |
| `GOOGLE_CHAT_WEBHOOK_BILLING` | No | Incoming webhook URL for the billing team space |
| `DRAFT_MODE` | No | `true` = no sends without approval (default); `false` = live mode |
| `ENABLE_POLLING` | No | `true` = background Gmail poll every 60 s (default: `false`) |

### 4. Set up Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services → Credentials**.
2. Create an **OAuth 2.0 Client ID** of type **Desktop app**.
3. Download the JSON and set `GOOGLE_CREDENTIALS_PATH` to its path.
4. Enable the **Gmail API** (and **Google Chat API** if using webhook notifications).
5. On first run a browser window opens for consent; a `token.json` is saved for subsequent runs.

> **Security**: Add `token.json` to `.gitignore`. Protect it with file-system permissions (read/write for the service user only).

### 5. Ingest historical emails (recommended first-time step)

Run this **before** starting the server. It fetches the last 90 days of emails,
generates PHI-scrubbed summaries, and stores them in ChromaDB for few-shot context.

```bash
# Ingest last 90 days (up to 500 emails) and print an audit report
python main.py --ingest-history

# Ingest more emails
python main.py --ingest-history --max-emails 1000
```

An audit report is saved to `training/audit_report.txt` showing totals by
classification and the date range covered.

### 6. Start the server

```bash
python main.py
# or with auto-reload during development
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Every startup runs `validate_hipaa_posture()` and prints the result.

### 7. Open the approval dashboard

```
http://localhost:8000/
```

The dashboard shows:
- **Gmail Drafts** — one row per pending draft with Date, Type, To, Subject, Preview, and Discard/Open buttons.
- **Google Chat Queue** — staged notifications waiting for Approve & Send or Reject.
- **Status summary** — emails processed today, drafts pending, messages sent today.
- **Run Agent Pass** — manually trigger one Gmail poll cycle.

> The amber "DRAFT MODE" banner is always visible. No message or email is sent until you click **Approve & Send** or the draft is sent from Gmail.

### 8. Enable automatic polling (optional)

```ini
# .env
ENABLE_POLLING=true
```

With polling enabled, the server starts a background thread that checks Gmail
every 60 seconds. New emails are classified and routed automatically; outbound
actions still require dashboard approval when `DRAFT_MODE=true`.

---

## Running the tests

```bash
pip install pytest pytest-mock   # already in requirements.txt
pytest tests/ -v
```

| Test file | What is tested |
|---|---|
| `tests/test_phi_scrub.py` | All `phi_scrub()` regex patterns — 30+ cases covering SSN, DOB, insurance IDs, phones, false-positive prevention |
| `tests/test_classification.py` | Ollama response → classification routing (all 4 categories, punctuation stripping, garbage fallback) |
| `tests/test_draft_creation.py` | Gmail draft creation — correct recipient extraction, subject prefixing, no direct sends, DRAFT_MODE enforcement |

---

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | HTML approval dashboard |
| `/health` | GET | Liveness check + Ollama reachability |
| `/drafts` | GET | JSON list of pending Gmail drafts |
| `/drafts/{id}/discard` | POST | Permanently delete a draft |
| `/staged` | GET | JSON list of all staged Chat messages |
| `/chat/approve/{id}` | POST | Send a staged message to Google Chat |
| `/chat/reject/{id}` | POST | Reject a staged message (body: `{"reason": "..."}`) |
| `/agent/run` | POST | Trigger one Gmail poll pass synchronously |
| `/email` | POST | Process a single email dict through the orchestrator |
| `/chat` | POST | Conversational query via ChatAgent |

Interactive docs: `http://localhost:8000/docs`

---

## Project structure

```
case-manager-agent/
├── agents/
│   ├── orchestrator.py       # Classifies and routes emails
│   ├── referral_agent.py     # Referral intake: PDF extraction, draft reply, Chat notification
│   ├── billing_agent.py      # Billing triage: subtype classification, draft reply
│   └── chat_agent.py         # Internal email processing + staged-message queue manager
├── tools/
│   ├── gmail_tool.py         # Gmail API: fetch, draft, label, list drafts, delete draft
│   ├── pdf_tool.py           # In-memory PDF text extraction + LLM field parsing
│   ├── google_chat_tool.py   # Webhook poster (send_webhook_message is the only send path)
│   └── prompt_emr_tool.py    # EMR integration stub — not yet implemented
├── memory/
│   └── email_store.py        # ChromaDB: email_records + email_summaries collections
├── training/
│   └── ingest_history.py     # HistoryIngester (live Gmail) + HistoryIngestor (file-based)
├── tests/
│   ├── test_phi_scrub.py
│   ├── test_classification.py
│   └── test_draft_creation.py
├── HIPAA_POSTURE.md          # Data-flow audit, OAuth scopes, revocation procedure
├── config.py                 # Pydantic settings + validate_hipaa_posture()
├── main.py                   # FastAPI app, dashboard, CLI (--ingest-history)
├── requirements.txt
└── .env.example
```

---

## EMR integration (stub)

`tools/prompt_emr_tool.py` is a **stub pending API access**. All four methods
(`search_patient`, `get_referral_history`, `create_referral`, `get_insurance`)
raise `NotImplementedError`. To connect to your EMR:

1. Obtain API credentials from your EMR vendor.
2. Implement the four methods using your vendor's SDK or REST API.
3. Inject `PromptEmrTool` into `ReferralAgent.__init__` and call it after `pdf.extract_referral_fields()`.

---

## License

MIT
