"""
Self-hosted bank scraper for Simple Budget.

Logs into your bank, downloads the CSV export, and imports it directly into
your shared budget via the local server's /api/import_csv endpoint.

Setup (one-time):
  1. Copy bank_credentials.example.json to bank_credentials.json
  2. Fill in your real Access ID / password / the app account name it maps to
     (bank_credentials.json is gitignored -- it never leaves this machine)

Uses a persistent browser profile (like a real Chrome profile that stays
logged in) rather than exporting/reimporting cookies into a fresh browser
each run -- banks trust an actual returning browser far more than a
freshly-spawned one with copied-in cookies, so this should need far fewer
manual logins over time. First run still needs you to log in / handle any
passcode once; after that the same profile is reused automatically.

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
PROFILE_DIR = ROOT / "bank_browser_profile"
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


def open_bank_page(pw):
    # A persistent profile directory behaves like a real, continuously-used
    # Chrome profile (cookies, local storage, device fingerprint all stick
    # around naturally) instead of a fresh throwaway browser each time.
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), headless=False,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(BANK_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    return context, page


def find_login_frame(page, timeout_ms=8000):
    # The login form is served by a third-party banking platform
    # (seblo.banking.apiture.com, per Chrome's saved-password suggestion)
    # embedded in an iframe -- Playwright won't reach into it via page.*
    # locators, which is why DevTools could "see" #aid but the script
    # couldn't interact with it. Find that frame specifically.
    waited = 0
    while waited < timeout_ms:
        for frame in page.frames:
            url = frame.url or ""
            if "apiture" in url or "seblo" in url:
                return frame
        page.wait_for_timeout(300)
        waited += 300
    return page  # fallback: maybe it really is on the main page


def do_login(page, creds):
    print("Not logged in yet -- logging in...")
    page.click("text=Sign in to Online Banking")
    target = find_login_frame(page)

    try:
        # Confirmed via live DevTools inspection: the Access ID field is
        # <input id="aid">. Target it directly -- exact and unambiguous,
        # unlike placeholder/type-based guesses which kept timing out.
        target.wait_for_selector("#aid", state="visible", timeout=10000)
        target.locator("#aid").fill(creds["access_id"])
        # Password field's id wasn't confirmed, so fall back to placeholder
        # matching with .first to dodge any hidden-duplicate ambiguity, and
        # force=True to skip animation-stability checks that may be racing
        # a modal fade-in.
        pw_field = target.get_by_placeholder("Password").first
        pw_field.wait_for(state="visible", timeout=10000)
        try:
            pw_field.fill(creds["password"])
        except Exception:
            pw_field.fill(creds["password"], force=True)
        target.locator("text=Log In").click()
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
            return
        page.wait_for_timeout(1000)
        waited += 1
    print("Didn't detect a successful login after 5 minutes.")
    input("Press Enter here once you're fully logged in... ")


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
        context, page = open_bank_page(pw)
        try:
            if not is_logged_in(page):
                do_login(page, creds)
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
