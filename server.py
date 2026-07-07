"""
Local Plaid bridge server for Simple Budget bank sync.
Run with: python server.py
Listens on http://localhost:5112
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests as http

app = Flask(__name__)
CORS(app)  # allow the HTML file (file://) to call this server

CONFIG_FILE = Path(__file__).parent / "plaid_config.json"

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
    print("  Simple Budget - Bank Sync Server")
    print("  Running at http://localhost:5112")
    print("  Keep this window open while using the app.")
    print("  Ctrl+C to stop.")
    print()
    app.run(port=5112, debug=False)
