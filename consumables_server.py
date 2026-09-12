#!/usr/bin/env python3
from flask import Flask, jsonify
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

GROCY_URL = "https://localhost"
GROCY_KEY = "YOUR_GROCY_API_KEY"
HEADERS = {"GROCY-API-KEY": GROCY_KEY, "Content-Type": "application/json"}

# How many individual units a "box" adds for each tracked product, used only
# by the Restock action below -- not a Grocy quantity-unit conversion, just a
# quick default for the common case of buying one box.
BOX_SIZES = {
    45: 30,   # Garbage Bags
    46: 20,   # Recycling Bags
    47: 100,  # Coffee Filters
    48: 30,   # Detergent Pods
}


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
    amount = BOX_SIZES.get(product_id, 1)
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


@app.route("/status")
def status():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9400)
