"""
Unit tests for email classification and agent routing logic.

Ollama and all agent/tool dependencies are mocked so no real API calls or
file I/O occur.  Tests verify that:
  - Each LLM response string routes to the correct specialist agent.
  - Malformed LLM output defaults to "unknown".
  - Punctuation in LLM output is stripped before comparison.
  - BillingAgent subtype classification follows the same robustness rules.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_email(
    msg_id: str = "msg001",
    subject: str = "Test subject",
    body: str = "Test body text",
    sender: str = "provider@example.com",
) -> dict:
    return {
        "id": msg_id,
        "thread_id": "thread001",
        "subject": subject,
        "sender": sender,
        "date": "Mon, 01 Apr 2024 10:00:00 +0000",
        "body_text": body,
        "body_html": "",
        "has_attachments": False,
        "attachment_filenames": [],
    }


def _ollama_response(content: str) -> dict:
    return {"message": {"content": content}}


# ---------------------------------------------------------------------------
# OrchestratorAgent — classification
# ---------------------------------------------------------------------------

@pytest.fixture
def orchestrator():
    """
    Return an OrchestratorAgent with all tool/agent dependencies mocked so
    no real Gmail OAuth, ChromaDB, or Ollama calls are made during tests.
    """
    with (
        patch("agents.orchestrator.GmailTool") as mock_gmail_cls,
        patch("agents.orchestrator.ReferralAgent") as mock_referral_cls,
        patch("agents.orchestrator.BillingAgent") as mock_billing_cls,
        patch("agents.orchestrator.ChatAgent") as mock_chat_cls,
    ):
        mock_gmail = MagicMock()
        mock_gmail.is_processed.return_value = False
        mock_gmail_cls.return_value = mock_gmail
        mock_referral_cls.return_value = MagicMock()
        mock_billing_cls.return_value = MagicMock()
        mock_chat_cls.return_value = MagicMock()

        from agents.orchestrator import OrchestratorAgent

        orch = OrchestratorAgent()
        yield (
            orch,
            mock_referral_cls.return_value,
            mock_billing_cls.return_value,
            mock_chat_cls.return_value,
        )


class TestClassifyEmail:
    @patch("agents.orchestrator._ollama_client")
    def test_returns_referral(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("referral")
        assert orch.classify_email(_make_email()) == "referral"

    @patch("agents.orchestrator._ollama_client")
    def test_returns_billing(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("billing")
        assert orch.classify_email(_make_email()) == "billing"

    @patch("agents.orchestrator._ollama_client")
    def test_returns_internal(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("internal")
        assert orch.classify_email(_make_email()) == "internal"

    @patch("agents.orchestrator._ollama_client")
    def test_returns_unknown(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("unknown")
        assert orch.classify_email(_make_email()) == "unknown"

    @patch("agents.orchestrator._ollama_client")
    def test_garbage_output_defaults_to_unknown(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response(
            "I cannot determine the classification."
        )
        assert orch.classify_email(_make_email()) == "unknown"

    @patch("agents.orchestrator._ollama_client")
    def test_empty_output_defaults_to_unknown(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("")
        assert orch.classify_email(_make_email()) == "unknown"

    @patch("agents.orchestrator._ollama_client")
    def test_trailing_period_stripped(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("referral.")
        assert orch.classify_email(_make_email()) == "referral"

    @patch("agents.orchestrator._ollama_client")
    def test_trailing_newline_stripped(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("billing\n")
        assert orch.classify_email(_make_email()) == "billing"

    @patch("agents.orchestrator._ollama_client")
    def test_uppercase_normalised(self, mock_ollama, orchestrator):
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("REFERRAL")
        assert orch.classify_email(_make_email()) == "referral"

    @patch("agents.orchestrator._ollama_client")
    def test_only_first_word_used(self, mock_ollama, orchestrator):
        """LLM says two words; only the first word should be evaluated."""
        orch, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("billing inquiry")
        assert orch.classify_email(_make_email()) == "billing"


# ---------------------------------------------------------------------------
# OrchestratorAgent — routing
# ---------------------------------------------------------------------------

class TestProcessEmailRouting:
    @patch("agents.orchestrator._ollama_client")
    def test_referral_routes_to_referral_agent(self, mock_ollama, orchestrator):
        orch, mock_referral, mock_billing, mock_chat = orchestrator
        mock_ollama.chat.return_value = _ollama_response("referral")
        mock_referral.run.return_value = {"status": "draft", "draft_id": "d1"}

        result = orch.process_email(_make_email())

        mock_referral.run.assert_called_once()
        mock_billing.run.assert_not_called()
        mock_chat.run.assert_not_called()
        assert result.classification == "referral"

    @patch("agents.orchestrator._ollama_client")
    def test_billing_routes_to_billing_agent(self, mock_ollama, orchestrator):
        orch, mock_referral, mock_billing, mock_chat = orchestrator
        mock_ollama.chat.return_value = _ollama_response("billing")
        mock_billing.run.return_value = {"status": "draft", "draft_id": "d2"}

        result = orch.process_email(_make_email())

        mock_billing.run.assert_called_once()
        mock_referral.run.assert_not_called()
        assert result.classification == "billing"

    @patch("agents.orchestrator._ollama_client")
    def test_internal_routes_to_chat_agent(self, mock_ollama, orchestrator):
        orch, mock_referral, mock_billing, mock_chat = orchestrator
        mock_ollama.chat.return_value = _ollama_response("internal")
        mock_chat.run.return_value = {"status": "ok"}

        result = orch.process_email(_make_email())

        mock_chat.run.assert_called_once()
        mock_referral.run.assert_not_called()
        assert result.classification == "internal"

    @patch("agents.orchestrator._ollama_client")
    def test_unknown_creates_review_draft(self, mock_ollama, orchestrator):
        orch, mock_referral, mock_billing, mock_chat = orchestrator
        mock_ollama.chat.return_value = _ollama_response("unknown")

        # Mock the gmail create_draft call inside _create_review_draft
        orch.gmail.create_draft.return_value = "review_draft_id"
        # Mock _get_thread_id
        orch.gmail._fetch_full_message.return_value = {"thread_id": "t1"}  # noqa: SLF001

        result = orch.process_email(_make_email())

        mock_referral.run.assert_not_called()
        mock_billing.run.assert_not_called()
        assert result.classification == "unknown"

    @patch("agents.orchestrator._ollama_client")
    def test_agent_exception_sets_error_status(self, mock_ollama, orchestrator):
        orch, mock_referral, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("referral")
        mock_referral.run.side_effect = RuntimeError("Ollama down")

        result = orch.process_email(_make_email())

        assert result.agent_status == "error"

    @patch("agents.orchestrator._ollama_client")
    def test_mark_as_processed_always_called(self, mock_ollama, orchestrator):
        """mark_as_processed should be called regardless of classification."""
        orch, mock_referral, *_ = orchestrator
        mock_ollama.chat.return_value = _ollama_response("referral")
        mock_referral.run.return_value = {"status": "draft"}

        orch.process_email(_make_email())

        orch.gmail.mark_as_processed.assert_called_once_with("msg001")


# ---------------------------------------------------------------------------
# BillingAgent — subtype classification
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_agent():
    """BillingAgent with GmailTool and EmailStore mocked out."""
    with (
        patch("agents.billing_agent.GmailTool") as mock_gmail_cls,
        patch("agents.billing_agent.EmailStore") as mock_store_cls,
    ):
        mock_gmail_cls.return_value = MagicMock()
        mock_store_cls.return_value = MagicMock()

        from agents.billing_agent import BillingAgent

        agent = BillingAgent()
        yield agent


class TestBillingSubtypeClassification:
    @patch("agents.billing_agent._ollama_client")
    def test_eligibility_question(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("eligibility_question")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "eligibility_question"

    @patch("agents.billing_agent._ollama_client")
    def test_claim_status(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("claim_status")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "claim_status"

    @patch("agents.billing_agent._ollama_client")
    def test_authorization_request(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("authorization_request")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "authorization_request"

    @patch("agents.billing_agent._ollama_client")
    def test_payment_inquiry(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("payment_inquiry")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "payment_inquiry"

    @patch("agents.billing_agent._ollama_client")
    def test_other(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("other")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "other"

    @patch("agents.billing_agent._ollama_client")
    def test_garbage_defaults_to_other(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("I am not sure.")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "other"

    @patch("agents.billing_agent._ollama_client")
    def test_punctuation_stripped(self, mock_ollama, billing_agent):
        mock_ollama.chat.return_value = _ollama_response("claim_status.")
        result = billing_agent._classify_subtype(_make_email())  # noqa: SLF001
        assert result == "claim_status"
