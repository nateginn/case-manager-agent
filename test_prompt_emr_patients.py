"""
Explorer script: navigate to /patients, search for a patient, screenshot each step.
Run with: .venv\Scripts\python.exe test_prompt_emr_patients.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from tools.prompt_emr_browser_tool import PromptEmrBrowserTool, _DOWNLOAD_DIR

PATIENT_NAME = "Nathan Ginn"


def dump_html(page, name: str) -> None:
    try:
        html = page.content()
        path = _DOWNLOAD_DIR / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        print(f"  HTML saved -> {path}")
    except Exception as exc:
        print(f"  HTML save failed: {exc}")


def main() -> None:
    print(f"\n{'='*60}")
    print("Prompt EMR Patients Explorer")
    print(f"  Patient: {PATIENT_NAME}")
    print(f"  Screenshots -> {_DOWNLOAD_DIR}")
    print(f"{'='*60}\n")

    with PromptEmrBrowserTool(headless=False) as emr:

        # Step 1: Login (uses cached session)
        print("Step 1: Login...")
        if not emr.login():
            print("FAILED — check login_error.png")
            sys.exit(1)
        print(f"  OK — {emr._page.url}")

        # Step 2: Navigate to /patients
        print("\nStep 2: Navigating to /patients...")
        emr._page.goto("https://go.promptemr.com/patients", wait_until="load", timeout=20_000)
        emr._page.wait_for_timeout(2_000)
        emr._screenshot("patients_page")
        dump_html(emr._page, "patients_page")
        print(f"  URL: {emr._page.url}")
        print(f"  Page text preview: {emr._page.inner_text('body')[:500]}")

        # Step 3: Find the search box and type patient name
        print(f"\nStep 3: Searching for '{PATIENT_NAME}'...")

        # Try common search input selectors
        search_selectors = [
            "input[placeholder*='search' i]",
            "input[placeholder*='patient' i]",
            "input[placeholder*='name' i]",
            "input[type='search']",
            "input[type='text']",
        ]
        filled = False
        for sel in search_selectors:
            try:
                loc = emr._page.locator(sel).first
                if loc.count() > 0:
                    print(f"  Found search input: {sel}")
                    loc.fill(PATIENT_NAME)
                    emr._page.wait_for_timeout(2_000)
                    emr._screenshot("patients_search_results")
                    dump_html(emr._page, "patients_search_results")
                    print(f"  Search results text: {emr._page.inner_text('body')[:800]}")
                    filled = True
                    break
            except Exception as exc:
                print(f"  Selector {sel} failed: {exc}")

        if not filled:
            print("  WARNING: no search input found")
            dump_html(emr._page, "patients_no_search")
            sys.exit(1)

        # Step 4: Click the patient in the results list
        print(f"\nStep 4: Selecting patient '{PATIENT_NAME}'...")
        try:
            # Patient cards are div.patient — click the first result
            patient_row = emr._page.locator("div.patient").first
            if patient_row.count() > 0:
                patient_row.click()
                emr._page.wait_for_timeout(3_000)
                emr._screenshot("patient_profile")
                dump_html(emr._page, "patient_profile")
                print(f"  URL after click: {emr._page.url}")
                print(f"  Profile text: {emr._page.inner_text('body')[:1000]}")
            else:
                print("  No div.patient found — check patients_search_results.html")
                emr._screenshot("patient_not_found")
        except Exception as exc:
            print(f"  Patient selection failed: {exc}")
            emr._screenshot("patient_selection_error")

        # Step 5: Click the Visits tab (Quasar chip with name="visitList")
        print("\nStep 5: Clicking Visits tab...")
        try:
            visits_tab = emr._page.locator('div[name="visitList"]').first
            if visits_tab.count() > 0:
                visits_tab.click()
                emr._page.wait_for_timeout(2_000)
                emr._screenshot("visits_tab")
                dump_html(emr._page, "visits_tab")
                print(f"  URL: {emr._page.url}")
                print(f"  Visits page text:\n{emr._page.inner_text('body')[:2000]}")
            else:
                print("  div[name='visitList'] not found")
                chips = emr._page.locator(".q-chip").all_inner_texts()
                print(f"  All q-chip tabs: {chips}")
        except Exception as exc:
            print(f"  Visits tab failed: {exc}")
            emr._screenshot("visits_tab_error")

    print(f"\nDone. Review screenshots in: {_DOWNLOAD_DIR}")


if __name__ == "__main__":
    main()
