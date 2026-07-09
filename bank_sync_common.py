"""
Shared helpers used by both bank_scraper.py (State Exchange Bank) and
cfcu_scraper.py (Communication FCU): the desktop popup, posting a CSV export
to the Simple Budget server, and checking the resulting app balance against
the bank's own reported balance so a scrape/parse bug can't silently drift.
"""

import csv
import ctypes
import io
import json
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

SERVER = "http://localhost:5112"
SERVER_PORT = 5112
ROOT = Path(__file__).parent
PYTHONW = ROOT.parent / "py314" / "pythonw.exe"

MB_ICONWARNING = 0x30
MB_TOPMOST = 0x40000


def ensure_server_running():
    """Start server.py if it isn't already listening. Scheduled/unattended
    scrapers can't rely on a separate at-logon task having already started
    it -- that trigger has been unreliable (doesn't fire on sleep/wake, only
    a real logon event) -- so each scraper brings the server up itself,
    same as the desktop launcher does."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0:
            return  # already running
    subprocess.Popen(
        [str(PYTHONW), str(ROOT / "server.py")],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0:
                return
        time.sleep(0.3)


def notify(title, message):
    # Native MessageBoxW via ctypes -- no installs, no accounts, stays on
    # top of everything so a failure can't be missed even if you've
    # switched away from the browser window.
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, MB_ICONWARNING | MB_TOPMOST)
    except Exception:
        pass


def import_to_server(account_name, csv_text):
    body = json.dumps({"account_name": account_name, "csv_text": csv_text}).encode()
    req = urllib.request.Request(
        f"{SERVER}/api/import_csv", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def latest_balance_from_csv(csv_text):
    # The export is newest-first, and every row (including summary rows
    # like "Daily Ledger Bal") carries the running balance as of that row --
    # so the first row's Balance is the bank's current balance.
    for row in csv.DictReader(io.StringIO(csv_text)):
        raw = (row.get("Balance") or "").strip()
        if not raw:
            continue
        try:
            return float(re.sub(r"[^0-9.\-]", "", raw))
        except ValueError:
            continue
    return None


def app_balance(account_name):
    with urllib.request.urlopen(f"{SERVER}/api/state") as resp:
        data = json.loads(resp.read().decode())
    account = next((a for a in data["accounts"] if a["name"] == account_name), None)
    if not account:
        return None
    total = account["startingBalance"] + sum(
        t["amount"] for t in data["transactions"] if t["accountId"] == account["id"]
    )
    return total


def reconcile(app_name, csv_text):
    """Compare the bank's own reported balance to the app's computed
    balance after import, and pop up a notification if they don't match."""
    bank_bal = latest_balance_from_csv(csv_text)
    app_bal = app_balance(app_name)
    if bank_bal is None or app_bal is None:
        print(f"Couldn't verify balance for {app_name} (missing data) -- skipping reconciliation check.")
        return
    diff = round(bank_bal - app_bal, 2)
    if abs(diff) < 0.01:
        print(f"Reconciled: {app_name} balance matches bank (${bank_bal:,.2f}).")
    else:
        print(f"MISMATCH for {app_name}: bank ${bank_bal:,.2f} vs budget ${app_bal:,.2f} (diff ${diff:,.2f}).")
        notify(
            f"Simple Budget - {app_name} Balance Mismatch",
            f"Bank says ${bank_bal:,.2f}\n"
            f"Budget shows ${app_bal:,.2f}\n"
            f"Difference: ${diff:,.2f}\n\n"
            "Check for a missing, duplicate, or miscategorized transaction.",
        )
