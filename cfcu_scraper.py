"""
Self-hosted bank scraper for Simple Budget -- Communication Federal Credit
Union (CFCU).

Same idea as bank_scraper.py (persistent browser profile, download CSV,
POST to the local server), but CFCU's login sits behind Cloudflare
Turnstile, which hard-blocks automated/CDP-controlled browsers -- so unlike
SEB, login here is entirely manual. This script pops up a notification,
opens a blank CFCU login page, and waits for you to log in yourself; once
logged in, the CSV download/import/reconcile steps run automatically.

Setup (one-time):
  Fill in cfcu_credentials.json (gitignored) with the app account name(s)
  it maps to.

Run with: python cfcu_scraper.py
"""

import json
import re
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

from bank_sync_common import notify, import_to_server, reconcile

ROOT = Path(__file__).parent
CREDS_FILE = ROOT / "cfcu_credentials.json"
PROFILE_DIR = ROOT / "cfcu_browser_profile"


def load_creds():
    if not CREDS_FILE.exists():
        print(f"Missing {CREDS_FILE.name}.")
        sys.exit(1)
    creds = json.loads(CREDS_FILE.read_text())
    missing = [f for f in ("login_url", "access_id", "password") if not creds.get(f)]
    accounts = creds.get("accounts") or []
    if not accounts:
        missing.append("accounts (non-empty list)")
    if missing:
        print(f"{CREDS_FILE.name} is missing or malformed: {', '.join(missing)}")
        sys.exit(1)
    return creds


def is_logged_in(page):
    # The login form (#username/#password) only exists on the Authentication
    # page -- if it's not there, we're either already logged in or somewhere
    # else in the app.
    return "/Authentication" not in page.url and page.locator("#username").count() == 0


def do_login(page, creds):
    # CFCU's login page sits behind Cloudflare Turnstile, which hard-blocks
    # (Error 600010) when it detects an automated/CDP-controlled browser --
    # confirmed live, not just a checkbox click. Auto-filling and
    # auto-clicking through it isn't something to push past: that's
    # circumventing a bot-detection control CFCU deliberately put on their
    # login, not just a UI quirk to route around.
    #
    # So this step is entirely manual: pop up a blocking notification,
    # then open a blank login page and wait for you to log in yourself
    # (username, password, verification, any MFA). Once logged in, the
    # rest of the run (navigating to the account, downloading the CSV,
    # importing, reconciling) proceeds automatically again.
    notify(
        "Simple Budget - CFCU Monthly Sync",
        "Click OK, then log into CFCU yourself in the browser window that opens.\n\n"
        "Once you're logged in, the sync will continue automatically.",
    )
    page.goto(creds["login_url"], wait_until="domcontentloaded")

    print("Waiting for you to log in manually (up to 10 minutes)...")
    waited = 0
    while waited < 600:
        if "/Authentication" not in page.url:
            print("Login detected.")
            return
        page.wait_for_timeout(1000)
        waited += 1
    print("Didn't detect a successful login after 10 minutes.")
    notify(
        "Simple Budget - CFCU Sign-In Timed Out",
        "Didn't detect a successful CFCU login after 10 minutes -- run it again when ready.",
    )
    raise RuntimeError("CFCU login not completed in time")


def run():
    creds = load_creds()
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(creds["login_url"], wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            if not is_logged_in(page):
                do_login(page, creds)

            # --- TEMPORARY: dump the logged-in dashboard so the real
            # download flow can be built from what's actually there. ---
            print("\n=== Logged-in page inspection ===")
            print("URL:", page.url)
            print("TITLE:", page.title())
            nav_links = page.locator("nav a, nav button").all()
            for el in nav_links[:40]:
                try:
                    print("NAV:", el.inner_text().strip()[:40])
                except Exception:
                    pass
            print("\n--- links containing 'account' ---")
            for el in page.get_by_text(re.compile("account", re.I)).all()[:20]:
                try:
                    print("MATCH:", el.inner_text().strip()[:60])
                except Exception:
                    pass
            page.wait_for_timeout(600000)
        finally:
            context.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("\n--- Something went wrong ---")
        traceback.print_exc()
        notify("Simple Budget - CFCU Scraper Failed", f"The CFCU scraper crashed:\n{e}")
    finally:
        input("\nPress Enter to close this window... ")
