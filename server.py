"""
Simple Budget server: serves the app itself, holds the shared budget data
(so phone + laptop see the same thing), and bridges Plaid bank sync.
Run with: python server.py
Listens on http://localhost:5112 (also reachable via Tailscale from other devices)
"""

import csv
import io
import json
import os
import re
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as http

ROOT = Path(__file__).parent
app = Flask(__name__, static_folder=None)
CORS(app)  # allow requests from other devices on the tailnet

CONFIG_FILE = ROOT / "plaid_config.json"
STATE_FILE = ROOT / "budget_data.json"

# Guards every read-modify-write of STATE_FILE. Without this, a bank-import
# request (which reads the file, spends time parsing/merging CSV rows, then
# writes the whole file back) can race a plain browser save that happens in
# between: the import's write silently overwrites whatever the browser just
# saved with the older snapshot it started with. Confirmed as the real cause
# of budgeted-amount edits appearing to save and then reverting later.
STATE_LOCK = threading.Lock()

# Every already-imported month up through June 2026 is fully categorized by
# hand -- the scheduled bank scraper re-downloads a rolling CSV window on
# every run, and this floor keeps it from ever re-adding (or partially
# re-matching) anything that old, even if a description happens not to match
# byte-for-byte against what's already in budget_data.json. Only touches new
# imports going forward; nothing already in the budget is affected.
IMPORT_CUTOFF_DATE = "2026-07-01"

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(data):
    cfg = load_config()
    cfg.update(data)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def plaid_base():
    cfg = load_config()
    env = cfg.get("plaid_env", "sandbox")
    return f"https://{env}.plaid.com"

def plaid_headers():
    cfg = load_config()
    return {
        "Content-Type": "application/json",
        "PLAID-CLIENT-ID": cfg.get("client_id", ""),
        "PLAID-SECRET": cfg.get("secret", ""),
    }

def plaid_post(endpoint, body):
    r = http.post(f"{plaid_base()}{endpoint}", json=body, headers=plaid_headers())
    return r.json(), r.status_code

# ── Static app serving ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(ROOT, "manifest.json")

@app.route("/sw.js")
def service_worker():
    return send_from_directory(ROOT, "sw.js")

@app.route("/icons/<path:filename>")
def icons(filename):
    return send_from_directory(ROOT / "icons", filename)

# ── Shared budget state ──────────────────────────────────────────────────────

@app.route("/api/state", methods=["GET"])
def get_state():
    if STATE_FILE.exists():
        return jsonify(json.loads(STATE_FILE.read_text()))
    return jsonify(None)

@app.route("/api/state", methods=["POST"])
def save_state():
    # A browser tab left open for hours (or another device) only ever loads
    # the server's data once, then blindly re-saves its own full in-memory
    # copy on every local edit -- with no check at all, that silently
    # overwrites anything newer that arrived from elsewhere (the daily bank
    # scraper, another tab, a phone) in the meantime. Confirmed cause of a
    # real data-loss incident. _rev is a simple monotonic counter: a save is
    # only accepted if the client's _rev matches what's actually on disk
    # right now -- i.e. the client's copy is provably not stale. A stale
    # client gets rejected (409) with the real current data instead of
    # winning a silent last-write-wins race.
    body = request.json
    with STATE_LOCK:
        current = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
        current_rev = (current or {}).get("_rev", 0)
        client_rev = body.get("_rev", 0) if isinstance(body.get("_rev", 0), int) else 0
        if current is not None and client_rev != current_rev:
            return jsonify({"ok": False, "conflict": True, "serverState": current}), 409
        new_rev = current_rev + 1
        body["_rev"] = new_rev
        STATE_FILE.write_text(json.dumps(body))
    return jsonify({"ok": True, "rev": new_rev})

# ── Bank CSV scrape import ───────────────────────────────────────────────────

def parse_bank_date(s):
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", (s or "").strip())
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"

def parse_bank_csv(text):
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        d = parse_bank_date(row.get("Date", ""))
        if not d:
            continue
        try:
            amount = float(re.sub(r"[^0-9.\-]", "", row.get("Amount", "")))
        except ValueError:
            continue  # non-transaction summary row, e.g. "Daily Ledger Bal"
        bank_category = (row.get("Category") or "").strip()
        memo = (row.get("Memo") or "").strip() or (f"(bank category: {bank_category})" if bank_category else "")
        rows.append({
            "date": d,
            "description": (row.get("Description") or "(no description)").strip(),
            "amount": amount,
            "memo": memo,
        })
    return rows

def is_placeholder_description(desc):
    # Pending transactions often show as a raw reference number before the
    # real merchant name posts, e.g. "000000154126".
    return bool(re.fullmatch(r"0*\d+", (desc or "").strip()))

def _shifted(d, days):
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()

@app.route("/api/import_csv", methods=["POST"])
def import_csv():
    body = request.json
    account_name = body.get("account_name")
    csv_text = body.get("csv_text", "")

    all_rows = parse_bank_csv(csv_text)
    rows = [r for r in all_rows if r["date"] >= IMPORT_CUTOFF_DATE]

    with STATE_LOCK:
        data = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
        if not data:
            return jsonify({"error": "No budget data yet — open the app once in a browser first, then re-run this"}), 400

        account = next((a for a in data["accounts"] if a["name"] == account_name), None)
        if not account:
            return jsonify({"error": f"No account named '{account_name}' in the budget"}), 400
        account_id = account["id"]

        by_date = {}
        for t in data["transactions"]:
            if t["accountId"] == account_id:
                by_date.setdefault(t["date"], []).append(t)

        # How many rows in *this* CSV batch share a given (date, description) --
        # used below so the "amount finalized" settle only fires when it's
        # unambiguous which existing transaction a row corresponds to.
        new_desc_counts = {}
        for r in rows:
            new_desc_counts[(r["date"], r["description"])] = new_desc_counts.get((r["date"], r["description"]), 0) + 1

        claimed = set()  # existing transaction ids already matched to a row this run
        added = 0
        updated = 0
        for r in rows:
            # Exact match already present -- true duplicate, skip. Checked
            # across a +/-1 day window (not just the exact date) because a
            # transaction can show as pending on one day and post the next
            # with the identical description/amount -- same-day-only used to
            # miss that shift and double-count it.
            exact = next(
                (t for d in (r["date"], _shifted(r["date"], -1), _shifted(r["date"], 1))
                 for t in by_date.get(d, [])
                 if t["id"] not in claimed and t["description"] == r["description"] and t["amount"] == r["amount"]),
                None,
            )
            if exact:
                claimed.add(exact["id"])
                continue

            same_day = by_date.get(r["date"], [])
            # Never a settle candidate: something you typed in by hand isn't
            # a bank placeholder that's "still settling" -- if it happens to
            # share a date+description with an incoming bank row, the right
            # answer is to add the bank row as its own transaction (worst
            # case: a duplicate you notice and merge), never to silently
            # rewrite the amount/description you actually entered.
            same_day_unclaimed = [t for t in same_day if t["id"] not in claimed and not t.get("manual")]
            existing_desc_count = sum(1 for t in same_day_unclaimed if t["description"] == r["description"])

            # Same transaction settling: pending placeholder -> real name, or
            # same description with the amount finalized (e.g. a tip added).
            # Only trusted when there's exactly one existing same-day
            # transaction with this description AND exactly one row in this
            # batch with it too -- otherwise it's ambiguous which one goes
            # with which (e.g. three separate same-day charges at the same
            # place), and guessing silently destroyed real transactions
            # before. When ambiguous, falls through to just adding it as a
            # new row instead -- a duplicate a human can merge later beats
            # data quietly vanishing.
            candidate = None
            placeholder_candidates = [t for t in same_day_unclaimed if is_placeholder_description(t["description"]) and not is_placeholder_description(r["description"])]
            if len(placeholder_candidates) == 1:
                candidate = placeholder_candidates[0]
            elif existing_desc_count == 1 and new_desc_counts.get((r["date"], r["description"]), 0) == 1:
                candidate = next((t for t in same_day_unclaimed if t["description"] == r["description"] and t["amount"] != r["amount"]), None)

            if candidate:
                candidate["description"] = r["description"]
                candidate["amount"] = r["amount"]
                if r["memo"]:
                    candidate["memo"] = r["memo"]
                claimed.add(candidate["id"])
                updated += 1
                continue

            new_tx = {
                "id": uuid.uuid4().hex[:8],
                "date": r["date"],
                "accountId": account_id,
                "description": r["description"],
                "amount": r["amount"],
                "group": "Deposits" if r["amount"] > 0 else None,
                "categoryId": None,
                "memo": r["memo"],
            }
            data["transactions"].append(new_tx)
            by_date.setdefault(r["date"], []).append(new_tx)
            claimed.add(new_tx["id"])
            added += 1

        # Bump _rev here too (see save_state's comment) -- otherwise a
        # browser tab that hasn't reloaded since before this import would
        # still think its stale in-memory copy is current, and a later edit
        # in that tab would overwrite everything this import just added.
        data["_rev"] = data.get("_rev", 0) + 1
        STATE_FILE.write_text(json.dumps(data))

    return jsonify({"ok": True, "added": added, "updated": updated, "parsed": len(all_rows)})

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    cfg = load_config()
    return jsonify({
        "configured": bool(cfg.get("client_id") and cfg.get("secret")),
        "env": cfg.get("plaid_env", "sandbox"),
        "connected_items": list(cfg.get("access_tokens", {}).keys()),
    })

@app.route("/api/configure", methods=["POST"])
def configure():
    body = request.json
    save_config({
        "client_id": body["client_id"].strip(),
        "secret": body["secret"].strip(),
        "plaid_env": body.get("plaid_env", "sandbox"),
    })
    return jsonify({"ok": True})

@app.route("/api/create_link_token", methods=["POST"])
def create_link_token():
    body = {
        "user": {"client_user_id": "simple-budget-user"},
        "client_name": "Simple Budget",
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
    }
    data, status = plaid_post("/link/token/create", body)
    if status != 200:
        return jsonify({"error": data.get("error_message", "Failed to create link token")}), 400
    return jsonify({"link_token": data["link_token"]})

@app.route("/api/exchange_token", methods=["POST"])
def exchange_token():
    body = request.json
    public_token = body["public_token"]
    account_name = body.get("account_name", "Checking")

    data, status = plaid_post("/item/public_token/exchange", {"public_token": public_token})
    if status != 200:
        return jsonify({"error": data.get("error_message", "Exchange failed")}), 400

    access_token = data["access_token"]
    item_id = data["item_id"]

    # Store access token keyed by account name
    cfg = load_config()
    tokens = cfg.get("access_tokens", {})
    tokens[account_name] = {"access_token": access_token, "item_id": item_id, "cursor": None}
    save_config({"access_tokens": tokens})

    return jsonify({"ok": True, "item_id": item_id})

@app.route("/api/sync_transactions", methods=["POST"])
def sync_transactions():
    body = request.json
    account_name = body.get("account_name")

    cfg = load_config()
    tokens = cfg.get("access_tokens", {})

    if account_name and account_name not in tokens:
        return jsonify({"error": f"No connection for '{account_name}'"}), 400

    # Sync all connected accounts if no specific one requested
    targets = {account_name: tokens[account_name]} if account_name else tokens
    all_added = []
    all_removed = []

    for name, info in targets.items():
        cursor = info.get("cursor")
        access_token = info["access_token"]

        added, modified, removed, next_cursor = [], [], [], cursor

        # Page through all updates
        has_more = True
        while has_more:
            req_body = {"access_token": access_token, "options": {"include_personal_finance_category": True}}
            if next_cursor:
                req_body["cursor"] = next_cursor

            data, status = plaid_post("/transactions/sync", req_body)
            if status != 200:
                return jsonify({"error": data.get("error_message", "Sync failed")}), 400

            added.extend(data.get("added", []))
            modified.extend(data.get("modified", []))
            removed.extend(data.get("removed", []))
            next_cursor = data.get("next_cursor", next_cursor)
            has_more = data.get("has_more", False)

        # Save updated cursor
        tokens[name]["cursor"] = next_cursor
        save_config({"access_tokens": tokens})

        def convert(tx):
            # Plaid: positive amount = money leaving account (debit)
            # Our clone: positive = inflow, negative = outflow
            return {
                "id": tx["transaction_id"],
                "date": tx["date"],
                "payee": tx.get("merchant_name") or tx.get("name", ""),
                "memo": tx.get("name", ""),
                "amount": -tx["amount"],   # flip sign
                "pending": tx.get("pending", False),
                "plaid_category": (tx.get("personal_finance_category", {}) or {}).get("primary", ""),
                "account_name": name,
            }

        all_added.extend([convert(t) for t in added])
        all_removed.extend([t["transaction_id"] for t in removed])

    return jsonify({"added": all_added, "removed": all_removed})

@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    body = request.json
    name = body.get("account_name")
    cfg = load_config()
    tokens = cfg.get("access_tokens", {})
    if name in tokens:
        del tokens[name]
        save_config({"access_tokens": tokens})
    return jsonify({"ok": True})

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  Simple Budget Server")
    print("  Running at http://localhost:5112 (also reachable via Tailscale)")
    print("  Keep this window open while using the app.")
    print("  Ctrl+C to stop.")
    print()
    app.run(host="0.0.0.0", port=5112, debug=False)
