"""
PDF tool — extract text from PDF bytes and parse structured referral fields
using the local Ollama model.  All processing is fully on-device; no content
is uploaded to external services.
"""

from __future__ import annotations

import io
import json
import re

import ollama
from loguru import logger

try:
    from PyPDF2 import PdfReader
except ImportError:
    from pypdf import PdfReader  # type: ignore[no-redef]

from config import settings

# Fields the LLM is asked to populate.  Used for building the prompt and for
# ensuring every key is present in partial-parse fallback.
_REFERRAL_FIELDS: tuple[str, ...] = (
    "patient_first_name",
    "patient_last_name",
    "date_of_birth",
    "referring_provider_name",
    "referring_provider_phone",
    "referring_provider_fax",
    "diagnosis_code",
    "treatment_requested",
    "insurance_name",
    "insurance_id",
    "authorization_required",
)

# Keywords whose presence (case-insensitive) strongly suggests a referral fax.
_REFERRAL_KEYWORDS: tuple[str, ...] = (
    "referral",
    "authorization",
    "diagnosis",
    "referring physician",
    "npi",
)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a medical document parser.  Extract structured data from the referral \
fax text provided by the user and return ONLY a single valid JSON object — no \
markdown, no explanation, no extra keys.

The JSON object must contain exactly these keys:
  patient_first_name        (string or null)
  patient_last_name         (string or null)
  date_of_birth             (string in YYYY-MM-DD format, or null)
  referring_provider_name   (string or null)
  referring_provider_phone  (string or null)
  referring_provider_fax    (string or null)
  diagnosis_code            (string, e.g. ICD-10 code, or null)
  treatment_requested       (string or null)
  insurance_name            (string or null)
  insurance_id              (string or null)
  authorization_required    (boolean or null)

Rules:
- Use null (JSON null, not the string "null") for any field you cannot find.
- Do not invent or infer values that are not present in the text.
- authorization_required is true if the document mentions prior auth, \
pre-authorization, or pre-cert; false if it explicitly states no auth is \
needed; null if not mentioned.
- Return only the JSON object, nothing else."""


class PdfTool:
    """
    Extract text from PDF bytes and parse structured referral data using the
    local Ollama LLM.
    """

    # ------------------------------------------------------------------
    # 1. Extract raw text from PDF bytes
    # ------------------------------------------------------------------

    def extract_text(self, pdf_bytes: bytes) -> str:
        """
        Extract all text from *pdf_bytes* and return it as a single string,
        with pages separated by double newlines.

        Pages that yield no text (e.g. scanned image-only pages) are skipped
        with a debug log.  If the entire document has no text layer, an empty
        string is returned — callers should check for this and handle
        image-only PDFs separately (e.g. via OCR).

        Args:
            pdf_bytes: Raw bytes of a PDF file.

        Returns:
            Extracted text string, possibly empty.
        """
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        pages: list[str] = []

        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
            else:
                logger.debug("PDF page {}/{} has no extractable text (may be scanned)", i + 1, page_count)

        full_text = "\n\n".join(pages)
        logger.info(
            "PDF text extraction complete: {} pages, {} extractable, {} chars",
            page_count,
            len(pages),
            len(full_text),
        )
        return full_text

    # ------------------------------------------------------------------
    # 2. Extract referral fields via local LLM
    # ------------------------------------------------------------------

    def extract_referral_fields(self, pdf_text: str) -> dict:
        """
        Use the configured local Ollama model to parse structured referral
        fields from *pdf_text*.

        The model is asked to return a JSON object with a fixed set of keys
        (see ``_REFERRAL_FIELDS``).  Temperature is set to 0.1 for
        deterministic, consistent extraction.

        Parse failures are handled gracefully:
          - If the response is valid JSON, it is returned directly (with any
            missing keys back-filled to ``None``).
          - If the response contains an embedded JSON object, that is
            extracted and returned.
          - If parsing fails entirely, a dict with all fields set to ``None``
            is returned so callers always receive a predictable structure.

        PHI note: extracted field values are returned to the caller but are
        never written to logs.  Only token counts and parse status are logged.

        Args:
            pdf_text: The text content of the referral PDF.

        Returns:
            Dict with all ``_REFERRAL_FIELDS`` keys present.
        """
        logger.info(
            "Extracting referral fields via Ollama model={} text_len={}",
            settings.OLLAMA_MODEL,
            len(pdf_text),
        )

        response = ollama.chat(
            model=settings.OLLAMA_MODEL,
            options={"temperature": 0.1},
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": pdf_text},
            ],
        )

        raw_content: str = response["message"]["content"].strip()
        logger.debug(
            "LLM response received: ~{} chars",
            len(raw_content),
        )

        return self._parse_llm_json(raw_content)

    # ------------------------------------------------------------------
    # 3. Heuristic referral fax detection
    # ------------------------------------------------------------------

    def is_referral_fax(self, pdf_text: str) -> bool:
        """
        Return ``True`` if *pdf_text* contains at least one keyword strongly
        associated with a referral fax document.

        The check is case-insensitive.  It is intentionally simple — use it
        as a fast pre-filter before committing to a full LLM extraction call.

        Args:
            pdf_text: The text content of a PDF document.

        Returns:
            ``True`` if the document looks like a referral fax.
        """
        lowered = pdf_text.lower()
        matched = [kw for kw in _REFERRAL_KEYWORDS if kw in lowered]

        if matched:
            logger.debug("Referral heuristic matched keywords: {}", matched)
            return True

        logger.debug("Referral heuristic found no matching keywords")
        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_llm_json(self, raw: str) -> dict:
        """
        Attempt to parse *raw* as a JSON object, falling back gracefully on
        errors.  Always returns a dict with every key in ``_REFERRAL_FIELDS``.

        Strategy (tried in order):
          1. Direct ``json.loads`` of the full response.
          2. Extract the first ``{...}`` block via regex and parse that.
          3. Return a null-filled skeleton and log a warning.
        """
        empty: dict = {field: None for field in _REFERRAL_FIELDS}

        # Strategy 1: response is already clean JSON
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                logger.debug("LLM JSON parsed successfully (strategy=direct)")
                return self._normalise(data, empty)
        except json.JSONDecodeError:
            pass

        # Strategy 2: JSON embedded inside markdown or prose
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict):
                    logger.debug("LLM JSON parsed successfully (strategy=regex_extract)")
                    return self._normalise(data, empty)
            except json.JSONDecodeError:
                pass

        # Strategy 3: total parse failure — return safe skeleton
        logger.warning(
            "Failed to parse LLM JSON response; returning null-filled skeleton. "
            "response_len={}",  # HIPAA: no PHI logged — raw LLM output suppressed
            len(raw),
        )
        return empty

    @staticmethod
    def _normalise(data: dict, empty: dict) -> dict:
        """
        Merge *data* into *empty* so that every expected key is present.
        Casts ``authorization_required`` to ``bool | None`` if the LLM
        returned a string like ``"true"`` / ``"yes"``.
        """
        result = {**empty, **{k: v for k, v in data.items() if k in empty}}

        auth = result.get("authorization_required")
        if isinstance(auth, str):
            lowered = auth.strip().lower()
            if lowered in ("true", "yes", "1"):
                result["authorization_required"] = True
            elif lowered in ("false", "no", "0"):
                result["authorization_required"] = False
            else:
                result["authorization_required"] = None

        return result
