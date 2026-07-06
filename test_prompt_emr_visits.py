"""
End-to-end test of PromptEmrBrowserTool.get_patient_visits().

Run with:  .venv/Scripts/python.exe test_prompt_emr_visits.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.prompt_emr_browser_tool import PromptEmrBrowserTool

PATIENT_NAME = "Nathan Ginn"


def main() -> None:
    print(f"\nFetching visits for: {PATIENT_NAME}\n")

    with PromptEmrBrowserTool(headless=True) as emr:
        result = emr.get_patient_visits(PATIENT_NAME)

    if result is None:
        print("FAILED -- no result (check memory/emr_downloads/ screenshots)")
        sys.exit(1)

    print(json.dumps(result, indent=2))
    print(f"\nPatient : {result['patient']['name']}  (Acct# {result['patient']['account_number']})")
    print(f"Future visits: {len(result['future_visits'])}")
    print(f"Past visits  : {len(result['past_visits'])}")


if __name__ == "__main__":
    main()
