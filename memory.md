# Case Manager Agent — Project Memory

## What this project is
A local, HIPAA-compliant AI agent system for a chiropractic/PT/massage/acupuncture/shockwave clinic.
Processes incoming emails, drafts replies and internal Google Chat messages, routes referrals and
billing inquiries. Human-in-the-loop approval via a local FastAPI dashboard. No PHI leaves the machine.

## Current status: BUILD COMPLETE — moving into setup/beta testing phase

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
- GmailTool — OAuth2, fetch emails, create drafts, mark processed, list/delete drafts
- PdfTool — PyPDF2 text extraction + Ollama structured field extraction + granite3.2-vision fallback
- GoogleChatTool — webhook sender (only function that actually sends)
- PromptEmrTool — stub, pending Prompt EMR API access

## Memory / storage
- ChromaDB: two collections — email_records and email_summaries (PHI-scrubbed)
- Staged Chat messages: memory/staged_chat_messages.json
- Embeddings: bge-m3 (needs to be wired in — not yet updated from default)

## Key design decisions
- DRAFT_MODE=True always — nothing sends without human approval
- phi_scrub() runs twice: before sending to Ollama AND on Ollama output
- Gmail label "agent-processed" prevents re-processing across restarts
- Three-strategy JSON parse fallback in all agents (direct parse → regex → null skeleton)
- Ollama structured output format parameter should be added (not yet done)
- staged_chat_messages.json uses read-modify-write, not append, for valid JSON

## What still needs to be done (in order)
1. Google Cloud Console — create project, enable Gmail API + Google Chat API, create OAuth credentials, download credentials.json  ← NEXT STEP
2. Google Chat webhook URLs — create webhooks for receptionist space and billing space
3. Code updates needed:
   - config.py: add OLLAMA_LIGHT_MODEL=qwen3:4b field
   - ChatAgent + Orchestrator: wire in qwen3:4b for light tasks
   - PdfTool: add granite3.2-vision fallback when PyPDF2 returns empty text
   - ChromaDB embedding function: switch to bge-m3
   - All agents: add Ollama format parameter for native JSON schema enforcement
4. Run pytest tests/ — confirm all three test files pass
5. Update .env with correct values
6. Run --ingest-history on 1-3 months of historical email
7. Review training/audit_report.txt
8. Manual beta testing via POST /agent/run
9. Enable live polling only after draft quality is confirmed

## HIPAA posture
- validate_hipaa_posture() runs on startup
- HIPAA_POSTURE.md documents all data flows
- No cloud LLM calls anywhere in codebase
- PHI never logged — only message IDs, timestamps, classifications

## Files of note
- main.py — FastAPI app, dashboard at GET /, polling thread
- config.py — pydantic BaseSettings, loads from .env
- training/ingest_history.py — one-time historical email ingestion
- HIPAA_POSTURE.md — compliance documentation
- memory/staged_chat_messages.json — pending Chat messages
- training/audit_report.txt — generated after ingestion