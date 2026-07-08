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
    STATE_FILE.write_text(json.dumps(request.json))
    return jsonify({"ok": True})

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

@app.route("/api/import_csv", methods=["POST"])
def import_csv():
    body = request.json
    account_name = body.get("account_name")
    csv_text = body.get("csv_text", "")

    if not STATE_FILE.exists():
        return jsonify({"error": "No budget data yet — open the app once first"}), 400
    data = json.loads(STATE_FILE.read_text())

    account = next((a for a in data["accounts"] if a["name"] == account_name), None)
    if not account:
        return jsonify({"error": f"No account named '{account_name}' in the budget"}), 400
    account_id = account["id"]

    rows = parse_bank_csv(csv_text)
    existing_keys = {
        f"{t['date']}|{t['description']}|{t['amount']}"
        for t in data["transactions"] if t["accountId"] == account_id
    }
    added = 0
    for r in rows:
        key = f"{r['date']}|{r['description']}|{r['amount']}"
        if key in existing_keys:
            continue
        data["transactions"].append({
            "id": uuid.uuid4().hex[:8],
            "date": r["date"],
            "accountId": account_id,
            "description": r["description"],
            "amount": r["amount"],
            "group": "Deposits" if r["amount"] > 0 else None,
            "categoryId": None,
            "memo": r["memo"],
        })
        existing_keys.add(key)
        added += 1

    STATE_FILE.write_text(json.dumps(data))
    return jsonify({"ok": True, "added": added, "parsed": len(rows)})

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
