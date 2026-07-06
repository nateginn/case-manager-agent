Here's a markdown file you can save as `ROUTINES_CONSIDERATION.md` in your project root:

---

```markdown
# Claude Code Routines — Future Integration Consideration

## What This Document Is
A planning note for a potential future enhancement to the Case Manager Agent.
Not on the active roadmap. Revisit after beta testing is stable and live polling
is confirmed working.

---

## Background
On April 14, 2026, Anthropic launched **Claude Code Routines** (currently in
research preview). Routines are saved Claude Code configurations — a prompt,
repositories, and connectors — that run automatically on Anthropic-managed cloud
infrastructure on a schedule, API trigger, or GitHub event.

More info: https://code.claude.com/docs/en/routines

---

## The HIPAA Boundary — What Routines Can and Cannot Do Here

This project has a hard architectural requirement: **no PHI leaves the local
machine.** That constraint does not change.

| Layer | Where it runs | PHI involved | Routines eligible? |
|---|---|---|---|
| Email fetch + classify | Local (Ollama/qwen3) | Yes | ❌ No |
| PDF extraction | Local (PyPDF2/granite) | Yes | ❌ No |
| Draft generation | Local (Ollama/qwen3) | Yes | ❌ No |
| ChromaDB reads/writes | Local | Yes (scrubbed) | ❌ No |
| **Polling trigger** | **Could be remote** | **No** | **✅ Yes** |
| **Health check / status ping** | **Could be remote** | **No** | **✅ Yes** |
| **Audit log summary** | **Could be remote** | **No (counts only)** | **✅ Yes** |

The local LLM pipeline stays local. Routines would only ever touch the
**trigger layer** — firing an HTTP POST to kick off a local agent pass, or
reading a non-PHI status endpoint.

---

## Proposed Use Case: Scheduled Polling Trigger

### Current behavior
Live polling must be manually enabled in `.env` (`POLLING_ENABLED=True`) or
triggered via `POST /agent/run` from the dashboard. The server must be running
and someone must initiate the pass.

### With a Routine
A Claude Code Routine on a **scheduled trigger** (e.g. every weekday at 8 AM,
noon, and 4 PM) could POST to the local `/agent/run` endpoint:

```
POST http://<clinic-internal-IP>:8000/agent/run
Authorization: Bearer <local_api_key>
```

This would:
- Remove the need for manual polling initiation
- Allow the agent to run on a predictable schedule without `POLLING_ENABLED`
  keeping a permanent thread alive
- Keep all PHI processing entirely local — the Routine only fires an HTTP
  trigger, it never sees email content

### Prerequisites before implementing
- [ ] Add `API_KEY` authentication to `/agent/run` endpoint (currently
      unauthenticated — fine for local-only, required before any external trigger)
- [ ] Expose the FastAPI server on the clinic's internal network (not public
      internet) with a stable internal IP or hostname
- [ ] Confirm clinic network policy allows inbound HTTP from Anthropic
      infrastructure, or use a lightweight local webhook receiver instead
- [ ] Routines must graduate from research preview and stabilize API surface
      (currently under `experimental-cc-routine-2026-04-01` beta header)

---

## Alternative: Local Scheduler (No Routines)
If Routines are not suitable (network policy, HIPAA auditor concern about any
external call touching clinic infrastructure, or feature still in preview),
the same scheduled trigger behavior can be achieved entirely locally with
Windows Task Scheduler calling a simple `.bat` file:

```bat
:: trigger_agent_pass.bat
curl -s -X POST http://127.0.0.1:8000/agent/run
```

Scheduled via Task Scheduler to run at desired intervals. No external
dependency, no network exposure, fully air-gapped. This is the recommended
path until Routines stabilizes and the network/HIPAA questions are resolved.

---

## Secondary Use Case: Audit Log Routine (Low Priority)

A weekly scheduled Routine could POST to a `/agent/audit-summary` endpoint
(to be built) that returns only non-PHI counts:

```json
{
  "week": "2026-W16",
  "emails_processed": 47,
  "referral": 18,
  "billing": 22,
  "internal": 5,
  "unknown": 2,
  "drafts_approved": 31,
  "drafts_rejected": 4
}
```

The Routine could post this summary to a Google Chat space or Slack channel
for the clinic manager without any PHI ever leaving the machine. This is a
clean use case with no compliance risk.

---

## Recommended Decision Path

```
Beta testing stable?
        │
        ▼
Live polling confirmed working?
        │
        ▼
Add API key auth to /agent/run    ← small code change, do this regardless
        │
        ▼
Does clinic network allow         ── No ──► Use Windows Task Scheduler
inbound from Anthropic infra?               (simpler, fully local)
        │
       Yes
        │
        ▼
Has Routines graduated from       ── No ──► Wait, use Task Scheduler in
research preview?                           the interim
        │
       Yes
        │
        ▼
Implement Routine scheduled trigger
for /agent/run + audit summary
```

---

## Current Status
- [ ] Not started — monitoring Routines research preview for stabilization
- [ ] Prerequisite: API key auth on `/agent/run` (recommend adding regardless
      of Routines decision — good practice for any externally-triggered endpoint)
- [ ] Revisit: after 30 days of stable beta operation

---

*Last updated: 2026-04-15*
```

---

Save this as `ROUTINES_CONSIDERATION.md` in the project root. It won't clutter your main `README.md` but gives you a clear decision record to come back to. The Windows Task Scheduler fallback is worth doing soon regardless — it's 10 minutes of work and solves the manual polling problem without any external dependency.