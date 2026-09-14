#!/usr/bin/env python3
from flask import Flask, jsonify, request
import json
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

GROCY_URL = "https://localhost"
GROCY_KEY = "YOUR_GROCY_API_KEY"
HEADERS = {"GROCY-API-KEY": GROCY_KEY, "Content-Type": "application/json"}

CONSUMABLES_GROUP_ID = 14  # Grocy product group "Household Consumables"
DEFAULT_LOCATION_ID = 4    # "Pantry" -- matches the other seed products, trivially changeable per-product in Grocy's own UI afterward
PIECE_QU_ID = 2             # Grocy quantity unit "Piece" -- everything here is tracked per individual unit

# How many individual units a "box" adds for each tracked product, used only
# by the Restock action below -- not a Grocy quantity-unit conversion, just a
# quick default for the common case of buying one box. User-editable via
# POST /box_size/<id>, persisted here since Grocy has nowhere natural to
# store this (it's not a real quantity-unit conversion, just our own
# shortcut), so it needs to survive service restarts on its own.
BOX_SIZES_FILE = os.path.expanduser("~/consumables_box_sizes.json")
DEFAULT_BOX_SIZES = {
    "45": 30,   # Garbage Bags
    "46": 20,   # Recycling Bags
    "47": 100,  # Coffee Filters
    "48": 30,   # Detergent Pods
}


def _load_box_sizes():
    if os.path.exists(BOX_SIZES_FILE):
        try:
            with open(BOX_SIZES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_BOX_SIZES)


def _save_box_sizes(sizes):
    with open(BOX_SIZES_FILE, "w") as f:
        json.dump(sizes, f)


BOX_SIZES = _load_box_sizes()


@app.route("/consume/<int:product_id>", methods=["POST"])
def consume(product_id):
    try:
        r = requests.post(
            f"{GROCY_URL}/api/stock/products/{product_id}/consume",
            headers=HEADERS,
            json={"amount": 1, "transaction_type": "consume", "allow_substock_change": True},
            verify=False, timeout=10,
        )
        r.raise_for_status()

        # Let Grocy's own below-minimum logic decide what needs restocking --
        # catches any other low item too, not just this one.
        r2 = requests.post(
            f"{GROCY_URL}/api/stock/shoppinglist/add-missing-products",
            headers=HEADERS, json={"list_id": 1},
            verify=False, timeout=10,
        )
        r2.raise_for_status()
        return jsonify(success=True)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/restock/<int:product_id>", methods=["POST"])
def restock(product_id):
    amount = BOX_SIZES.get(str(product_id), 1)
    try:
        r = requests.post(
            f"{GROCY_URL}/api/stock/products/{product_id}/add",
            headers=HEADERS,
            json={"amount": amount, "transaction_type": "purchase"},
            verify=False, timeout=10,
        )
        r.raise_for_status()
        return jsonify(success=True, amount=amount)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/box_sizes")
def box_sizes():
    return jsonify(BOX_SIZES)


@app.route("/box_size/<int:product_id>", methods=["POST"])
def set_box_size(product_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="amount must be a whole number"), 400
    if amount < 1:
        return jsonify(success=False, error="amount must be at least 1"), 400
    BOX_SIZES[str(product_id)] = amount
    _save_box_sizes(BOX_SIZES)
    return jsonify(success=True, amount=amount)


@app.route("/set_amount/<int:product_id>", methods=["POST"])
def set_amount(product_id):
    # Grocy's own "inventory" transaction is exactly a physical stock count
    # correction -- it sets stock to an absolute value (recorded as an
    # inventory-correction transaction) rather than adding/consuming a
    # delta, which is exactly what "reset to 0" or "manually fix a wrong
    # count" need. Confirmed against a live product before wiring this up,
    # not assumed from the API docs alone.
    data = request.get_json(force=True, silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="amount must be a number"), 400
    if amount < 0:
        return jsonify(success=False, error="amount cannot be negative"), 400
    try:
        r = requests.post(
            f"{GROCY_URL}/api/stock/products/{product_id}/inventory",
            headers=HEADERS,
            json={"new_amount": amount},
            verify=False, timeout=10,
        )
        r.raise_for_status()
        return jsonify(success=True, amount=amount)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/add_product", methods=["POST"])
def add_product():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(success=False, error="Name is required"), 400
    try:
        min_stock = max(0, int(data.get("min_stock", 5)))
    except (TypeError, ValueError):
        min_stock = 5
    try:
        box_size = max(1, int(data.get("box_size", 1)))
    except (TypeError, ValueError):
        box_size = 1
    try:
        initial_amount = max(0.0, float(data.get("initial_amount", 0)))
    except (TypeError, ValueError):
        initial_amount = 0.0

    try:
        r = requests.post(
            f"{GROCY_URL}/api/objects/products",
            headers=HEADERS,
            json={
                "name": name,
                "product_group_id": CONSUMABLES_GROUP_ID,
                "location_id": DEFAULT_LOCATION_ID,
                "qu_id_purchase": PIECE_QU_ID,
                "qu_id_stock": PIECE_QU_ID,
                "min_stock_amount": min_stock,
            },
            verify=False, timeout=10,
        )
        r.raise_for_status()
        product_id = r.json()["created_object_id"]

        BOX_SIZES[str(product_id)] = box_size
        _save_box_sizes(BOX_SIZES)

        if initial_amount > 0:
            r2 = requests.post(
                f"{GROCY_URL}/api/stock/products/{product_id}/add",
                headers=HEADERS,
                json={"amount": initial_amount, "transaction_type": "purchase"},
                verify=False, timeout=10,
            )
            r2.raise_for_status()

        return jsonify(success=True, product_id=product_id)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/remove_product/<int:product_id>", methods=["POST"])
def remove_product(product_id):
    # Deleting the Grocy product removes its stock history along with it --
    # the confirmation step for this lives entirely in the dashboard UI
    # (a second explicit tap), since that's the only place a mistaken press
    # can actually be caught before it's irreversible.
    try:
        r = requests.delete(
            f"{GROCY_URL}/api/objects/products/{product_id}",
            headers=HEADERS,
            verify=False, timeout=10,
        )
        r.raise_for_status()
        BOX_SIZES.pop(str(product_id), None)
        _save_box_sizes(BOX_SIZES)
        return jsonify(success=True)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/status")
def status():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9400)
