"""
EMR tool — stub for future integration with a prompt-based EMR interface.

This module is intentionally a stub. Real EMR integration requires
site-specific credentials, HL7/FHIR endpoints, or proprietary APIs.
Implement the methods below once you have access to your EMR's API.

HIPAA note: Any data retrieved from the EMR is considered PHI. Ensure
all processing remains local and is not forwarded to external services.
"""

from __future__ import annotations

from loguru import logger


class PromptEmrTool:
    """Stub — replace method bodies with your EMR integration logic."""

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        logger.warning(
            "PromptEmrTool is a stub. Implement EMR integration before use."
        )

    def search_patient(self, name: str = "", dob: str = "", mrn: str = "") -> dict | None:
        """
        Search the EMR for a patient record.

        Args:
            name: Patient full name (optional).
            dob:  Date of birth in YYYY-MM-DD format (optional).
            mrn:  Medical record number (optional).

        Returns:
            A dict containing patient demographics, or None if not found.
        """
        raise NotImplementedError("PromptEmrTool.search_patient is not yet implemented")

    def get_referral_history(self, mrn: str) -> list[dict]:
        """
        Retrieve prior referrals for a patient.

        Args:
            mrn: Medical record number.

        Returns:
            List of referral records.
        """
        raise NotImplementedError("PromptEmrTool.get_referral_history is not yet implemented")

    def create_referral(self, mrn: str, referral_data: dict) -> dict:
        """
        Create a new referral order in the EMR.

        Args:
            mrn:           Patient's medical record number.
            referral_data: Structured referral dict (see ReferralAgent schema).

        Returns:
            Confirmation dict with order ID and status.
        """
        raise NotImplementedError("PromptEmrTool.create_referral is not yet implemented")

    def get_insurance(self, mrn: str) -> dict | None:
        """
        Retrieve the patient's active insurance information.

        Args:
            mrn: Medical record number.

        Returns:
            Dict with payer, plan, member ID, and group number, or None.
        """
        raise NotImplementedError("PromptEmrTool.get_insurance is not yet implemented")
