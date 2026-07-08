"""
Self-hosted bank scraper for Simple Budget.

Logs into your bank, downloads the CSV export, and imports it directly into
your shared budget via the local server's /api/import_csv endpoint.

Setup (one-time):
  1. Copy bank_credentials.example.json to bank_credentials.json
  2. Fill in your real Access ID / password / the app account name it maps to
     (bank_credentials.json is gitignored -- it never leaves this machine)

First run: a visible browser window opens so you can complete any MFA/passcode
prompt yourself. After that, your session is saved to bank_session_state.json
and future runs are fully unattended (headless) until that session expires,
at which point it falls back to a visible window again automatically.

Run with: python bank_scraper.py
"""

import json
import sys
import traceback
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
CREDS_FILE = ROOT / "bank_credentials.json"
SESSION_FILE = ROOT / "bank_session_state.json"
BANK_URL = "https://stateexchangebank.com/index.html"
SERVER = "http://localhost:5112"


REQUIRED_FIELDS = ["access_id", "password", "bank_account_name", "account_name"]


def load_creds():
    if not CREDS_FILE.exists():
        print(f"Missing {CREDS_FILE.name}.")
        print(f"Copy bank_credentials.example.json to bank_credentials.json and fill in your real login.")
        sys.exit(1)
    creds = json.loads(CREDS_FILE.read_text())
    missing = [f for f in REQUIRED_FIELDS if not creds.get(f)]
    if missing:
        print(f"{CREDS_FILE.name} is missing: {', '.join(missing)}")
        print("Add those fields (see bank_credentials.example.json for the format) and try again.")
        sys.exit(1)
    return creds


def is_logged_in(page):
    # If the "Sign in to Online Banking" button is present, we're logged out.
    return page.locator("text=Sign in to Online Banking").count() == 0


def interactive_login(pw, creds):
    print("Opening a visible browser window for login...")
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BANK_URL, wait_until="domcontentloaded")
    page.click("text=Sign in to Online Banking")

    try:
        # The Password field isn't actually type="password" in this bank's
        # markup (confirmed via screenshot), so target both fields by their
        # placeholder text instead -- fill() auto-waits for them to appear.
        page.get_by_placeholder("Access ID").fill(creds["access_id"])
        page.get_by_placeholder("Password").fill(creds["password"])
        page.click("text=Log In")
        print("Auto-filled login form.")
    except Exception as e:
        screenshot = ROOT / "bank_scraper_login_error.png"
        page.screenshot(path=str(screenshot))
        print(f"Auto-fill didn't work ({e}). Screenshot saved to {screenshot}")
        print("Please type your Access ID and Password into the browser window yourself.")

    print()
    print("If your bank asks for a passcode or MFA step, complete it now in the browser window.")
    print("Waiting for login to finish on its own (up to 5 minutes) -- no need to press anything here.")
    waited = 0
    while waited < 300:
        if is_logged_in(page):
            print("Login detected.")
            break
        page.wait_for_timeout(1000)
        waited += 1
    else:
        print("Didn't detect a successful login after 5 minutes.")
        input("Press Enter here once you're fully logged in... ")

    context.storage_state(path=str(SESSION_FILE))
    print("Session saved.")
    return browser, context, page


def get_authenticated_page(pw, creds):
    if SESSION_FILE.exists():
        # Headless gets blocked by the bank's bot protection (403), so stay
        # visible even on session-reuse runs -- it's still fully automatic,
        # just not invisible.
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()
        page.goto(BANK_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if is_logged_in(page):
            return browser, context, page
        context.close()
        browser.close()
        print("Saved session expired.")

    return interactive_login(pw, creds)


def download_csv(page, bank_account_name):
    # Use the "Accounts" nav dropdown to get to the specific account, rather
    # than relying on whatever page happens to load after login.
    if page.locator("text=Download").count() == 0:
        page.click("nav >> text=Accounts")
        page.wait_for_timeout(800)
        page.click(f"text={bank_account_name}")
        page.wait_for_timeout(1500)
    page.click("text=Download")
    page.wait_for_timeout(1000)
    # "Spreadsheet CSV" is the default format per the bank's export menu.
    fmt = page.locator("text=Spreadsheet CSV").first
    if fmt.count():
        fmt.click()
        page.wait_for_timeout(500)
    with page.expect_download(timeout=15000) as dl_info:
        page.locator("text=Download").last.click()
    download = dl_info.value
    path = ROOT / "last_bank_export.csv"
    download.save_as(str(path))
    return path.read_text()


def import_to_server(account_name, csv_text):
    body = json.dumps({"account_name": account_name, "csv_text": csv_text}).encode()
    req = urllib.request.Request(
        f"{SERVER}/api/import_csv", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def run():
    creds = load_creds()
    csv_text = None
    with sync_playwright() as pw:
        browser, context, page = get_authenticated_page(pw, creds)
        try:
            print("Downloading CSV export...")
            csv_text = download_csv(page, creds["bank_account_name"])
            print("Download captured.")
        except Exception as e:
            screenshot = ROOT / "bank_scraper_error.png"
            page.screenshot(path=str(screenshot))
            print(f"Something didn't match on the page. Screenshot saved to {screenshot}")
            print(f"Error: {e}")
        finally:
            context.close()
            browser.close()

    if csv_text is None:
        return
    print("Sending to Simple Budget server...")
    result = import_to_server(creds["account_name"], csv_text)
    print(f"Imported {result['added']} new transactions ({result['parsed']} total in the export).")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("\n--- Something went wrong ---")
        traceback.print_exc()
    finally:
        input("\nPress Enter to close this window... ")
