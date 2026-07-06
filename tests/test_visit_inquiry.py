"""Unit tests for agents.visit_inquiry — pure logic only, no network."""

from agents.visit_inquiry import _reply_all_recipients, build_reply_body


def _email(**overrides) -> dict:
    base = {
        "sender": "Jane CM <jane@thirdpartycm.com>",
        "reply_to": "",
        "to": "casemanager.art@gmail.com",
        "cc": "",
        "subject": "Attendance check - patient",
        "message_id_header": "<abc123@mail.example.com>",
        "thread_id": "t1",
        "body_text": "Did the patient attend?",
    }
    base.update(overrides)
    return base


class TestReplyAllRecipients:
    def test_simple_sender_only(self):
        to, cc = _reply_all_recipients(_email())
        assert to == "jane@thirdpartycm.com"
        assert cc == ""

    def test_reply_to_wins_over_from(self):
        to, cc = _reply_all_recipients(_email(reply_to="replies@thirdpartycm.com"))
        assert to == "replies@thirdpartycm.com"

    def test_cc_includes_other_recipients_excludes_own(self):
        to, cc = _reply_all_recipients(_email(
            to="casemanager.art@gmail.com, attorney@lawfirm.com",
            cc="adjuster@insuranceco.com, cm.assistant.art@gmail.com",
        ))
        assert to == "jane@thirdpartycm.com"
        assert "attorney@lawfirm.com" in cc
        assert "adjuster@insuranceco.com" in cc
        assert "casemanager.art@gmail.com" not in cc
        assert "cm.assistant.art@gmail.com" not in cc

    def test_sender_not_duplicated_in_cc(self):
        to, cc = _reply_all_recipients(_email(
            to="jane@thirdpartycm.com, casemanager.art@gmail.com",
        ))
        assert to == "jane@thirdpartycm.com"
        assert "jane@thirdpartycm.com" not in cc

    def test_no_valid_recipient(self):
        to, cc = _reply_all_recipients(_email(sender="casemanager.art@gmail.com", to=""))
        assert to == ""


class TestBuildReplyBody:
    def test_with_past_and_future(self):
        body = build_reply_body("NATHAN GINN", {
            "past_visits": [
                {"date": "6/12/26", "day_time": "Thu, 9:00am",
                 "visit_type": "AUTO CHIRO (MVA Chiro)", "facility": "ART Greeley",
                 "stage": "Completed", "visit_number": "Visit #1", "provider": "N. Ginn"},
            ],
            "future_visits": [
                {"date": "7/31/26", "day_time": "Fri, 8:10am",
                 "visit_type": "AUTO CHIRO Re-Eval (MVA Chiro)", "facility": "ART Greeley",
                 "stage": "Not Checked In", "visit_number": "Visit #1", "provider": "N. Ginn"},
            ],
        })
        assert "NATHAN GINN" in body
        assert "6/12/26" in body
        assert "Completed" in body
        assert "7/31/26" in body
        assert "No past visits" not in body

    def test_empty_visits(self):
        body = build_reply_body("JOHN DOE", {"past_visits": [], "future_visits": []})
        assert "No past visits on record." in body
        assert "No upcoming appointments are currently scheduled." in body

    def test_no_llm_content(self):
        # The body must be fully deterministic — same input, same output.
        visits = {"past_visits": [], "future_visits": []}
        assert build_reply_body("A B", visits) == build_reply_body("A B", visits)
