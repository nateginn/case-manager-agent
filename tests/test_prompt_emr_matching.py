"""Unit tests for patient-name matching logic in PromptEmrBrowserTool.

Pure logic only — no browser, no network. The class is not instantiated;
the matching helpers are static methods.
"""

from tools.prompt_emr_browser_tool import PromptEmrBrowserTool as T
from utils import normalize_dob


class TestNameVariants:
    def test_four_token_hispanic_name(self):
        # The real case that motivated this: EMR chart lacks the second
        # given name.
        assert T._name_variants("Ariadne Alejandra Orozco Mendoza") == [
            "Ariadne Alejandra Orozco Mendoza",
            "Ariadne Orozco Mendoza",
            "Ariadne Mendoza",
        ]

    def test_three_token_name(self):
        assert T._name_variants("Fanny Antonio Romero") == [
            "Fanny Antonio Romero",
            "Fanny Romero",
        ]

    def test_two_token_name(self):
        assert T._name_variants("Jordan Wagner") == ["Jordan Wagner"]

    def test_never_single_token(self):
        for variants in (T._name_variants("Cher"), T._name_variants("")):
            assert all(len(v.split()) >= 2 for v in variants)

    def test_extra_whitespace(self):
        assert T._name_variants("  Jane   Roe ") == ["Jane Roe"]


class TestScoreNameMatch:
    def test_emr_subset_of_email_name(self):
        score = T._score_name_match(
            "Ariadne Orozco Mendoza", "Ariadne Alejandra Orozco Mendoza")
        assert score == 1.0

    def test_first_name_mismatch_is_zero(self):
        # Surname overlap alone must never select a patient.
        assert T._score_name_match(
            "Becky Orozco Mendoza", "Ariadne Alejandra Orozco Mendoza") == 0.0

    def test_partial_overlap_below_threshold(self):
        # 1 of 2 tokens: first name matches, surname doesn't.
        score = T._score_name_match("Ariadne Martinez", "Ariadne Alejandra Orozco Mendoza")
        assert 0 < score < T._MATCH_THRESHOLD

    def test_case_insensitive_and_commas(self):
        assert T._score_name_match("OROZCO, ARIADNE", "Ariadne Orozco Mendoza") > 0

    def test_empty_inputs(self):
        assert T._score_name_match("", "Jane Roe") == 0.0
        assert T._score_name_match("Jane Roe", "") == 0.0


class TestNormalizeDob:
    def test_common_formats(self):
        for raw in ("2007-09-18", "09/18/2007", "9/18/2007", "09-18-2007",
                    "9.18.2007", "September 18, 2007", "Sep 18 2007",
                    "18 September 2007"):
            assert normalize_dob(raw) == "09/18/2007", raw

    def test_two_digit_year(self):
        assert normalize_dob("9/18/07") == "09/18/2007"
        assert normalize_dob("12/10/95") == "12/10/1995"

    def test_unparseable_and_empty(self):
        assert normalize_dob("") == ""
        assert normalize_dob("unknown") == ""
        assert normalize_dob("13/45/2007") == ""
