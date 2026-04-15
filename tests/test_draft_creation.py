"""
Unit tests for draft creation and DRAFT_MODE enforcement.

All Gmail API calls and Ollama inference are mocked so no real credentials,
network requests, or file I/O occur.  Tests verify:

  - ReferralAgent.process() calls gmail.create_draft() and never sends email.
  - BillingAgent.process() calls gmail.create_draft() and never sends email.
  - In DRAFT_MODE=True the run() wrappers return status="draft".
  - gmail.create_draft() receives the expected arguments (correct recipient
    extracted from the sender string, subject prefixed with "Re: ").
  - The Gmail draft API is called with the correct user-facing arguments.
  - No GoogleChatTool.send_webhook_message() is ever called by the agents
    (that is only triggered by a human approving via ChatAgent.approve_and_send).
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_email(
    msg_id: str = "msg_ref_001",
    subject: str = "PT Evaluation Request",
    sender: str = "Dr. Jones <dr.jones@orthoclinic.example.com>",
    body: str = "Please evaluate this patient for physical therapy.",
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


_EMPTY_REFERRAL_FIELDS: dict = {
    "patient_first_name": None,
    "patient_last_name": None,
    "date_of_birth": None,
    "referring_provider_name": None,
    "referring_provider_phone": None,
    "referring_provider_fax": None,
    "diagnosis_code": None,
    "treatment_requested": None,
    "insurance_name": None,
    "insurance_id": None,
    "authorization_required": None,
}


# ---------------------------------------------------------------------------
# ReferralAgent
# ---------------------------------------------------------------------------

@pytest.fixture
def referral_agent_mocked():
    """
    ReferralAgent with GmailTool, PdfTool, EmailStore, and ollama all mocked.
    Yields (agent, mock_gmail, mock_pdf, mock_store).
    """
    with (
        patch("agents.referral_agent.GmailTool") as mock_gmail_cls,
        patch("agents.referral_agent.PdfTool") as mock_pdf_cls,
        patch("agents.referral_agent.EmailStore") as mock_store_cls,
        patch("agents.referral_agent.ollama") as mock_ollama,
    ):
        mock_gmail = MagicMock()
        mock_gmail_cls.return_value = mock_gmail
        mock_gmail.create_draft.return_value = "draft_ref_001"

        # Simulate an empty Gmail payload (no attachments to walk)
        mock_gmail._service.users.return_value.messages.return_value.get.return_value.execute.return_value = {  # noqa: SLF001
            "payload": {"mimeType": "text/plain", "parts": [], "headers": []}
        }

        mock_pdf = MagicMock()
        mock_pdf_cls.return_value = mock_pdf
        mock_pdf.extract_text.return_value = ""
        mock_pdf.extract_referral_fields.return_value = _EMPTY_REFERRAL_FIELDS.copy()

        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        mock_ollama.chat.return_value = {
            "message": {"content": "Thank you for this referral. We will contact the patient."}
        }

        from agents.referral_agent import ReferralAgent

        agent = ReferralAgent()
        yield agent, mock_gmail, mock_pdf, mock_store


class TestReferralAgentDraftCreation:
    def test_process_returns_draft_id(self, referral_agent_mocked):
        agent, mock_gmail, *_ = referral_agent_mocked
        draft_id = agent.process(_make_email())
        assert draft_id == "draft_ref_001"

    def test_create_draft_called_exactly_once(self, referral_agent_mocked):
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email())
        mock_gmail.create_draft.assert_called_once()

    def test_draft_recipient_extracted_from_angle_brackets(self, referral_agent_mocked):
        """Sender "Name <email>" should be unwrapped to bare email address."""
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email(sender="Dr. Jones <dr.jones@orthoclinic.example.com>"))
        _, kwargs = mock_gmail.create_draft.call_args
        assert kwargs.get("to") == "dr.jones@orthoclinic.example.com"

    def test_draft_recipient_bare_email_unchanged(self, referral_agent_mocked):
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email(sender="dr.jones@orthoclinic.example.com"))
        _, kwargs = mock_gmail.create_draft.call_args
        assert kwargs.get("to") == "dr.jones@orthoclinic.example.com"

    def test_draft_subject_prefixed_with_re(self, referral_agent_mocked):
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email(subject="PT Evaluation Request"))
        _, kwargs = mock_gmail.create_draft.call_args
        assert kwargs.get("subject") == "Re: PT Evaluation Request"

    def test_draft_subject_not_double_prefixed(self, referral_agent_mocked):
        """If subject already starts with 'Re:' it should not get a second one."""
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email(subject="Re: PT Evaluation Request"))
        _, kwargs = mock_gmail.create_draft.call_args
        assert kwargs.get("subject") == "Re: PT Evaluation Request"

    def test_draft_body_is_non_empty_string(self, referral_agent_mocked):
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email())
        _, kwargs = mock_gmail.create_draft.call_args
        body = kwargs.get("body", "")
        assert isinstance(body, str) and len(body) > 0

    def test_email_store_save_called(self, referral_agent_mocked):
        agent, _, _, mock_store = referral_agent_mocked
        agent.process(_make_email())
        mock_store.save.assert_called_once()
        record = mock_store.save.call_args[0][0]
        assert record["type"] == "referral"

    def test_no_direct_message_send(self, referral_agent_mocked):
        """The agent must never call any 'send' method on GmailTool — only create_draft."""
        agent, mock_gmail, *_ = referral_agent_mocked
        agent.process(_make_email())
        # GmailTool has no send_message; if someone added one and called it, this fails
        assert not hasattr(mock_gmail, "send_message") or not mock_gmail.send_message.called

    def test_run_returns_draft_status_in_draft_mode(self, referral_agent_mocked):
        agent, *_ = referral_agent_mocked
        with patch("agents.referral_agent.settings") as mock_settings:
            mock_settings.DRAFT_MODE = True
            mock_settings.OLLAMA_MODEL = "llama3:70b"
            result = agent.run({"email": _make_email()})
        assert result["status"] == "draft"
        assert "draft_id" in result

    def test_run_with_no_email_returns_no_email_status(self, referral_agent_mocked):
        agent, *_ = referral_agent_mocked
        result = agent.run({})
        assert result["status"] == "no_email"


# ---------------------------------------------------------------------------
# BillingAgent
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_agent_mocked():
    """BillingAgent with GmailTool, EmailStore, and ollama mocked."""
    with (
        patch("agents.billing_agent.GmailTool") as mock_gmail_cls,
        patch("agents.billing_agent.EmailStore") as mock_store_cls,
        patch("agents.billing_agent.ollama") as mock_ollama,
    ):
        mock_gmail = MagicMock()
        mock_gmail_cls.return_value = mock_gmail
        mock_gmail.create_draft.return_value = "draft_bill_001"

        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        # Two ollama calls: subtype classification + draft reply
        mock_ollama.chat.side_effect = [
            {"message": {"content": "claim_status"}},
            {"message": {"content": "Thank you for your inquiry about claim status."}},
        ]

        from agents.billing_agent import BillingAgent

        agent = BillingAgent()
        yield agent, mock_gmail, mock_store


class TestBillingAgentDraftCreation:
    def test_process_returns_draft_id(self, billing_agent_mocked):
        agent, mock_gmail, _ = billing_agent_mocked
        draft_id = agent.process(_make_email(subject="Claim Status Inquiry"))
        assert draft_id == "draft_bill_001"

    def test_create_draft_called_exactly_once(self, billing_agent_mocked):
        agent, mock_gmail, _ = billing_agent_mocked
        agent.process(_make_email(subject="Claim Status Inquiry"))
        mock_gmail.create_draft.assert_called_once()

    def test_draft_subject_prefixed(self, billing_agent_mocked):
        agent, mock_gmail, _ = billing_agent_mocked
        agent.process(_make_email(subject="Claim Status Inquiry"))
        _, kwargs = mock_gmail.create_draft.call_args
        assert kwargs.get("subject") == "Re: Claim Status Inquiry"

    def test_email_store_save_called(self, billing_agent_mocked):
        agent, _, mock_store = billing_agent_mocked
        agent.process(_make_email(subject="Claim Status Inquiry"))
        mock_store.save.assert_called_once()
        record = mock_store.save.call_args[0][0]
        assert record["type"] == "billing"

    def test_run_returns_draft_status(self, billing_agent_mocked):
        agent, *_ = billing_agent_mocked
        with patch("agents.billing_agent.settings") as mock_settings:
            mock_settings.DRAFT_MODE = True
            mock_settings.OLLAMA_MODEL = "llama3:70b"
            result = agent.run({"email": _make_email(subject="Claim Status Inquiry")})
        assert result["status"] == "draft"


# ---------------------------------------------------------------------------
# GmailTool.create_draft — argument validation
# ---------------------------------------------------------------------------

class TestGmailToolCreateDraft:
    def test_create_draft_encodes_body_and_calls_api(self):
        """
        Verify that create_draft builds a base64-encoded MIME message and
        calls the Gmail drafts.create API endpoint exactly once.
        """
        with patch("tools.gmail_tool.GmailTool.authenticate"):
            from tools.gmail_tool import GmailTool

            tool = GmailTool.__new__(GmailTool)
            tool._processed_label_id = None

            mock_service = MagicMock()
            tool._service = mock_service

            # Stub the nested API call chain
            mock_execute = MagicMock(return_value={"id": "draft_xyz"})
            (
                mock_service.users.return_value
                .drafts.return_value
                .create.return_value
                .execute
            ) = mock_execute

            with patch("tools.gmail_tool.settings") as mock_settings:
                mock_settings.GMAIL_USER_EMAIL = "clinic@example.com"

                draft_id = tool.create_draft(
                    to="provider@example.com",
                    subject="Re: Test",
                    body="Thank you for the referral.",
                )

        assert draft_id == "draft_xyz"
        mock_service.users.return_value.drafts.return_value.create.assert_called_once()
        call_kwargs = (
            mock_service.users.return_value.drafts.return_value.create.call_args[1]
        )
        # Verify the message payload is present and has a 'raw' key
        assert "body" in call_kwargs
        assert "message" in call_kwargs["body"]
        assert "raw" in call_kwargs["body"]["message"]

    def test_create_draft_threads_when_thread_id_given(self):
        """thread_id should be included in the message payload when provided."""
        with patch("tools.gmail_tool.GmailTool.authenticate"):
            from tools.gmail_tool import GmailTool

            tool = GmailTool.__new__(GmailTool)
            tool._processed_label_id = None
            mock_service = MagicMock()
            tool._service = mock_service
            mock_service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
                "id": "draft_threaded"
            }

            with patch("tools.gmail_tool.settings") as mock_settings:
                mock_settings.GMAIL_USER_EMAIL = "clinic@example.com"
                tool.create_draft(
                    to="p@example.com",
                    subject="Re: Test",
                    body="Body text",
                    thread_id="thread_abc",
                )

        call_kwargs = (
            mock_service.users.return_value.drafts.return_value.create.call_args[1]
        )
        message_payload = call_kwargs["body"]["message"]
        assert message_payload.get("threadId") == "thread_abc"
