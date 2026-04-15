from __future__ import annotations

import os
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    OLLAMA_HOST: str = "localhost:11434"
    OLLAMA_MODEL: str = "qwen3:32b"
    OLLAMA_LIGHT_MODEL: str = "qwen3:4b"
    GOOGLE_CREDENTIALS_PATH: str = ""
    GMAIL_USER_EMAIL: str = ""
    DRAFT_MODE: bool = True
    ENABLE_POLLING: bool = False

    # Google Chat spaces (REST API)
    GOOGLE_CHAT_SPACE_DENVER: str = ""
    GOOGLE_CHAT_SPACE_GREELEY: str = ""
    GOOGLE_CHAT_SPACE_BENEFITS: str = ""
    GOOGLE_CHAT_SPACE_BILLING: str = ""


settings = Settings()


# ---------------------------------------------------------------------------
# HIPAA posture validation
# ---------------------------------------------------------------------------

# Cloud LLM environment variables whose presence suggests PHI may leave the
# machine via an external inference API.
_CLOUD_LLM_KEY_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "COHERE_API_KEY",
    "HUGGINGFACE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GOOGLE_AI_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHER_API_KEY",
    "REPLICATE_API_TOKEN",
)

# RFC-1918 private address prefixes (host only, before any port).
_PRIVATE_IP_RE = re.compile(
    r"^("
    r"localhost"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|0\.0\.0\.0"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r")"
)


def validate_hipaa_posture() -> list[str]:
    """
    Check HIPAA-relevant configuration and print warnings for any issues.

    Checks performed:
      (a) OLLAMA_HOST must resolve to localhost or a private RFC-1918 address.
          An external / cloud endpoint would mean PHI leaves the machine.
      (b) DRAFT_MODE should be True — disabling it enables automatic sending
          of emails and Chat messages without human review.
      (c) No cloud LLM API keys should be present in the environment — their
          presence suggests a misconfiguration that could route LLM calls
          (and therefore PHI) to external services.

    Returns:
        List of warning strings (empty if all checks pass).  Warnings are
        also printed to stdout so they appear in server startup logs.
    """
    warnings: list[str] = []

    # --- (a) Ollama host locality check ---
    host_raw = settings.OLLAMA_HOST
    host = host_raw.split(":")[0].strip().lower()
    if not _PRIVATE_IP_RE.match(host):
        warnings.append(
            f"OLLAMA_HOST={host_raw!r} does not appear to be a local or "
            "private-network address. All LLM inference must remain on-premise "
            "to prevent PHI from leaving the machine."
        )

    # --- (b) Draft mode check ---
    if not settings.DRAFT_MODE:
        warnings.append(
            "DRAFT_MODE=false — emails and Google Chat messages will be sent "
            "automatically without human review. Ensure a PHI review workflow "
            "is in place before disabling draft mode in production."
        )

    # --- (c) Cloud LLM key check ---
    found_keys = [k for k in _CLOUD_LLM_KEY_NAMES if os.environ.get(k)]
    if found_keys:
        warnings.append(
            f"Cloud LLM API key(s) detected in environment: {found_keys}. "
            "This project uses local Ollama exclusively. The presence of these "
            "keys suggests a misconfiguration that could result in PHI being "
            "transmitted to an external inference service."
        )

    # --- Print results ---
    print("\n" + "=" * 62)
    print("HIPAA POSTURE CHECK")
    print("=" * 62)
    if warnings:
        for w in warnings:
            print(f"  \u26a0\ufe0f  {w}")
    else:
        print("  \u2713  All checks passed — inference is local, draft mode on.")
    print("=" * 62 + "\n")

    return warnings
