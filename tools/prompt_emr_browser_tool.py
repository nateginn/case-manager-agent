"""
Prompt EMR browser-automation client.

Uses Playwright's sync API to log in via Auth0 and interact with
go.promptemr.com.  Session cookies are cached locally so login only
happens once per session (re-authenticates on expiry automatically).

HIPAA note: all data stays on-machine.  Nothing is forwarded to
external services; the only outbound calls are to promptemr.com itself
using the practitioner's own credentials.

Usage:
    with PromptEmrBrowserTool() as emr:
        if emr.login():
            rows = emr.download_appointments_report(patient_name="Smith John")
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from loguru import logger
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from config import settings

_SESSION_PATH = Path(__file__).parent.parent / "memory" / "prompt_emr_session.json"
_DOWNLOAD_DIR = Path(__file__).parent.parent / "memory" / "emr_downloads"


class PromptEmrBrowserTool:
    """
    Browser-automation client for Prompt EMR.
    Use as a context manager to ensure the browser is always closed cleanly.
    """

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "PromptEmrBrowserTool":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
        )

        if _SESSION_PATH.exists():
            try:
                state = json.loads(_SESSION_PATH.read_text(encoding="utf-8"))
                self._context = self._browser.new_context(
                    storage_state=state,
                    accept_downloads=True,
                )
                logger.info("PromptEmrBrowserTool: restored cached session")
            except Exception as exc:
                logger.warning("PromptEmrBrowserTool: session cache invalid, will re-login: {}", exc)
                self._context = self._browser.new_context(accept_downloads=True)
        else:
            self._context = self._browser.new_context(accept_downloads=True)

        self._page = self._context.new_page()
        return self

    def __exit__(self, *_) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _screenshot(self, name: str) -> Path:
        path = _DOWNLOAD_DIR / f"{name}.png"
        try:
            self._page.screenshot(path=str(path), full_page=True)
            logger.info("PromptEmrBrowserTool: screenshot -> {}", path)
        except Exception as exc:
            logger.warning("PromptEmrBrowserTool: screenshot failed ({}): {}", name, exc)
        return path

    def _save_session(self) -> None:
        state = self._context.storage_state()
        _SESSION_PATH.write_text(json.dumps(state), encoding="utf-8")
        logger.info("PromptEmrBrowserTool: session cached")

    def _fill_first(self, selectors: list[str], value: str) -> bool:
        """Try each CSS selector in order; fill the first one found. Returns True on success."""
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    def _click_first(self, selectors: list[str]) -> bool:
        """Try each selector; click the first element found. Returns True on success."""
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if loc.count() > 0:
                    loc.click()
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _is_authenticated(self) -> bool:
        """
        Check whether the current page is authenticated.
        Waits 3 s after load so the SPA has time to process its auth state
        and potentially redirect to authenticate.promptemr.com before we judge.
        """
        self._page.wait_for_timeout(3_000)
        url = self._page.url
        return "go.promptemr.com" in url and "authenticate.promptemr.com" not in url

    def login(self) -> bool:
        """
        Log in via Auth0 username/password form.
        Tries the cached session first; falls back to full credential login.
        Returns True on success.
        """
        # Check if we're already authenticated on the current page
        if "go.promptemr.com" in self._page.url and "authenticate.promptemr.com" not in self._page.url:
            # Do a short wait to let SPA redirect if it needs to
            self._page.wait_for_timeout(2_000)
            if "go.promptemr.com" in self._page.url:
                logger.info("PromptEmrBrowserTool: session active on {}", self._page.url)
                return True

        if not settings.PROMPT_EMR_USERNAME or not settings.PROMPT_EMR_PASSWORD:
            logger.error(
                "PromptEmrBrowserTool: PROMPT_EMR_USERNAME / PROMPT_EMR_PASSWORD "
                "not set in .env -- cannot log in"
            )
            return False

        logger.info("PromptEmrBrowserTool: navigating to app...")
        try:
            # Navigate to the app — may redirect to authenticate.promptemr.com if not logged in,
            # or go straight to go.promptemr.com if the session cookie is still valid.
            self._page.goto(settings.PROMPT_EMR_BASE_URL, wait_until="load", timeout=20_000)

            # Give SPA a moment to process auth state / redirects
            self._page.wait_for_timeout(3_000)

            # Already authenticated via cached session — nothing more to do
            if "go.promptemr.com" in self._page.url and "authenticate.promptemr.com" not in self._page.url:
                logger.info("PromptEmrBrowserTool: session cookie valid, on {}", self._page.url)
                self._save_session()
                return True

            # We're on the auth page — wait for the login form to appear
            self._page.wait_for_selector(
                'input[type="email"], input[type="password"], input[name="username"], #username',
                timeout=15_000,
            )
            logger.info("PromptEmrBrowserTool: login form found, filling credentials...")

            self._fill_first(
                ['#1-email', 'input[type="email"]', 'input[name="email"]', 'input[name="username"]'],
                settings.PROMPT_EMR_USERNAME,
            )
            self._fill_first(
                ['#1-password', 'input[type="password"]', 'input[name="password"]'],
                settings.PROMPT_EMR_PASSWORD,
            )

            self._screenshot("login_form_filled")

            # The "Verify you are human" checkbox is an anti-bot widget that
            # detects automation and rejects automated clicks.  We fill the
            # credentials and then wait for the user to:
            #   1. click "Verify you are human" in the browser window
            #   2. click LOG IN
            # We watch for the navigation event rather than blocking on stdin.
            print("\n" + "=" * 60)
            print("BROWSER READY — credentials have been filled automatically.")
            print("  Please complete login in the browser window:")
            print("  1. Click 'Verify you are human'")
            print("  2. Wait for it to verify (checkmark)")
            print("  3. Click LOG IN")
            print("  (waiting up to 5 minutes for you to complete this)")
            print("=" * 60)

            # Wait for redirect back to go.promptemr.com (up to 5 min)
            self._page.wait_for_url("https://go.promptemr.com/**", timeout=300_000)

            # Wait for the SPA to finish processing the auth callback
            # (it exchanges the auth code for a token and stores it)
            self._page.wait_for_timeout(4_000)

            # Verify we're still on the app and haven't been bounced back to auth
            if "authenticate.promptemr.com" in self._page.url:
                logger.error("PromptEmrBrowserTool: redirected back to auth after submit -- wrong credentials or MFA?")
                self._screenshot("login_error")
                return False

            self._save_session()
            logger.info("PromptEmrBrowserTool: login successful, on {}", self._page.url)
            return True

        except Exception as exc:
            logger.error("PromptEmrBrowserTool: login failed: {}", exc)
            self._screenshot("login_error")
            return False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_reports(self) -> bool:
        """True if the current page is the authenticated Reports page."""
        url = self._page.url
        return (
            "go.promptemr.com" in url
            and "authenticate.promptemr.com" not in url
            and "report" in url.lower()
        )

    def navigate_to_reports(self) -> bool:
        """
        Navigate to the Reports section using SPA click-navigation where possible
        to avoid full page reloads that lose the in-memory auth token.
        Falls back to direct goto only if click navigation fails.
        """
        if not self.login():
            return False

        # Already there (login flow may have landed us on /reports)?
        if self._on_reports():
            logger.info("PromptEmrBrowserTool: already on reports: {}", self._page.url)
            self._screenshot("reports_page")
            return True

        # Direct goto first — URL /reports is confirmed working for this app.
        # Session cookie survives the reload (Auth0 tokens stored in localStorage).
        for path in ["/reports", "/report", "/Reports", "/app/reports"]:
            try:
                self._page.goto(
                    f"{settings.PROMPT_EMR_BASE_URL}{path}",
                    wait_until="load",
                    timeout=15_000,
                )
                self._page.wait_for_timeout(3_000)
                if self._on_reports():
                    logger.info("PromptEmrBrowserTool: Reports found via direct goto -> {}", self._page.url)
                    self._screenshot("reports_page")
                    return True
            except Exception:
                continue

        logger.warning("PromptEmrBrowserTool: could not reach Reports page")
        self._screenshot("reports_not_found")
        return False

    # ------------------------------------------------------------------
    # Patients
    # ------------------------------------------------------------------

    def search_patient(self, patient_name: str) -> dict | None:
        """
        Search /patients for a patient by name and open their profile.

        Verified flow (2026-07-06):
          1. goto /patients
          2. fill input[placeholder='Search patients']
          3. click the first div.patient result card
          4. profile URL becomes /patients/<uuid>

        Returns:
            {"name", "account_number", "profile_url"} on success, None if
            no result was found.
        """
        if not self.login():
            return None

        try:
            self._page.goto(
                f"{settings.PROMPT_EMR_BASE_URL}/patients",
                wait_until="load",
                timeout=20_000,
            )
            self._page.wait_for_timeout(2_000)

            filled = self._fill_first(
                ["input[placeholder*='search' i]", "input[type='search']"],
                patient_name,
            )
            if not filled:
                logger.error("PromptEmrBrowserTool: patient search input not found")
                self._screenshot("patient_search_error")
                return None

            # Wait for the results list to render
            self._page.wait_for_timeout(2_000)

            result = self._page.locator("div.patient").first
            if result.count() == 0:
                logger.warning("PromptEmrBrowserTool: no patient result for {!r}", patient_name)
                self._screenshot("patient_not_found")
                return None

            result_name = result.inner_text().split("\n")[0].strip()
            result.click()
            self._page.wait_for_timeout(3_000)

            profile_url = self._page.url
            if "/patients/" not in profile_url:
                logger.warning("PromptEmrBrowserTool: click did not open a profile (url={})", profile_url)
                self._screenshot("patient_profile_error")
                return None

            # Account number appears as "Acct#: 1001681-ARR" on the profile
            acct = ""
            try:
                body_text = self._page.inner_text("body")
                for line in body_text.split("\n"):
                    if line.strip().startswith("Acct#:"):
                        acct = line.split("Acct#:", 1)[1].strip()
                        break
            except Exception:
                pass

            logger.info("PromptEmrBrowserTool: opened patient {!r} ({})", result_name, profile_url)
            return {
                "name": result_name,
                "account_number": acct,
                "profile_url": profile_url,
            }

        except Exception as exc:
            logger.error("PromptEmrBrowserTool: search_patient failed: {}", exc)
            self._screenshot("patient_search_error")
            return None

    def get_patient_visits(self, patient_name: str) -> dict | None:
        """
        Search for a patient, open their Visits tab, and parse the visit table.

        Verified flow (2026-07-06): the Visits tab is a Quasar chip with
        name="visitList"; the visit table rows contain cells:
          [gutter, date ("7/31/26\\nFri, 8:10am"), visit info
          ("AUTO CHIRO Re-Eval (MVA Chiro)\\nVisit #1"), provider
          ("N. Ginn\\nART Greeley"), stage ("Not Checked In"), actions]
        Section header rows ("Future Visits" / "Past Visits") split the table.

        Returns:
            {
              "patient": {...},          # from search_patient
              "future_visits": [ {date, day_time, visit_type, visit_number,
                                  provider, facility, stage}, ... ],
              "past_visits":   [ ...same shape... ],
            }
            or None if the patient was not found.
        """
        patient = self.search_patient(patient_name)
        if patient is None:
            return None

        try:
            visits_tab = self._page.locator('div[name="visitList"]').first
            if visits_tab.count() == 0:
                logger.error("PromptEmrBrowserTool: Visits tab (div[name='visitList']) not found")
                self._screenshot("visits_tab_missing")
                return None
            visits_tab.click()
            self._page.wait_for_timeout(2_500)
            self._screenshot("visits_parsed")

            future: list[dict] = []
            past: list[dict] = []
            section = None

            rows = self._page.locator("tbody tr")
            for i in range(rows.count()):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
                joined = " ".join(cell_texts)

                if "Future Visits" in joined and len([t for t in cell_texts if t]) <= 1:
                    section = "future"
                    continue
                if "Past Visits" in joined and len([t for t in cell_texts if t]) <= 1:
                    section = "past"
                    continue

                visit = self._parse_visit_row(cell_texts)
                if visit is None:
                    continue
                if section == "past":
                    past.append(visit)
                else:
                    future.append(visit)

            logger.info(
                "PromptEmrBrowserTool: parsed {} future / {} past visits for {!r}",
                len(future), len(past), patient["name"],
            )
            return {
                "patient": patient,
                "future_visits": future,
                "past_visits": past,
            }

        except Exception as exc:
            logger.error("PromptEmrBrowserTool: get_patient_visits failed: {}", exc)
            self._screenshot("visits_parse_error")
            return None

    @staticmethod
    def _parse_visit_row(cell_texts: list[str]) -> dict | None:
        """
        Turn one visit-table row's cell texts into a structured dict.
        Returns None for header/spacer rows that don't hold visit data.
        """
        # Data rows have at least: gutter, date, visit info, provider, stage
        if len(cell_texts) < 5:
            return None

        date_cell = cell_texts[1]
        # Date cell looks like "7/31/26\nFri, 8:10am" — require a date-ish first line
        date_lines = [ln.strip() for ln in date_cell.split("\n") if ln.strip()]
        if not date_lines or "/" not in date_lines[0]:
            return None

        info_lines = [ln.strip() for ln in cell_texts[2].split("\n") if ln.strip()]
        provider_lines = [ln.strip() for ln in cell_texts[3].split("\n") if ln.strip()]

        return {
            "date": date_lines[0],
            "day_time": date_lines[1] if len(date_lines) > 1 else "",
            "visit_type": info_lines[0] if info_lines else "",
            "visit_number": info_lines[1] if len(info_lines) > 1 else "",
            "provider": provider_lines[0] if provider_lines else "",
            "facility": provider_lines[1] if len(provider_lines) > 1 else "",
            "stage": cell_texts[4].strip(),
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def download_appointments_report(
        self,
        patient_name: str = "",
        date_from: str = "",   # YYYY-MM-DD
        date_to: str = "",     # YYYY-MM-DD
    ) -> list[dict]:
        """
        Apply patient/date filters on the Reports page, download the CSV,
        and return parsed rows as a list of dicts.

        NOTE: filter selectors are best-effort placeholders until we have
        verified screenshots of the Reports UI.  Run test_prompt_emr_login.py
        first to capture those screenshots, then refine selectors here.
        """
        if not self.navigate_to_reports():
            return []

        try:
            if patient_name:
                filled = self._fill_first([
                    "input[placeholder*='patient' i]",
                    "input[placeholder*='name' i]",
                    "input[aria-label*='patient' i]",
                    "input[name*='patient' i]",
                ], patient_name)
                if not filled:
                    logger.warning("PromptEmrBrowserTool: patient name filter input not found")

            if date_from:
                self._fill_first([
                    "input[placeholder*='from' i]",
                    "input[placeholder*='start' i]",
                    "input[aria-label*='from' i]",
                    "input[type='date']:first-of-type",
                ], date_from)

            if date_to:
                self._fill_first([
                    "input[placeholder*='to' i]",
                    "input[placeholder*='end' i]",
                    "input[aria-label*='to' i]",
                    "input[type='date']:last-of-type",
                ], date_to)

            self._screenshot("reports_filters_applied")

            # Click Run / Apply / Search
            ran = self._click_first([
                "button:has-text('Run')",
                "button:has-text('Apply')",
                "button:has-text('Search')",
                "button:has-text('Generate')",
                "button:has-text('Filter')",
            ])
            if not ran:
                logger.warning("PromptEmrBrowserTool: Run/Apply button not found — attempting download anyway")
            else:
                self._page.wait_for_load_state("networkidle", timeout=30_000)

            self._screenshot("reports_results")

            # Download CSV
            fname = f"appointments_{patient_name.replace(' ', '_') or 'all'}_{date_from or 'nodate'}.csv"
            with self._page.expect_download(timeout=30_000) as dl_info:
                clicked = self._click_first([
                    "button:has-text('CSV')",
                    "a:has-text('CSV')",
                    "button:has-text('Export')",
                    "a:has-text('Export')",
                    "button:has-text('Download')",
                    "a:has-text('Download')",
                ])
                if not clicked:
                    logger.warning("PromptEmrBrowserTool: no CSV/Export/Download button found")
                    self._screenshot("no_export_button")
                    return []

            download = dl_info.value
            csv_path = _DOWNLOAD_DIR / fname
            download.save_as(str(csv_path))
            logger.info("PromptEmrBrowserTool: CSV saved → {}", csv_path)
            return _parse_csv(csv_path)

        except Exception as exc:
            logger.error("PromptEmrBrowserTool: download_appointments_report failed: {}", exc)
            self._screenshot("download_error")
            return []


def _parse_csv(path: Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        logger.error("PromptEmrBrowserTool: CSV parse failed: {}", exc)
        return []
