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
from pathlib import Path

from playwright.sync_api import sync_playwright

from bank_sync_common import notify, import_to_server, reconcile, ensure_server_running

# Passed by the daily Task Scheduler job (not by "Run Bank Scraper.bat", which
# stays interactive for manual double-click use). Nobody is present to press
# a key or complete a login step during an unattended run, so both blocking
# input() prompts below get skipped in that mode -- the run just finishes or
# aborts on its own instead of hanging forever.
UNATTENDED = "--unattended" in sys.argv

ROOT = Path(__file__).parent
CREDS_FILE = ROOT / "bank_credentials.json"
PROFILE_DIR = ROOT / "bank_browser_profile"
BANK_URL = "https://stateexchangebank.com/index.html"


def load_creds():
    if not CREDS_FILE.exists():
        print(f"Missing {CREDS_FILE.name}.")
        print(f"Copy bank_credentials.example.json to bank_credentials.json and fill in your real login.")
        sys.exit(1)
    creds = json.loads(CREDS_FILE.read_text())
    missing = [f for f in ("access_id", "password") if not creds.get(f)]
    accounts = creds.get("accounts") or []
    if not accounts:
        missing.append("accounts (non-empty list)")
    for a in accounts:
        if not a.get("bank_account_name") or not a.get("account_name"):
            missing.append("accounts[].bank_account_name/account_name")
            break
    if missing:
        print(f"{CREDS_FILE.name} is missing or malformed: {', '.join(missing)}")
        print("See bank_credentials.example.json for the expected format.")
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
    notify(
        "Simple Budget - Bank Sign-In Failed",
        "Didn't detect a successful bank login after 5 minutes.\n"
        "Check the browser window -- it may be stuck on a passcode/MFA step.",
    )
    if UNATTENDED:
        raise RuntimeError("Login not detected within 5 minutes (unattended run, no one to complete MFA)")
    input("Press Enter here once you're fully logged in... ")


def close_stuck_modal(page):
    # If a previous account's download errored out (e.g. expect_download
    # timed out), the bank's Angular modal is often left open -- and it
    # blocks all clicks outside itself, which silently breaks the *next*
    # account's attempt to even open the Accounts dropdown. Confirmed root
    # cause of FM Feeders / RG Exchange never actually being reached despite
    # Checking succeeding: this modal sat open across account iterations.
    # Defensively clear it before every account, not just after an error.
    if page.locator("ngb-modal-window").count() == 0:
        return
    try:
        page.locator(".icon-x-close-solid").first.click(timeout=2000)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(500)
    if page.locator("ngb-modal-window").count() > 0:
        # Still stuck -- reload is the reliable last resort; the persistent
        # profile keeps us logged in, so this doesn't require re-auth.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)


def download_csv(page, bank_account_name):
    close_stuck_modal(page)

    # Always go through the "Accounts" nav dropdown to reach the specific
    # account -- needed every time, since after finishing one account in a
    # multi-account run we're still sitting on that account's Activity page.
    page.click("nav >> text=Accounts")
    page.wait_for_timeout(800)
    # Scope to the open dropdown panel specifically -- a short name like
    # "Savings" can match a hidden duplicate elsewhere on the page, which
    # Playwright would otherwise try (and fail) to click.
    page.locator("#accounts_tab-dropdown").get_by_text(bank_account_name, exact=True).first.click()
    page.wait_for_timeout(1500)

    # Verify the switch actually landed -- confirmed failure mode: the click
    # above silently doesn't register (usually because a stuck modal from
    # the previous account intercepted it) and the page is left showing
    # whatever account we were already on. Downloading in that state
    # silently re-imports the WRONG account's data instead of failing loudly.
    # The account name isn't in a semantic <h1>/<h2> -- check for a
    # *visible* match instead (a hidden duplicate can still sit in the
    # closed Accounts dropdown's DOM, so count() alone isn't enough).
    matches = page.get_by_text(bank_account_name, exact=True)
    landed = any(matches.nth(i).is_visible() for i in range(matches.count()))
    if not landed:
        raise RuntimeError(
            f"Account switch to '{bank_account_name}' didn't land -- "
            f"still on a different account's page (possibly a stuck modal)."
        )

    page.click("text=Download")
    page.wait_for_timeout(1000)
    # "Spreadsheet CSV" is the default format per the bank's export menu.
    fmt = page.locator("text=Spreadsheet CSV").first
    if fmt.count():
        fmt.click()
        page.wait_for_timeout(500)
    with page.expect_download(timeout=30000) as dl_info:
        page.locator("text=Download").last.click()
    download = dl_info.value
    path = ROOT / "last_bank_export.csv"
    download.save_as(str(path))
    csv_text = path.read_text()

    # Close the Download dialog. Confirmed via live DevTools inspection:
    # it's an icon-font span (class="icon-x-close-solid"), not real text or
    # a plain <button> -- that's why every text/button/aria-label guess
    # found nothing to click.
    page.locator(".icon-x-close-solid").first.click(timeout=5000)
    page.wait_for_selector("ngb-modal-window", state="detached", timeout=5000)

    return csv_text


def run():
    creds = load_creds()
    ensure_server_running()
    with sync_playwright() as pw:
        context, page = open_bank_page(pw)
        try:
            if not is_logged_in(page):
                do_login(page, creds)

            for acc in creds["accounts"]:
                bank_name = acc["bank_account_name"]
                app_name = acc["account_name"]
                print(f"\n=== {bank_name} -> {app_name} ===")
                try:
                    print("Downloading CSV export...")
                    csv_text = download_csv(page, bank_name)
                    print("Download captured. Sending to Simple Budget server...")
                    result = import_to_server(app_name, csv_text)
                    print(f"Imported {result['added']} new transactions ({result['parsed']} total in the export).")
                    reconcile(app_name, csv_text)
                except Exception as e:
                    screenshot = ROOT / f"bank_scraper_error_{bank_name.replace(' ', '_')}.png"
                    page.screenshot(path=str(screenshot))
                    print(f"Something didn't work for {bank_name}. Screenshot saved to {screenshot}")
                    print(f"Error: {e}")
                    continue  # keep going with the remaining accounts
        finally:
            context.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("\n--- Something went wrong ---")
        traceback.print_exc()
        notify("Simple Budget - Bank Scraper Failed", f"The bank scraper crashed:\n{e}")
    finally:
        if not UNATTENDED:
            input("\nPress Enter to close this window... ")
