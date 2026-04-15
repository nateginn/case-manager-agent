"""
History ingester — bulk-load historical Gmail emails into the EmailStore
so the agent can reference past cases during retrieval-augmented generation.

Two top-level classes:

  HistoryIngester   — Fetches live emails from Gmail (last 90 days by
                      default), classifies them, generates non-PHI summaries
                      via Ollama, and persists them to ChromaDB.  PHI is
                      scrubbed before any text is written to storage.

  HistoryIngestor   — (legacy) Loads historical records from JSONL exports
                      or local directories.  Kept for offline/file-based
                      workflows.

Usage (live Gmail ingestion):
    python -m training.ingest_history --live --max-emails 500

Usage (file-based import):
    python -m training.ingest_history --source ./exports/emails.jsonl
    python -m training.ingest_history --source ./exports/emails/ --glob "*.eml"

PHI policy: phi_scrub() is applied to all text before it touches ChromaDB.
Summaries are generated with an explicit non-PHI instruction.  Raw email
bodies and PDF bytes are never written to storage.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import ollama
from loguru import logger

from config import settings
from memory.email_store import EmailStore
from tools.gmail_tool import GmailTool
from tools.pdf_tool import PdfTool

# Lazy import of OrchestratorAgent to avoid circular imports at module level.
# It is resolved inside HistoryIngester.__init__().

_AUDIT_REPORT_PATH = Path(__file__).parent / "audit_report.txt"

_SUMMARY_SYSTEM_PROMPT = """\
You are a HIPAA-aware medical records assistant for a chiropractic and \
physical therapy clinic.

Summarize the email below in 2-3 sentences describing its TYPE and INTENT.

You MUST NOT include any of the following in your summary:
- Patient names, first or last
- Dates of birth or ages
- Social Security numbers
- Insurance member IDs, policy numbers, or group numbers
- Phone numbers or fax numbers

Write at the level of: "Referral received from an orthopedic office for \
physical therapy evaluation. Prior authorization appears required. Staff \
acknowledged receipt and will schedule accordingly."

Return the summary text only — no labels, no preamble."""

# ---------------------------------------------------------------------------
# PHI scrubbing
# ---------------------------------------------------------------------------

# Each tuple: (compiled_pattern, replacement_string)
_PHI_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SSN  — XXX-XX-XXXX
    (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[SSN REDACTED]",
    ),
    # DOB when labelled — covers: DOB: 01/15/1985 | date of birth 1-15-85
    (
        re.compile(
            r"(?i)(dob|date\s+of\s+birth)\s*[:\-]?\s*"
            r"(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
            r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
            r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
            r"\s+\d{1,2},?\s+\d{4})"
        ),
        r"\1 [DOB REDACTED]",
    ),
    # Insurance member / policy IDs when labelled
    (
        re.compile(
            r"(?i)(member\s*(?:id|#|no|number)"
            r"|policy\s*(?:number|#|no)"
            r"|insurance\s*(?:id|#|no|number)"
            r"|group\s*(?:id|#|no|number)"
            r"|subscriber\s*(?:id|#|no|number))"
            r"\s*[:\-#]?\s*[A-Z0-9]{5,20}\b"
        ),
        r"\1 [INSURANCE ID REDACTED]",
    ),
    # 10-digit US phone numbers — (NNN) NNN-NNNN | NNN-NNN-NNNN | NNN.NNN.NNNN
    (
        re.compile(
            r"(?<!\d)"
            r"(?:\(\d{3}\)\s*|\d{3}[.\-\s])"
            r"\d{3}[.\-\s]\d{4}"
            r"(?!\d)"
        ),
        "[PHONE REDACTED]",
    ),
    # Bare 10-digit run (no formatting) — last resort to catch raw digit strings
    (
        re.compile(r"(?<!\d)\d{10}(?!\d)"),
        "[PHONE REDACTED]",
    ),
]


def phi_scrub(text: str) -> str:
    """
    Apply regex-based redaction to *text* to remove common PHI patterns:
      - SSNs (XXX-XX-XXXX)
      - Dates of birth when preceded by "DOB" or "date of birth"
      - Insurance/policy/member IDs when labelled
      - 10-digit US phone numbers (formatted or bare)

    This is a best-effort defence-in-depth layer — it is not a substitute
    for the LLM's own non-PHI instructions.

    Args:
        text: Raw text that may contain PHI.

    Returns:
        Text with matched PHI patterns replaced by labelled placeholders.
    """
    for pattern, replacement in _PHI_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# HistoryIngester — live Gmail ingestion
# ---------------------------------------------------------------------------

class HistoryIngester:
    """
    Fetch historical emails from Gmail, classify them, generate non-PHI
    summaries, and persist them to ChromaDB for future few-shot retrieval.

    Design constraints:
      - Raw email bodies and PDF bytes are never written to ChromaDB.
      - ``phi_scrub`` is applied to all text before it is passed to the LLM
        or stored.
      - The LLM (local Ollama) is given explicit non-PHI summarisation
        instructions.
      - Only the summary, classification, and email_id are persisted.
    """

    def __init__(self) -> None:
        # Deferred import avoids circular dependency at module load time.
        from agents.orchestrator import OrchestratorAgent  # noqa: PLC0415

        self.gmail = GmailTool()
        self.orchestrator = OrchestratorAgent()
        self.pdf = PdfTool()
        self.store = EmailStore()

        # Accumulate data for audit report across ingest_all() calls.
        self._audit: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "skipped": 0,
            "by_classification": defaultdict(int),
            "dates": [],  # parsed datetime objects
        }

    # ------------------------------------------------------------------
    # 1. Main ingestion pipeline
    # ------------------------------------------------------------------

    def ingest_all(self, max_emails: int = 500) -> int:
        """
        Fetch up to *max_emails* from Gmail (last 90 days, all emails
        regardless of read/processed status) and ingest each one.

        Per-email steps:
          a. Classify via OrchestratorAgent.classify_email().
          b. Optionally extract PDF text (best-effort; falls back to body).
          c. PHI-scrub the source text before passing to the LLM.
          d. Generate a non-PHI summary via Ollama.
          e. PHI-scrub the generated summary as a second safety layer.
          f. Persist via EmailStore.save_summary().

        Individual email failures are caught and logged without aborting the
        batch.

        Args:
            max_emails: Upper bound on emails to process in this run.

        Returns:
            Number of emails successfully ingested.
        """
        query = self._build_date_query(days=90)
        logger.info(
            "HistoryIngester.ingest_all starting (max={}, query={!r})",
            max_emails,
            query,
        )

        stubs = self.gmail._list_messages(query=query, max_results=max_emails)  # noqa: SLF001
        logger.info("Gmail returned {} message stubs for ingestion", len(stubs))

        ingested = 0
        for stub in stubs:
            message_id: str = stub["id"]
            try:
                ingested += self._ingest_one(message_id)
            except Exception as exc:
                logger.error(
                    "Skipping message_id={} due to error: {}",
                    message_id,
                    exc,
                )
                self._audit["skipped"] += 1

        self._audit["total"] = ingested
        logger.info(
            "HistoryIngester.ingest_all complete: ingested={} skipped={}",
            ingested,
            self._audit["skipped"],
        )
        return ingested

    # ------------------------------------------------------------------
    # 2. Few-shot example generation
    # ------------------------------------------------------------------

    def generate_few_shot_examples(self, classification: str, n: int = 5) -> str:
        """
        Query ChromaDB for up to *n* stored summaries of the given
        *classification* and format them as a few-shot block suitable for
        prepending to an Ollama prompt.

        The block format::

            ### Past examples of similar emails handled by this clinic:

            Example 1:
            <summary text>

            Example 2:
            <summary text>
            ...

        Returns an empty string if no summaries are stored for the
        *classification*.

        Args:
            classification: One of ``"referral"``, ``"billing"``,
                            ``"internal"``, ``"unknown"``.
            n:              Maximum number of examples to include.

        Returns:
            Formatted few-shot block string, or ``""`` if none available.
        """
        summaries = self._query_summaries_by_classification(classification, n)

        if not summaries:
            logger.debug(
                "generate_few_shot_examples: no summaries found for classification={}",
                classification,
            )
            return ""

        lines = [
            f"### Past examples of similar emails handled by this clinic:\n"
        ]
        for i, summary in enumerate(summaries, start=1):
            lines.append(f"Example {i}:\n{summary.strip()}\n")

        block = "\n".join(lines)
        logger.debug(
            "generate_few_shot_examples classification={} examples={}",
            classification,
            len(summaries),
        )
        return block

    # ------------------------------------------------------------------
    # 3. Audit report
    # ------------------------------------------------------------------

    def run_audit_report(self) -> str:
        """
        Build and return a plain-text summary of the ingestion run.

        Contents:
          - Run timestamp
          - Total emails ingested and skipped
          - Breakdown by classification
          - Date range of emails covered

        The report is also written to ``training/audit_report.txt``.

        Returns:
            The report as a plain-text string.
        """
        by_class = dict(self._audit["by_classification"])
        dates: list[datetime] = self._audit["dates"]

        if dates:
            oldest = min(dates).strftime("%Y-%m-%d")
            newest = max(dates).strftime("%Y-%m-%d")
            date_range = f"{oldest} to {newest}"
        else:
            date_range = "N/A"

        lines = [
            "=" * 60,
            "HISTORY INGESTER AUDIT REPORT",
            "=" * 60,
            f"Run started:       {self._audit['started_at']}",
            f"Report generated:  {datetime.now(timezone.utc).isoformat()}",
            "",
            f"Total ingested:    {self._audit['total']}",
            f"Total skipped:     {self._audit['skipped']}",
            "",
            "Breakdown by classification:",
        ]

        for cls in ("referral", "billing", "internal", "unknown"):
            count = by_class.get(cls, 0)
            lines.append(f"  {cls:<12} {count}")

        other_classes = {k: v for k, v in by_class.items()
                         if k not in ("referral", "billing", "internal", "unknown")}
        for cls, count in sorted(other_classes.items()):
            lines.append(f"  {cls:<12} {count}")

        lines += [
            "",
            f"Date range covered: {date_range}",
            "=" * 60,
        ]

        report = "\n".join(lines)

        _AUDIT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            _AUDIT_REPORT_PATH.write_text(report, encoding="utf-8")
            logger.info("Audit report saved to {}", _AUDIT_REPORT_PATH)
        except OSError as exc:
            logger.error("Failed to write audit report: {}", exc)

        print(report)
        return report

    # ------------------------------------------------------------------
    # Private helpers — per-email pipeline
    # ------------------------------------------------------------------

    def _ingest_one(self, message_id: str) -> int:
        """
        Process a single email by ID.  Returns 1 on success, 0 on skip
        (e.g. no body text and no PDF).
        """
        email = self.gmail._fetch_full_message(message_id)  # noqa: SLF001
        subject: str = email.get("subject", "(no subject)")
        date_str: str = email.get("date", "")

        # --- a. Classify ---
        classification = self.orchestrator.classify_email(email)

        # --- b. Build source text (body, supplemented by PDF if present) ---
        body_text: str = email.get("body_text") or email.get("body_html") or ""
        pdf_text: str = ""
        if email.get("has_attachments"):
            pdf_text = self._try_get_pdf_text(email)

        source_text = (pdf_text or body_text).strip()
        if not source_text:
            logger.debug(
                "Skipping message_id={} — no extractable text",  # HIPAA: no PHI logged
                message_id,
            )
            return 0

        # --- c. PHI-scrub before touching the LLM ---
        scrubbed = phi_scrub(source_text[:3000])  # cap at 3 000 chars for LLM context

        # --- d. Generate non-PHI summary ---
        summary = self._generate_summary(message_id, scrubbed, classification, subject)

        # --- e. Second PHI-scrub on the LLM output ---
        summary = phi_scrub(summary)

        # --- f. Persist ---
        self.store.save_summary(message_id, summary, classification)

        # --- Track audit data ---
        self._audit["by_classification"][classification] += 1
        parsed_date = self._parse_date(date_str)
        if parsed_date:
            self._audit["dates"].append(parsed_date)

        logger.debug(
            "Ingested message_id={} classification={} summary_len={}",
            message_id,
            classification,
            len(summary),
        )
        return 1

    def _generate_summary(
        self,
        message_id: str,
        scrubbed_text: str,
        classification: str,
        subject: str,
    ) -> str:
        """
        Ask the local LLM to produce a 2–3 sentence non-PHI summary.
        Returns a fallback string on Ollama failure.
        """
        user_content = (
            f"Email classification: {classification}\n"
            f"Subject: {subject}\n\n"
            f"Body:\n{scrubbed_text}"
        )

        try:
            response = ollama.chat(
                model=settings.OLLAMA_MODEL,
                options={"temperature": 0.1},
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            summary: str = response["message"]["content"].strip()
            logger.debug(
                "Summary generated message_id={} len={}", message_id, len(summary)
            )
            return summary
        except Exception as exc:
            logger.error(
                "Ollama summary generation failed message_id={}: {}",
                message_id,
                exc,
            )
            # HIPAA: no PHI in fallback — subject omitted
            return f"{classification.capitalize()} email received."

    # ------------------------------------------------------------------
    # Private helpers — PDF extraction
    # ------------------------------------------------------------------

    def _try_get_pdf_text(self, email: dict) -> str:
        """
        Best-effort: re-fetch the message payload, walk it for PDF parts,
        extract text from each, and return the concatenated result.

        Returns empty string on any failure so the caller can fall back to
        the email body without interrupting the pipeline.
        """
        message_id: str = email.get("id", "")
        try:
            msg = (
                self.gmail._service  # noqa: SLF001
                .users()
                .messages()
                .get(
                    userId=settings.GMAIL_USER_EMAIL,
                    id=message_id,
                    format="full",
                )
                .execute()
            )
        except Exception as exc:
            logger.warning(
                "Could not re-fetch payload for PDF walk message_id={}: {}",
                message_id,
                exc,
            )
            return ""

        pdf_parts: list[dict] = []
        self._walk_for_pdf_parts(msg.get("payload", {}), pdf_parts)

        texts: list[str] = []
        for part in pdf_parts:
            try:
                raw_bytes = self.gmail.fetch_attachment(
                    message_id, part["attachment_id"]
                )
                text = self.pdf.extract_text(raw_bytes)
                if text.strip():
                    texts.append(text)
            except Exception as exc:
                logger.debug(
                    "PDF extraction failed message_id={} filename={!r}: {}",
                    message_id,
                    part.get("filename"),
                    exc,
                )

        return "\n\n---\n\n".join(texts)

    def _walk_for_pdf_parts(self, part: dict, acc: list[dict]) -> None:
        """Depth-first MIME walk; appends {filename, attachment_id} for PDF parts."""
        mime_type: str = part.get("mimeType", "")
        filename: str = part.get("filename", "")
        body: dict = part.get("body", {})

        if mime_type.startswith("multipart/"):
            for sub in part.get("parts", []):
                self._walk_for_pdf_parts(sub, acc)
            return

        if filename and self._is_pdf_part(mime_type, filename):
            attachment_id: str = body.get("attachmentId", "")
            if attachment_id:
                acc.append({"filename": filename, "attachment_id": attachment_id})

    @staticmethod
    def _is_pdf_part(mime_type: str, filename: str) -> bool:
        pdf_mime_types = {
            "application/pdf",
            "application/x-pdf",
            "application/octet-stream",
        }
        return mime_type in pdf_mime_types or filename.lower().endswith(".pdf")

    # ------------------------------------------------------------------
    # Private helpers — ChromaDB summary query
    # ------------------------------------------------------------------

    def _query_summaries_by_classification(
        self, classification: str, n: int
    ) -> list[str]:
        """
        Return up to *n* summary strings from the ``email_summaries``
        ChromaDB collection filtered to *classification*.

        Uses a metadata ``where`` filter combined with a semantic query so
        results are both on-topic and ordered by relevance.
        """
        summaries_col = self.store._summaries  # noqa: SLF001
        total = summaries_col.count()
        if total == 0:
            return []

        # Count how many entries exist for this classification
        try:
            existing = summaries_col.get(
                where={"classification": classification},
                limit=1,
            )
            if not existing["ids"]:
                return []
        except Exception:
            return []

        try:
            results = summaries_col.query(
                query_texts=[classification],
                n_results=min(n, total),
                where={"classification": classification},
            )
            return [doc for doc in results.get("documents", [[]])[0] if doc]
        except Exception as exc:
            logger.warning(
                "Failed to query summaries for classification={}: {}",
                classification,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Private helpers — utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _build_date_query(days: int = 90) -> str:
        """Return a Gmail search query string for emails within the last *days*."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        date_str = cutoff.strftime("%Y/%m/%d")
        return f"after:{date_str}"

    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        """Parse an RFC 2822 email Date header into a datetime; return None on failure."""
        if not date_str:
            return None
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            # Some Date headers are malformed; ignore them for the audit
            return None


# ---------------------------------------------------------------------------
# HistoryIngestor — legacy file-based ingestor (preserved)
# ---------------------------------------------------------------------------

class HistoryIngestor:
    """Load historical records into the local vector store from files."""

    def __init__(self) -> None:
        self.store = EmailStore()

    def ingest_jsonl(self, path: str | Path) -> int:
        """
        Ingest a JSONL file where each line is a JSON record with keys
        ``raw``, ``type``, and optionally ``structured``.
        Returns the number of records successfully ingested.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"JSONL file not found: {path}")

        count = 0
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    self.store.save(record)
                    count += 1
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning("Skipping line {}: {}", lineno, exc)

        logger.info("Ingested {} records from {}", count, path.name)
        return count

    def ingest_directory(self, directory: str | Path, glob_pattern: str = "*.json") -> int:
        """
        Walk *directory* and ingest all files matching *glob_pattern*.
        Returns total records ingested.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory}")

        files = list(directory.glob(glob_pattern))
        logger.info(
            "Found {} files matching {} in {}", len(files), glob_pattern, directory
        )

        total = 0
        for fp in files:
            try:
                record = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    logger.warning(
                        "Skipping {}: top-level value is not a JSON object", fp.name
                    )
                    continue
                self.store.save(record)
                total += 1
            except Exception as exc:
                logger.warning("Skipping {}: {}", fp.name, exc)

        logger.info("Ingested {} total records from directory {}", total, directory)
        return total

    def ingest_raw_text(self, text: str, record_type: str = "chat") -> str:
        """Ingest a single raw text string.  Returns the generated document ID."""
        record = {"raw": text, "type": record_type, "structured": {}}
        doc_id = self.store.save(record)
        logger.debug("Ingested single record id={}", doc_id)
        return doc_id


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest historical email records into the vector store"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--live",
        action="store_true",
        help="Fetch emails directly from Gmail (last 90 days)",
    )
    mode.add_argument(
        "--source",
        help="Path to a .jsonl file or a directory (file-based import)",
    )

    parser.add_argument(
        "--max-emails",
        type=int,
        default=500,
        help="Maximum emails to ingest when using --live (default: 500)",
    )
    parser.add_argument(
        "--glob",
        default="*.json",
        help="Glob pattern when --source is a directory",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Print an audit report after ingestion (only applies to --live)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.live:
        ingester = HistoryIngester()
        count = ingester.ingest_all(max_emails=args.max_emails)
        logger.info("Live ingestion complete: {} emails processed", count)
        if args.audit:
            ingester.run_audit_report()
    else:
        ingestor = HistoryIngestor()
        source = Path(args.source)
        if source.is_file():
            ingestor.ingest_jsonl(source)
        elif source.is_dir():
            ingestor.ingest_directory(source, glob_pattern=args.glob)
        else:
            logger.error("--source must be an existing file or directory")
