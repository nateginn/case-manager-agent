"""
Proof-of-concept: verify Prompt EMR login and Reports navigation.

Run this BEFORE integrating with Claire:
    .venv\\Scripts\\python.exe test_prompt_emr_login.py

Screenshots are saved to memory/emr_downloads/ so you can inspect
the actual UI and refine selectors in prompt_emr_browser_tool.py.

Pass --headed to watch the browser in real time (useful for debugging):
    .venv\\Scripts\\python.exe test_prompt_emr_login.py --headed
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from tools.prompt_emr_browser_tool import PromptEmrBrowserTool, _DOWNLOAD_DIR

headless = "--headed" not in sys.argv


def main() -> None:
    print(f"\n{'='*60}")
    print("Prompt EMR Login Test")
    print(f"{'='*60}")
    print(f"  Base URL  : {settings.PROMPT_EMR_BASE_URL}")
    print(f"  Username  : {settings.PROMPT_EMR_USERNAME or '(not set -- check .env)'}")
    print(f"  Headless  : {headless}")
    print(f"  Screenshots -> {_DOWNLOAD_DIR}")
    print(f"{'='*60}\n")

    if not settings.PROMPT_EMR_USERNAME or not settings.PROMPT_EMR_PASSWORD:
        print("ERROR: PROMPT_EMR_USERNAME and PROMPT_EMR_PASSWORD must be set in .env")
        sys.exit(1)

    with PromptEmrBrowserTool(headless=headless) as emr:

        # Step 1: Login
        print("Step 1: Logging in...")
        ok = emr.login()
        if not ok:
            # Grab whatever HTML we can after a failed login
            try:
                emr._page.wait_for_load_state("load", timeout=5_000)
                html = emr._page.content()
                html_path = _DOWNLOAD_DIR / "login_page.html"
                html_path.write_text(html, encoding="utf-8")
                print(f"  Login page HTML saved -> {html_path}")
            except Exception:
                pass
            print("FAILED -- login unsuccessful. Check memory/emr_downloads/login_error.png")
            sys.exit(1)

        print(f"  OK -- landed on: {emr._page.url}")

        # Step 2: Screenshot the landing page
        path = emr._screenshot("landing_page")
        print(f"  Landing page screenshot: {path}")

        # Save landing page HTML for nav structure analysis
        try:
            html = emr._page.content()
            html_path = _DOWNLOAD_DIR / "landing_page.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"  Landing page HTML saved -> {html_path}")
        except Exception as exc:
            print(f"  (HTML save failed: {exc})")

        # Step 3: Navigate to Reports
        print("\nStep 2: Navigating to Reports...")
        found = emr.navigate_to_reports()
        if found:
            print(f"  OK -- Reports URL: {emr._page.url}")
            print(f"  Reports screenshot: {_DOWNLOAD_DIR / 'reports_page.png'}")
        else:
            print("  WARNING -- could not reach Reports page automatically.")
            print("  Check memory/emr_downloads/reports_not_found.png")

        # Step 4: Print visible page text for nav structure
        print("\nStep 3: Visible page text (first 2000 chars):")
        print("-" * 40)
        try:
            emr._page.wait_for_load_state("load", timeout=10_000)
            text = emr._page.inner_text("body")
            print(text[:2000])
        except Exception as exc:
            print(f"  (could not extract page text: {exc})")
        print("-" * 40)

    print(f"\nDone. Review screenshots in: {_DOWNLOAD_DIR}")


if __name__ == "__main__":
    main()
