"""Unit tests for agents.pav_request — pure logic only, no network."""

from agents.pav_request import build_forward_body, is_pav_request, try_handle


def _email(**overrides) -> dict:
    base = {
        "id": "m1",
        "thread_id": "t1",
        "sender": "Billing West <billingwest@marrick.com>",
        "date": "Mon, 6 Jul 2026 09:00:00 -0600",
        "to": "casemanager.art@gmail.com",
        "subject": "MARRICK PAV - C.BAUGHN",
        "body_text": "Attached you will find a Patient Account Verification (“PAV”).",
        "attachment_parts": [
            {"filename": "PAV_Baughn.pdf", "mime_type": "application/pdf",
             "attachment_id": "att1"},
        ],
    }
    base.update(overrides)
    return base


class TestIsPavRequest:
    def test_real_subject_shapes(self):
        # Subjects seen in the actual inbox, all from @marrick.com senders.
        for subject in [
            "MARRICK PAV - C.BAUGHN",
            "Marrick PAV - Geary 4887534",
            "Marrick Patient Account Verification",
            "Fw: Marrick Patient Account Verification for R. Stock follow up",
        ]:
            assert is_pav_request(_email(subject=subject, body_text="")), subject

    def test_pav_in_body_only(self):
        # Cancellation-of-authorization emails often carry the PAV in the body.
        assert is_pav_request(_email(
            subject="Marrick - Cancellation of Authorization",
            body_text="Please find the attached Patient Account Verification.",
        ))

    def test_non_marrick_sender_rejected(self):
        assert not is_pav_request(_email(
            sender="Brittney McCarty <brittneymccarty.abc@gmail.com>"))

    def test_marrick_without_pav_rejected(self):
        assert not is_pav_request(_email(
            subject="Marrick - Additional Authorization",
            body_text="Attached is the additional authorization you requested.",
        ))

    def test_pav_word_boundary(self):
        # "pavement" must not match the standalone PAV token.
        assert not is_pav_request(_email(
            subject="Parking", body_text="The pavement was icy."))


class TestBuildForwardBody:
    def test_contains_note_and_original(self):
        body = build_forward_body(_email())
        assert "Hi Brit," in body
        assert "reply-all in this email string" in body
        assert "---------- Forwarded message ---------" in body
        assert "billingwest@marrick.com" in body
        assert "Patient Account Verification" in body

    def test_deterministic(self):
        assert build_forward_body(_email()) == build_forward_body(_email())


class _FakeGmail:
    def __init__(self, fail_draft=False, fail_attachments=False):
        self.fail_draft = fail_draft
        self.fail_attachments = fail_attachments
        self.last_kwargs = None

    def fetch_message_attachments(self, email):
        if self.fail_attachments:
            raise RuntimeError("boom")
        return [{"filename": "PAV.pdf", "mime_type": "application/pdf",
                 "data": b"%PDF"}]

    def get_signature(self):
        return "<b>sig</b>"

    def create_draft(self, **kwargs):
        if self.fail_draft:
            raise RuntimeError("boom")
        self.last_kwargs = kwargs
        return "draft123"


class TestTryHandle:
    def test_non_pav_returns_none(self):
        assert try_handle(_email(sender="someone@example.com"), _FakeGmail()) is None

    def test_drafted(self):
        gmail = _FakeGmail()
        result = try_handle(_email(), gmail)
        assert result == {"status": "drafted", "draft_id": "draft123",
                          "attachment_count": 1}
        assert gmail.last_kwargs["subject"] == "Fwd: MARRICK PAV - C.BAUGHN"
        assert gmail.last_kwargs["thread_id"] == "t1"
        assert gmail.last_kwargs["attachments"][0]["filename"] == "PAV.pdf"

    def test_fwd_prefix_not_duplicated(self):
        gmail = _FakeGmail()
        try_handle(_email(subject="Fw: MARRICK PAV - J.GEARY"), gmail)
        assert gmail.last_kwargs["subject"] == "Fw: MARRICK PAV - J.GEARY"

    def test_draft_failure(self):
        assert try_handle(_email(), _FakeGmail(fail_draft=True)) == {
            "status": "draft_error"}

    def test_attachment_failure(self):
        assert try_handle(_email(), _FakeGmail(fail_attachments=True)) == {
            "status": "draft_error"}
