"""
Unit tests for the phi_scrub() regex redaction function.

These tests verify that each PHI pattern is correctly redacted and that
no false positives are introduced for ordinary clinical text.
"""
import pytest
from training.ingest_history import phi_scrub


class TestSsnRedaction:
    def test_ssn_standalone(self):
        assert phi_scrub("SSN: 123-45-6789") == "SSN: [SSN REDACTED]"

    def test_ssn_mid_sentence(self):
        result = phi_scrub("Patient SSN is 987-65-4321 per intake form.")
        assert "[SSN REDACTED]" in result
        assert "987-65-4321" not in result

    def test_ssn_at_start(self):
        result = phi_scrub("123-45-6789 is the SSN on file.")
        assert "[SSN REDACTED]" in result
        assert "123-45-6789" not in result

    def test_non_ssn_phone_not_flagged_as_ssn(self):
        # 10-digit phone should not match the 3-2-4 SSN pattern
        result = phi_scrub("555-867-5309")
        assert "[SSN REDACTED]" not in result

    def test_partial_ssn_not_redacted(self):
        # "123-45" alone should not trigger SSN pattern
        result = phi_scrub("Code 123-45 is valid.")
        assert "[SSN REDACTED]" not in result


class TestDobRedaction:
    def test_dob_numeric_slash(self):
        result = phi_scrub("DOB: 01/15/1985")
        assert "[DOB REDACTED]" in result
        assert "01/15/1985" not in result

    def test_dob_numeric_dash(self):
        result = phi_scrub("DOB: 03-22-1970")
        assert "[DOB REDACTED]" in result
        assert "03-22-1970" not in result

    def test_dob_two_digit_year(self):
        result = phi_scrub("DOB: 01/15/85")
        assert "[DOB REDACTED]" in result

    def test_date_of_birth_spelled_out(self):
        result = phi_scrub("Date of birth: 01/15/1985")
        assert "[DOB REDACTED]" in result
        assert "01/15/1985" not in result

    def test_date_of_birth_written_month(self):
        result = phi_scrub("date of birth January 15, 1985")
        assert "[DOB REDACTED]" in result

    def test_date_of_birth_abbreviated_month(self):
        result = phi_scrub("DOB: Feb 5, 1990")
        assert "[DOB REDACTED]" in result

    def test_standalone_date_not_flagged(self):
        # A date without a DOB label should not be redacted
        result = phi_scrub("The appointment is on 01/15/2025.")
        assert "[DOB REDACTED]" not in result

    def test_dob_case_insensitive(self):
        result = phi_scrub("dob: 07/04/1976")
        assert "[DOB REDACTED]" in result


class TestInsuranceIdRedaction:
    def test_member_id(self):
        result = phi_scrub("Member ID: XYZ123456789")
        assert "[INSURANCE ID REDACTED]" in result
        assert "XYZ123456789" not in result

    def test_member_hash(self):
        result = phi_scrub("Member #: ABC987654")
        assert "[INSURANCE ID REDACTED]" in result

    def test_policy_number(self):
        result = phi_scrub("Policy Number: POL9988776")
        assert "[INSURANCE ID REDACTED]" in result

    def test_policy_hash(self):
        result = phi_scrub("Policy #: XYZ001")
        assert "[INSURANCE ID REDACTED]" in result

    def test_insurance_id(self):
        result = phi_scrub("Insurance ID: INS123456")
        assert "[INSURANCE ID REDACTED]" in result

    def test_group_number(self):
        result = phi_scrub("Group Number: GRP55512")
        assert "[INSURANCE ID REDACTED]" in result

    def test_subscriber_id(self):
        result = phi_scrub("Subscriber ID: SUB998877")
        assert "[INSURANCE ID REDACTED]" in result

    def test_case_insensitive(self):
        result = phi_scrub("member id: abc123456")
        assert "[INSURANCE ID REDACTED]" in result

    def test_short_id_not_flagged(self):
        # IDs shorter than 5 chars should not be flagged (avoids false positives)
        result = phi_scrub("Group #: AB1")
        assert "[INSURANCE ID REDACTED]" not in result


class TestPhoneRedaction:
    def test_formatted_parentheses(self):
        result = phi_scrub("Call (555) 867-5309 for info.")
        assert "[PHONE REDACTED]" in result
        assert "867-5309" not in result

    def test_formatted_dashes(self):
        result = phi_scrub("Fax: 555-867-5309")
        assert "[PHONE REDACTED]" in result

    def test_formatted_dots(self):
        result = phi_scrub("Phone: 555.867.5309")
        assert "[PHONE REDACTED]" in result

    def test_formatted_spaces(self):
        result = phi_scrub("555 867 5309")
        assert "[PHONE REDACTED]" in result

    def test_bare_10_digits(self):
        result = phi_scrub("5558675309")
        assert "[PHONE REDACTED]" in result

    def test_short_number_not_flagged(self):
        # 7-digit and shorter numbers should not be flagged
        result = phi_scrub("Claim 8675309")
        assert "[PHONE REDACTED]" not in result

    def test_phone_in_context(self):
        result = phi_scrub("Referring office fax: (800) 555-0199. Please call.")
        assert "[PHONE REDACTED]" in result
        assert "555-0199" not in result


class TestMultiplePatternsAndEdgeCases:
    def test_multiple_patterns_in_one_string(self):
        text = "SSN: 123-45-6789, DOB: 01/15/1985, Phone: 555-867-5309"
        result = phi_scrub(text)
        assert "[SSN REDACTED]" in result
        assert "[DOB REDACTED]" in result
        assert "[PHONE REDACTED]" in result
        assert "123-45-6789" not in result
        assert "01/15/1985" not in result
        assert "867-5309" not in result

    def test_no_false_positive_plain_clinical_text(self):
        text = "Patient referred for physical therapy evaluation of lumbar spine."
        assert phi_scrub(text) == text

    def test_no_false_positive_icd_code(self):
        # ICD-10 codes should not be redacted
        text = "Diagnosis code: M54.5 (Low back pain)"
        assert phi_scrub(text) == text

    def test_no_false_positive_claim_number(self):
        text = "Claim number 12345 is under review."
        assert phi_scrub(text) == text

    def test_empty_string(self):
        assert phi_scrub("") == ""

    def test_none_equivalent_empty(self):
        # phi_scrub only accepts str; passing "" should return ""
        assert phi_scrub("") == ""

    def test_already_redacted_text_unchanged(self):
        # Applying phi_scrub twice should be idempotent on redacted output
        text = "SSN: 123-45-6789"
        once = phi_scrub(text)
        twice = phi_scrub(once)
        assert once == twice

    def test_full_referral_fax_scenario(self):
        fax = (
            "REFERRAL FAX\n"
            "Patient: Jane Doe\n"
            "DOB: 03/12/1978\n"
            "SSN: 456-78-9012\n"
            "Insurance Member ID: BCBS987654321\n"
            "Referring Provider Phone: (312) 555-0101\n"
            "Diagnosis: M54.5\n"
            "Requested: Physical Therapy Evaluation"
        )
        result = phi_scrub(fax)
        assert "[DOB REDACTED]" in result
        assert "[SSN REDACTED]" in result
        assert "[INSURANCE ID REDACTED]" in result
        assert "[PHONE REDACTED]" in result
        # Non-PHI content preserved
        assert "REFERRAL FAX" in result
        assert "M54.5" in result
        assert "Physical Therapy Evaluation" in result
        # PHI removed
        assert "03/12/1978" not in result
        assert "456-78-9012" not in result
        assert "BCBS987654321" not in result
        assert "555-0101" not in result
