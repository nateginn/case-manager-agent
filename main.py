"""
case-manager-agent — FastAPI entry point and human-in-the-loop dashboard.

Endpoints:
  GET  /              — HTML approval dashboard (drafts + staged chat queue)
  POST /chat/approve/{id} — Approve and send a staged Google Chat message
  POST /chat/reject/{id}  — Reject a staged message with a reason
  GET  /staged        — JSON list of all staged chat messages (all statuses)
  GET  /drafts        — JSON list of pending Gmail drafts
  POST /drafts/{id}/discard — Permanently delete a Gmail draft
  POST /agent/run     — Trigger a single agent polling pass
  POST /email         — Process a single pre-fetched email dict
  POST /chat          — Conversational interface via ChatAgent
  GET  /health        — Liveness + Ollama reachability check

Background polling:
  Set ENABLE_POLLING=true in .env to start the orchestrator polling loop
  as a daemon thread on startup.  Defaults to False for development.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests as http_requests
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel

from config import settings, validate_hipaa_posture
from agents.orchestrator import OrchestratorAgent, ProcessingResult
from agents.chat_agent import ChatAgent

# ---------------------------------------------------------------------------
# Staged messages — read directly from file (avoids circular init overhead)
# ---------------------------------------------------------------------------

_STAGED_PATH = Path(__file__).parent / "memory" / "staged_chat_messages.json"


def _write_staged(entries: list[dict]) -> None:
    """Rewrite staged_chat_messages.json with *entries*."""
    _STAGED_PATH.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _backfill_uuids(entries: list[dict]) -> list[dict]:
    """Ensure every entry has a stable 'id'; rewrites the file if any were missing."""
    import uuid as _uuid
    modified = False
    for entry in entries:
        if not entry.get("id"):
            entry["id"] = str(_uuid.uuid4())
            modified = True
    if modified:
        try:
            _write_staged(entries)
        except OSError as exc:
            logger.warning("backfill_uuids: write failed: {}", exc)
    return entries


def _read_all_staged() -> list[dict]:
    """Read the raw staged messages JSON file; return empty list on any error."""
    if not _STAGED_PATH.exists():
        return []
    try:
        data = json.loads(_STAGED_PATH.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else []
        return _backfill_uuids(entries)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read staged messages for dashboard: {}", exc)
        return []


# ---------------------------------------------------------------------------
# Application lifecycle — background polling thread
# ---------------------------------------------------------------------------

_poll_thread: threading.Thread | None = None
_orchestrator: OrchestratorAgent | None = None
_chat_agent: ChatAgent | None = None

# Prevents two manual /agent/run calls from running simultaneously.
_poll_lock = threading.Lock()
_job_state: dict = {"running": False, "last_status": None}


def _polling_worker(interval: int = 60) -> None:
    """Daemon thread body: poll Gmail once per interval seconds."""
    logger.info("Background polling thread started (interval={}s)", interval)
    while True:
        try:
            if _orchestrator is not None:
                _orchestrator._poll_once()  # noqa: SLF001
        except Exception as exc:
            logger.error("Background polling error: {}", exc)
        time.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _chat_agent, _poll_thread

    validate_hipaa_posture()

    _orchestrator = OrchestratorAgent()
    _chat_agent = ChatAgent()

    if settings.ENABLE_POLLING:
        _poll_thread = threading.Thread(
            target=_polling_worker,
            kwargs={"interval": 60},
            daemon=True,
            name="orchestrator-poll",
        )
        _poll_thread.start()
        logger.info("Background polling enabled (ENABLE_POLLING=true)")
    else:
        logger.info(
            "Background polling disabled (ENABLE_POLLING=false). "
            "Use POST /agent/run or set ENABLE_POLLING=true in .env."
        )

    yield
    # Daemon thread stops automatically when the process exits.


app = FastAPI(
    title="Case Manager Agent",
    description=(
        "Local-LLM-powered medical case management assistant. "
        "All PHI stays on-premise."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class EmailRequest(BaseModel):
    """An email dict as returned by GmailTool.fetch_unread_emails."""
    id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    date: str = ""
    body_text: str = ""
    body_html: str = ""
    has_attachments: bool = False
    attachment_filenames: list[str] = []


class ChatRequest(BaseModel):
    text: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    status: str
    reply: str = ""


class RejectRequest(BaseModel):
    reason: str = "No reason provided"

class RouteRequest(BaseModel):
    office: str  # "denver" or "greeley"

class IdsRequest(BaseModel):
    ids: list[str]

# ---------------------------------------------------------------------------
# GET / — HTML dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Case Manager Agent</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f0f2f5;
      color: #1a1a2e;
      font-size: 14px;
      line-height: 1.5;
    }

    /* ---- Draft-mode banner ---- */
    .draft-banner {
      background: #fff8e1;
      border-bottom: 3px solid #f9a825;
      padding: 11px 24px;
      text-align: center;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: .2px;
      color: #5d4037;
    }

    /* ---- Layout ---- */
    .container { max-width: 1280px; margin: 0 auto; padding: 24px 28px; }
    h1 { font-size: 22px; font-weight: 700; color: #1e293b; margin-bottom: 22px; }

    /* ---- Status cards ---- */
    .status-row { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
    .stat-card {
      flex: 1; min-width: 160px;
      background: #fff;
      border-radius: 10px;
      padding: 18px 22px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }
    .stat-card .label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .6px;
      color: #64748b;
    }
    .stat-card .value {
      font-size: 32px;
      font-weight: 800;
      color: #0f172a;
      margin-top: 4px;
      line-height: 1.1;
    }

    /* ---- Section cards ---- */
    .section {
      background: #fff;
      border-radius: 10px;
      padding: 22px 26px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      margin-bottom: 24px;
    }
    .section-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 18px;
    }
    .section-header h2 { font-size: 15px; font-weight: 700; color: #1e293b; }
    .badge {
      background: #e0e7ff;
      color: #3730a3;
      border-radius: 20px;
      padding: 2px 10px;
      font-size: 12px;
      font-weight: 700;
    }

    /* ---- Tables ---- */
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    thead th {
      background: #f8fafc;
      text-align: left;
      padding: 9px 12px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .5px;
      color: #64748b;
      border-bottom: 2px solid #e2e8f0;
      white-space: nowrap;
    }
    tbody td {
      padding: 10px 12px;
      border-bottom: 1px solid #f1f5f9;
      vertical-align: top;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: #f8faff; }
    .empty-row td {
      text-align: center;
      color: #94a3b8;
      padding: 32px;
      font-style: italic;
    }

    /* ---- Type badges ---- */
    .tag {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .tag-referral  { background: #dcfce7; color: #166534; }
    .tag-billing   { background: #fef3c7; color: #92400e; }
    .tag-internal  { background: #e0e7ff; color: #3730a3; }
    .tag-unknown   { background: #fee2e2; color: #991b1b; }
    .tag-routing { background: #fce7f3; color: #9d174d; }
    .btn-denver  { background: #3b82f6; color: #fff; }
    .btn-greeley { background: #8b5cf6; color: #fff; }

    /* ---- Buttons ---- */
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 5px 12px;
      border-radius: 5px;
      border: 1px solid transparent;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      text-decoration: none;
      transition: filter .15s, opacity .15s;
      white-space: nowrap;
    }
    .btn:hover { filter: brightness(.92); }
    .btn:disabled { opacity: .5; cursor: default; }
    .btn-primary  { background: #3b82f6; color: #fff; }
    .btn-success  { background: #22c55e; color: #fff; }
    .btn-danger   { background: #ef4444; color: #fff; }
    .btn-gray     { background: #6b7280; color: #fff; }
    .btn-outline  { background: #fff; color: #3b82f6; border-color: #93c5fd; }
    .actions      { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }

    /* ---- Footer bar ---- */
    .footer-bar {
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 8px 0 4px;
    }
    .last-updated { margin-left: auto; font-size: 12px; color: #94a3b8; }

    /* ---- Toast notification ---- */
    #toast {
      position: fixed;
      bottom: 28px;
      right: 28px;
      padding: 12px 20px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      color: #fff;
      opacity: 0;
      pointer-events: none;
      transition: opacity .3s;
      z-index: 9999;
      max-width: 360px;
    }
    #toast.show { opacity: 1; }
    #toast.ok  { background: #1e293b; }
    #toast.err { background: #dc2626; }

    /* ---- Column widths ---- */
    .col-date    { width: 130px; white-space: nowrap; color: #475569; font-size: 12px; }
    .col-type    { width: 90px; }
    .col-to      { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .col-subject { max-width: 220px; }
    .col-preview { color: #64748b; font-size: 12px; }
    .col-target  { width: 120px; font-weight: 600; }
    .col-created { width: 130px; white-space: nowrap; color: #475569; font-size: 12px; }
    .col-cb      { width: 36px; text-align: center; padding: 10px 6px; }

    /* ---- Duplicate rows ---- */
    tr.dup-row td { background: #fffbeb; }
    tr.dup-row:hover td { background: #fef3c7; }

    /* ---- Queue toolbar ---- */
    .queue-toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      padding: 10px 0 14px;
      border-bottom: 1px solid #e2e8f0;
      margin-bottom: 14px;
    }
    .btn-amber  { background: #f59e0b; color: #fff; }
  </style>
</head>
<body>

<div class="draft-banner">
  &#9888;&#65039; DRAFT MODE &mdash; No emails or messages will be sent without your approval.
</div>

<div class="container">
  <h1>Case Manager Agent Dashboard</h1>

  <!-- Status summary -->
  <div class="status-row">
    <div class="stat-card">
      <div class="label">Processed Today</div>
      <div class="value" id="stat-processed">&mdash;</div>
    </div>
    <div class="stat-card">
      <div class="label">Drafts Pending</div>
      <div class="value" id="stat-drafts">&mdash;</div>
    </div>
    <div class="stat-card">
      <div class="label">Messages Sent Today</div>
      <div class="value" id="stat-sent">&mdash;</div>
    </div>
  </div>

  <!-- Gmail drafts -->
  <div class="section">
    <div class="section-header">
      <h2>Gmail Drafts</h2>
      <span class="badge" id="drafts-badge">&hellip;</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Type</th>
          <th>To</th>
          <th>Subject</th>
          <th>Preview</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="drafts-body">
        <tr><td colspan="6" class="empty-row" style="font-style:italic;color:#94a3b8">Loading&hellip;</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Google Chat queue -->
  <div class="section">
    <div class="section-header">
      <h2>Google Chat Queue</h2>
      <span class="badge" id="staged-badge">&hellip;</span>
    </div>
    <!-- Bulk-action toolbar -->
    <div class="queue-toolbar">
      <button class="btn btn-amber" id="btn-clear-dups" onclick="clearDuplicates()">
        &#9888; Clear Duplicates (<span id="dup-count">0</span>)
      </button>
      <button class="btn btn-danger" id="btn-clear-sel" onclick="clearSelected()" disabled>
        &#128465; Clear Selected
      </button>
      <button class="btn btn-gray" id="btn-reject-sel" onclick="rejectSelected()" disabled>
        &#10007; Reject Selected
      </button>
      <button class="btn btn-danger" id="btn-clear-all" onclick="clearAll()">
        &#128465; Clear All
      </button>
    </div>
    <table>
      <thead>
        <tr>
          <th class="col-cb"><input type="checkbox" id="sel-all" onchange="toggleSelectAll(this)" title="Select all"></th>
          <th>Target</th>
          <th>Message</th>
          <th>Created At</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="staged-body">
        <tr><td colspan="5" class="empty-row" style="font-style:italic;color:#94a3b8">Loading&hellip;</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Footer controls -->
  <div class="footer-bar">
    <button class="btn btn-primary" id="run-btn" onclick="runAgent(event)">&#9654; Run Agent Pass</button>
    <button class="btn btn-outline" onclick="loadData()">&#8635; Refresh</button>
    <span class="last-updated" id="last-updated"></span>
  </div>
</div>

<div id="toast"></div>

<script>
  const GMAIL_DRAFTS_URL = 'https://mail.google.com/mail/u/0/#drafts';
  const todayPrefix = new Date().toISOString().slice(0, 10);

  // ---- Utilities ----

  function esc(s) {
    return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function fmtDate(epochMs) {
    if (!epochMs) return '&mdash;';
    const d = new Date(Number(epochMs));
    return d.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'2-digit'})
      + '&nbsp;' + d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:true});
  }

  function fmtIso(isoStr) {
    if (!isoStr) return '&mdash;';
    return fmtDate(new Date(isoStr).getTime());
  }

  function showToast(msg, isErr = false) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'show ' + (isErr ? 'err' : 'ok');
    clearTimeout(t._tid);
    t._tid = setTimeout(() => t.className = '', 3500);
  }

  function inferType(subject) {
    const s = (subject || '').toLowerCase();
    if (s.includes('unclassified') || s.includes('\u26a0')) return 'unknown';
    if (/(referral|physical therapy|chiropractic|pt eval|ortho|specialist)/.test(s)) return 'referral';
    if (/(claim|billing|payment|insurance|eob|authorization|remittance|denial|invoice)/.test(s)) return 'billing';
    return 'internal';
  }

  function typeTag(type) {
    return `<span class="tag tag-${type}">${type}</span>`;
  }

  function targetLabel(msgType, status) {
    if (status === 'needs_routing') {
      return '<span class="tag tag-routing">Needs Routing</span>';
    }
    return msgType === 'billing_team_notification' ? 'Billing' : 'Receptionist';
  }

  // ---- Data loading ----

  async function loadData() {
    try {
      const [dr, st] = await Promise.all([
        fetch('/drafts').then(r => { if (!r.ok) throw new Error('drafts ' + r.status); return r.json(); }),
        fetch('/staged').then(r => { if (!r.ok) throw new Error('staged ' + r.status); return r.json(); }),
      ]);
      const pending = st.filter(m => !m.sent && m.status !== 'rejected' && m.status !== 'sent');
      renderDrafts(dr);
      renderStaged(pending);
      updateStats(dr, st);
      document.getElementById('last-updated').textContent =
        'Updated ' + new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    } catch (e) {
      showToast('Failed to load data: ' + e.message, true);
    }
  }

  function updateStats(drafts, allStaged) {
    const processedToday = allStaged.filter(m => (m.staged_at || '').startsWith(todayPrefix)).length;
    const sentToday = allStaged.filter(
      m => m.status === 'sent' && (m.sent_at || '').startsWith(todayPrefix)
    ).length;
    document.getElementById('stat-processed').textContent = processedToday;
    document.getElementById('stat-drafts').textContent = drafts.length;
    document.getElementById('stat-sent').textContent = sentToday;
  }

  // ---- Render drafts ----

  function renderDrafts(drafts) {
    document.getElementById('drafts-badge').textContent = drafts.length;
    const tbody = document.getElementById('drafts-body');
    if (!drafts.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No pending drafts</td></tr>';
      return;
    }
    tbody.innerHTML = drafts.map(d => {
      const type = inferType(d.subject);
      return `<tr>
        <td class="col-date">${fmtDate(d.internal_date)}</td>
        <td class="col-type">${typeTag(type)}</td>
        <td class="col-to" title="${esc(d.to)}">${esc(d.to) || '&mdash;'}</td>
        <td class="col-subject" title="${esc(d.subject)}">${esc(d.subject)}</td>
        <td class="col-preview">${esc((d.snippet || '').slice(0, 100))}</td>
        <td>
          <div class="actions">
            <a class="btn btn-outline" href="${GMAIL_DRAFTS_URL}" target="_blank" rel="noopener">Open in Gmail</a>
            <button class="btn btn-danger" onclick="discardDraft('${esc(d.draft_id)}')">Discard</button>
          </div>
        </td>
      </tr>`;
    }).join('');
  }

  // ---- Duplicate detection ----

  function dupKey(m) {
    // Message text is the unique fingerprint — it encodes type + sender + subject.
    return m.message || '';
  }

  function findDuplicateIds(staged) {
    // Returns a Set of IDs that are NOT the newest entry in their duplicate group.
    const groups = {};
    for (const m of staged) {
      const k = dupKey(m);
      if (!groups[k]) groups[k] = [];
      groups[k].push(m);
    }
    const toRemove = new Set();
    for (const group of Object.values(groups)) {
      if (group.length <= 1) continue;
      // Sort newest-first by staged_at; keep the first, flag the rest.
      group.sort((a, b) => (b.staged_at || '').localeCompare(a.staged_at || ''));
      for (let i = 1; i < group.length; i++) toRemove.add(group[i].id);
    }
    return toRemove;
  }

  // ---- Render staged messages ----

  function renderStaged(staged) {
    document.getElementById('staged-badge').textContent = staged.length;
    const dupIds = findDuplicateIds(staged);

    // Update duplicate count label and button state.
    document.getElementById('dup-count').textContent = dupIds.size;
    document.getElementById('btn-clear-dups').disabled = dupIds.size === 0;

    // Reset selection state on every re-render.
    const selAll = document.getElementById('sel-all');
    selAll.checked = false;
    selAll.indeterminate = false;
    document.getElementById('btn-clear-sel').disabled = true;
    document.getElementById('btn-reject-sel').disabled = true;

    const tbody = document.getElementById('staged-body');
    if (!staged.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No pending messages</td></tr>';
      return;
    }
    tbody.innerHTML = staged.map(m => {
      const isDup = dupIds.has(m.id);
      const rowClass = isDup ? ' class="dup-row"' : '';
      const target = targetLabel(m.type, m.status);
      const preview = esc((m.message || '').slice(0, 140));
      const actionBtns = m.status === 'needs_routing'
        ? `<button class="btn btn-denver" onclick="routeMsg('${esc(m.id)}', 'denver')">Denver</button>
           <button class="btn btn-greeley" onclick="routeMsg('${esc(m.id)}', 'greeley')">Greeley</button>
           <button class="btn btn-gray" onclick="rejectMsg('${esc(m.id)}')">Reject</button>`
        : `<button class="btn btn-success" onclick="approveMsg('${esc(m.id)}')">Approve &amp; Send</button>
           <button class="btn btn-gray" onclick="rejectMsg('${esc(m.id)}')">Reject</button>`;
      return `<tr${rowClass}>
        <td class="col-cb"><input type="checkbox" class="row-cb" value="${esc(m.id)}" onchange="onCbChange()"></td>
        <td class="col-target">${target}</td>
        <td>${preview}</td>
        <td class="col-created">${fmtIso(m.staged_at)}</td>
        <td>
          <div class="actions">
            ${actionBtns}
          </div>
        </td>
      </tr>`;
    }).join('');
  }

  // ---- Actions ----

  async function approveMsg(id) {
    if (!confirm('Send this message to Google Chat now?')) return;
    try {
      const r = await fetch('/chat/approve/' + id, {method: 'POST'});
      const j = await r.json();
      if (j.success) { showToast('Message sent to Google Chat.'); loadData(); }
      else showToast('Send failed: ' + (j.error || 'unknown error'), true);
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function rejectMsg(id) {
    const reason = prompt('Rejection reason (required):');
    if (reason === null) return;          // cancelled
    if (!reason.trim()) { showToast('Reason cannot be empty.', true); return; }
    try {
      await fetch('/chat/reject/' + id, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reason}),
      });
      showToast('Message rejected.');
      loadData();
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function routeMsg(id, office) {
    if (!confirm(`Route this message to the ${office.charAt(0).toUpperCase() + office.slice(1)} office?`)) return;
    try {
      const r = await fetch('/chat/route/' + id, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({office}),
      });
      const j = await r.json();
      if (j.success) { showToast('Message routed to ' + j.routed_to + '.'); loadData(); }
      else showToast('Routing failed: ' + (j.error || 'unknown error'), true);
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function discardDraft(draftId) {
    if (!confirm('Permanently discard this draft? This cannot be undone.')) return;
    try {
      const r = await fetch('/drafts/' + draftId + '/discard', {method: 'POST'});
      const j = await r.json();
      if (j.success) { showToast('Draft discarded.'); loadData(); }
      else showToast('Failed to discard draft.', true);
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function runAgent(event) {
    const btn = document.getElementById('run-btn');
    btn.disabled = true;
    btn.textContent = '\u23f3 Starting\u2026';
    try {
      const r = await fetch('/agent/run', {method: 'POST'});
      const j = await r.json();
      if (r.status === 409) {
        showToast('Agent pass already running \u2014 check back shortly.', true);
      } else {
        showToast('Agent pass started in background. Results will appear as emails are processed.');
        loadData();
      }
    } catch (e) {
      showToast('Error: ' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = '&#9654; Run Agent Pass';
    }
  }

  // ---- Checkbox selection helpers ----

  function getSelectedIds() {
    return Array.from(document.querySelectorAll('.row-cb:checked')).map(cb => cb.value);
  }

  function onCbChange() {
    const all = document.querySelectorAll('.row-cb');
    const checked = document.querySelectorAll('.row-cb:checked');
    const selAll = document.getElementById('sel-all');
    selAll.indeterminate = checked.length > 0 && checked.length < all.length;
    selAll.checked = all.length > 0 && checked.length === all.length;
    const hasSelection = checked.length > 0;
    document.getElementById('btn-clear-sel').disabled = !hasSelection;
    document.getElementById('btn-reject-sel').disabled = !hasSelection;
  }

  function toggleSelectAll(cb) {
    document.querySelectorAll('.row-cb').forEach(el => { el.checked = cb.checked; });
    onCbChange();
  }

  // ---- Bulk action handlers ----

  async function clearSelected() {
    const ids = getSelectedIds();
    if (!ids.length) { showToast('Nothing selected.', true); return; }
    try {
      const r = await fetch('/chat-queue/selected', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids}),
      });
      const j = await r.json();
      showToast(`Removed ${j.removed} entr${j.removed === 1 ? 'y' : 'ies'}.`);
      loadData();
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function rejectSelected() {
    const ids = getSelectedIds();
    if (!ids.length) { showToast('Nothing selected.', true); return; }
    try {
      const r = await fetch('/chat-queue/reject-selected', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids}),
      });
      const j = await r.json();
      showToast(`Rejected ${j.updated} entr${j.updated === 1 ? 'y' : 'ies'}.`);
      loadData();
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function clearDuplicates() {
    try {
      const r = await fetch('/chat-queue/duplicates', {method: 'DELETE'});
      const j = await r.json();
      if (j.removed === 0) {
        showToast('No duplicates found.');
      } else {
        showToast(`Removed ${j.removed} duplicate entr${j.removed === 1 ? 'y' : 'ies'}.`);
      }
      loadData();
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  async function clearAll() {
    if (!confirm('Remove every entry from the staged chat queue? This cannot be undone.')) return;
    try {
      const r = await fetch('/chat-queue/all', {method: 'DELETE'});
      const j = await r.json();
      showToast(`Queue cleared (${j.cleared} entr${j.cleared === 1 ? 'y' : 'ies'} removed).`);
      loadData();
    } catch (e) { showToast('Error: ' + e.message, true); }
  }

  // Initial load; auto-refresh every 30 s
  loadData();
  setInterval(loadData, 30000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# GET /drafts — JSON list of pending Gmail drafts
# ---------------------------------------------------------------------------

@app.get("/drafts")
def list_drafts() -> list[dict]:
    """Return metadata for all pending Gmail drafts, newest first."""
    try:
        return _orchestrator.gmail.list_drafts(max_results=50)
    except Exception as exc:
        logger.error("list_drafts failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/discard
# ---------------------------------------------------------------------------

@app.post("/drafts/{draft_id}/discard")
def discard_draft(draft_id: str) -> dict:
    """Permanently delete a Gmail draft by its draft ID."""
    try:
        success = _orchestrator.gmail.delete_draft(draft_id)
        return {"success": success}
    except Exception as exc:
        logger.error("discard_draft failed draft_id={}: {}", draft_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# GET /staged — all staged Google Chat messages (all statuses)
# ---------------------------------------------------------------------------

@app.get("/staged")
def get_staged() -> list[dict]:
    """
    Return all staged Google Chat messages regardless of status.
    The dashboard JS uses this to populate the queue table (filtering to
    pending) and the status counters (all statuses).
    """
    return _read_all_staged()


# ---------------------------------------------------------------------------
# DELETE /chat-queue/selected — remove entries by ID
# ---------------------------------------------------------------------------

@app.delete("/chat-queue/selected")
def queue_delete_selected(body: IdsRequest) -> dict:
    """Remove specific staged entries by their ID."""
    entries = _read_all_staged()
    id_set = set(body.ids)
    remaining = [e for e in entries if e.get("id") not in id_set]
    removed = len(entries) - len(remaining)
    _write_staged(remaining)
    logger.info("queue_delete_selected removed={}", removed)
    return {"removed": removed}


# ---------------------------------------------------------------------------
# DELETE /chat-queue/duplicates — keep newest per message-text group
# ---------------------------------------------------------------------------

@app.delete("/chat-queue/duplicates")
def queue_delete_duplicates() -> dict:
    """Remove duplicate staged entries, keeping the newest per message-text group."""
    entries = _read_all_staged()
    # Walk newest-first; first occurrence of each message text wins.
    seen: dict[str, bool] = {}
    kept: list[dict] = []
    for entry in sorted(entries, key=lambda e: e.get("staged_at", ""), reverse=True):
        key = entry.get("message", "")
        if key not in seen:
            seen[key] = True
            kept.append(entry)
    removed = len(entries) - len(kept)
    if removed:
        kept.sort(key=lambda e: e.get("staged_at", ""))
        _write_staged(kept)
    logger.info("queue_delete_duplicates removed={} kept={}", removed, len(kept))
    return {"removed": removed, "kept": len(kept)}


# ---------------------------------------------------------------------------
# DELETE /chat-queue/all — clear the entire queue
# ---------------------------------------------------------------------------

@app.delete("/chat-queue/all")
def queue_delete_all() -> dict:
    """Overwrite the staged queue with an empty list."""
    count = len(_read_all_staged())
    _write_staged([])
    logger.info("queue_delete_all cleared={}", count)
    return {"cleared": count}


# ---------------------------------------------------------------------------
# PATCH /chat-queue/reject-selected — bulk reject by ID
# ---------------------------------------------------------------------------

@app.patch("/chat-queue/reject-selected")
def queue_reject_selected(body: IdsRequest) -> dict:
    """Set status=rejected on the specified staged entries."""
    entries = _read_all_staged()
    id_set = set(body.ids)
    updated = 0
    for entry in entries:
        if entry.get("id") in id_set:
            entry["status"] = "rejected"
            updated += 1
    _write_staged(entries)
    logger.info("queue_reject_selected updated={}", updated)
    return {"updated": updated}


# ---------------------------------------------------------------------------
# POST /chat/approve/{message_id}
# ---------------------------------------------------------------------------

@app.post("/chat/approve/{message_id}")
def approve_message(message_id: str) -> dict:
    """Mark a staged message approved and send it to the configured webhook."""
    try:
        _chat_agent.approve_and_send(message_id)
        return {"success": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Webhook send failed
        logger.error("approve_message failed id={}: {}", message_id, exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("approve_message unexpected error id={}", message_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /chat/route/{message_id} — assign office routing for needs_routing messages
# ---------------------------------------------------------------------------

@app.post("/chat/route/{message_id}")
def route_message(message_id: str, body: RouteRequest) -> dict:
    """Assign a Denver or Greeley space to a needs_routing staged message and send it."""
    office = body.office.lower().strip()
    if office not in ("denver", "greeley"):
        raise HTTPException(status_code=400, detail="office must be 'denver' or 'greeley'")

    space_map = {
        "denver": settings.GOOGLE_CHAT_SPACE_DENVER,
        "greeley": settings.GOOGLE_CHAT_SPACE_GREELEY,
    }
    space_id = space_map[office]

    try:
        _chat_agent.approve_and_send(message_id, space_override=space_id)
        return {"success": True, "routed_to": office}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# POST /agent/run — kick off a background polling pass
# ---------------------------------------------------------------------------

def _run_poll_background() -> None:
    """Background task: run one orchestrator poll pass, then release the lock."""
    _job_state["running"] = True
    try:
        _orchestrator._poll_once()  # noqa: SLF001
        _job_state["last_status"] = {
            "outcome": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Background agent pass completed")
    except Exception as exc:
        _job_state["last_status"] = {
            "outcome": "error",
            "error": str(exc),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.error("Background agent pass failed: {}", exc)
    finally:
        _job_state["running"] = False
        _poll_lock.release()


@app.post("/agent/run")
def run_agent_pass(background_tasks: BackgroundTasks) -> dict:
    """
    Trigger a single agent polling pass in the background.
    Returns ``{"status": "started"}`` immediately; the pass runs asynchronously.
    Returns 409 if a pass is already running.
    """
    if not _poll_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Agent pass already running")
    logger.info("Manual agent pass triggered via POST /agent/run (background)")
    background_tasks.add_task(_run_poll_background)
    return {"status": "started"}


# ---------------------------------------------------------------------------
# GET /agent/status — current background pass state
# ---------------------------------------------------------------------------

@app.get("/agent/status")
def agent_status() -> dict:
    """Return whether a background agent pass is running and the last completion status."""
    return {
        "running": _job_state["running"],
        "last_status": _job_state["last_status"],
    }


# ---------------------------------------------------------------------------
# POST /email — process a single pre-fetched email dict
# ---------------------------------------------------------------------------

@app.post("/email", response_model=ProcessingResult)
def process_email(request: EmailRequest) -> ProcessingResult:
    """Classify and route a single email dict through the orchestrator."""
    logger.info("POST /email id={}", request.id)  # HIPAA: no PHI logged
    try:
        return _orchestrator.process_email(request.model_dump())
    except Exception as exc:
        logger.exception("Email processing failed id={}", request.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /chat — conversational interface
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Send a plain text message and receive a conversational reply."""
    logger.info("POST /chat text_len={}", len(request.text))
    try:
        result = _chat_agent.run({"text": request.text, "history": request.history})
    except Exception as exc:
        logger.exception("Chat failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ChatResponse(status=result.get("status", "ok"), reply=result.get("reply", ""))


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness check with Ollama reachability probe."""
    ollama_reachable = False
    try:
        resp = http_requests.get(
            f"http://{settings.OLLAMA_HOST}/api/version",
            timeout=2,
        )
        ollama_reachable = resp.status_code == 200
    except Exception:
        pass

    return {
        "status": "ok",
        "model": settings.OLLAMA_MODEL,
        "ollama_host": settings.OLLAMA_HOST,
        "ollama_reachable": ollama_reachable,
        "draft_mode": settings.DRAFT_MODE,
        "polling_enabled": settings.ENABLE_POLLING,
        "polling_active": _poll_thread is not None and _poll_thread.is_alive(),
    }


# ---------------------------------------------------------------------------
# Dev server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Case Manager Agent — local-LLM medical case management"
    )
    parser.add_argument(
        "--ingest-history",
        action="store_true",
        help=(
            "Run HistoryIngester.ingest_all() once (last 90 days), "
            "print an audit report, then exit. "
            "Run this before starting the server for the first time."
        ),
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=500,
        metavar="N",
        help="Maximum emails to ingest (only used with --ingest-history, default: 500)",
    )
    args = parser.parse_args()

    if args.ingest_history:
        # One-shot ingestion path — no FastAPI server started.
        from training.ingest_history import HistoryIngester

        validate_hipaa_posture()
        ingester = HistoryIngester()
        count = ingester.ingest_all(max_emails=args.max_emails)
        ingester.run_audit_report()
        logger.info("History ingestion complete: {} email(s) processed", count)
    else:
        logger.info(
            "Starting case-manager-agent (draft_mode={}, polling={})",
            settings.DRAFT_MODE,
            settings.ENABLE_POLLING,
        )
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
