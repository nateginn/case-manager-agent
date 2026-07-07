"""Unit tests for agents.visit_inquiry — pure logic only, no network."""

from agents.visit_inquiry import (
    _parse_patient_dobs,
    _parse_patient_names,
    _reply_all_recipients,
    build_reply_body,
)


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


def _visit(date, stage="Completed", day_time="Thu, 9:00am", visit_type="AUTO CHIRO (MVA Chiro)"):
    return {"date": date, "day_time": day_time, "visit_type": visit_type,
            "facility": "ART Greeley", "stage": stage,
            "visit_number": "Visit #1", "provider": "N. Ginn"}


class TestBuildReplyBody:
    def test_with_past_and_future(self):
        body = build_reply_body([("NATHAN GINN", {
            "past_visits": [_visit("6/12/26")],
            "future_visits": [_visit("7/31/26", stage="Not Checked In",
                                     day_time="Fri, 8:10am",
                                     visit_type="AUTO CHIRO Re-Eval (MVA Chiro)")],
        })])
        assert "NATHAN GINN" in body
        assert "6/12/26" in body
        assert "Completed" in body
        assert "7/31/26" in body
        assert "No past visits" not in body

    def test_summary_block(self):
        # The fields MedHub asks for by name: last visit, next appointment,
        # completed count. Ordering of the EMR lists must not matter.
        body = build_reply_body([("JANE ROE", {
            "past_visits": [_visit("6/20/26"), _visit("5/12/26"),
                            _visit("6/1/26", stage="No Show")],
            "future_visits": [_visit("8/14/26", stage=""), _visit("7/31/26", stage="")],
        })])
        assert "Date of last visit: 6/20/26" in body
        assert "Next scheduled appointment: 7/31/26" in body
        assert "Visits completed to date: 2" in body

    def test_empty_visits(self):
        body = build_reply_body([("JOHN DOE", {"past_visits": [], "future_visits": []})])
        assert "No past visits on record." in body
        assert "No upcoming appointments are currently scheduled." in body
        assert "Visits completed to date: 0" in body

    def test_multiple_patients_with_one_not_found(self):
        body = build_reply_body([
            ("BRENDA SIMMS", {"past_visits": [], "future_visits": [_visit("7/10/26", stage="")]}),
            ("HEIDI RAMIREZ", None),
        ])
        assert "BRENDA SIMMS" in body
        assert "HEIDI RAMIREZ" in body
        assert "We have no record of this patient at our clinic." in body
        assert "7/10/26" in body

    def test_no_llm_content(self):
        # The body must be fully deterministic — same input, same output.
        patients = [("A B", {"past_visits": [], "future_visits": []})]
        assert build_reply_body(patients) == build_reply_body(patients)


class TestParsePatientNames:
    def test_new_list_shape(self):
        assert _parse_patient_names({"patient_names": ["Jane Roe", "John Doe"]}) == [
            "Jane Roe", "John Doe"]

    def test_legacy_string_shape(self):
        assert _parse_patient_names({"patient_name": "Jane Roe"}) == ["Jane Roe"]

    def test_string_in_patient_names(self):
        assert _parse_patient_names({"patient_names": "Jane Roe"}) == ["Jane Roe"]

    def test_dedupe_case_insensitive_and_blank(self):
        assert _parse_patient_names(
            {"patient_names": ["Jane Roe", "JANE ROE", "", None]}) == ["Jane Roe"]

    def test_caps_at_max(self):
        names = [f"Patient {i}" for i in range(10)]
        assert len(_parse_patient_names({"patient_names": names})) == 4

    def test_empty(self):
        assert _parse_patient_names({}) == []
        assert _parse_patient_names({"patient_names": []}) == []


class TestParsePatientDobs:
    def test_aligned_and_normalized(self):
        parsed = {"patient_dobs": ["2007-09-18", "12/10/1995"]}
        assert _parse_patient_dobs(parsed, 2) == ["09/18/2007", "12/10/1995"]

    def test_pads_missing_to_count(self):
        assert _parse_patient_dobs({"patient_dobs": ["09/18/2007"]}, 3) == [
            "09/18/2007", "", ""]

    def test_trims_extra(self):
        assert _parse_patient_dobs(
            {"patient_dobs": ["09/18/2007", "01/01/2000"]}, 1) == ["09/18/2007"]

    def test_unparseable_becomes_empty(self):
        assert _parse_patient_dobs({"patient_dobs": ["unknown", None]}, 2) == ["", ""]

    def test_missing_key(self):
        assert _parse_patient_dobs({}, 2) == ["", ""]

    def test_string_shape(self):
        assert _parse_patient_dobs({"patient_dobs": "9/18/07"}, 1) == ["09/18/2007"]
