#!/usr/bin/env python3
from flask import Flask, jsonify, request
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

GROCY_URL = "https://localhost"
GROCY_KEY = "YOUR_GROCY_API_KEY"
HEADERS = {"GROCY-API-KEY": GROCY_KEY, "Content-Type": "application/json"}

CONSUMABLES_GROUP_ID = 14   # kept in sync with dashboard.html / consumables_server.py, for labeling only
EMERGENCY_FOOD_GROUP_ID = 13
DEFAULT_LOCATION_ID = 4     # "Pantry" -- same inherited default as consumables_server.py's add_product
PIECE_QU_ID = 2             # "Piece" -- default QU for a product created from an unknown-barcode registration


def _lookup_by_barcode(barcode):
    # Grocy returns 400 (not 404) for an unregistered barcode -- confirmed
    # live against this instance, not assumed from the API docs.
    r = requests.get(f"{GROCY_URL}/api/stock/products/by-barcode/{barcode}",
                      headers=HEADERS, verify=False, timeout=10)
    if r.status_code >= 400:
        return None
    return r.json()


@app.route("/status")
def status():
    return jsonify(ok=True)


@app.route("/lookup/<barcode>")
def lookup(barcode):
    try:
        product = _lookup_by_barcode(barcode)
        if product is None:
            return jsonify(success=True, found=False, barcode=barcode)
        return jsonify(success=True, found=True, product=product)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/action", methods=["POST"])
def scan_action():
    data = request.get_json(force=True, silent=True) or {}
    barcode = (data.get("barcode") or "").strip()
    mode = data.get("mode")
    if not barcode or mode not in ("consume", "restock"):
        return jsonify(success=False, error="barcode and mode ('consume'|'restock') are required"), 400
    try:
        product = _lookup_by_barcode(barcode)
        if product is None:
            return jsonify(success=True, found=False, barcode=barcode)

        product_id = product["product"]["id"]
        name = product["product"]["name"]
        group_id = product["product"].get("product_group_id")

        # Always 1 unit per physical scan, regardless of mode -- a box of 12
        # gets scanned as one unit today (or bulk-added via the existing
        # Household Items "Bought a box" button); this isn't the place to
        # reintroduce consumables_server.py's BOX_SIZES-style bulk quantity,
        # since it doesn't generalize to an arbitrary scanned food product.
        if mode == "consume":
            r = requests.post(
                f"{GROCY_URL}/api/stock/products/{product_id}/consume",
                headers=HEADERS,
                json={"amount": 1, "transaction_type": "consume", "allow_substock_change": True},
                verify=False, timeout=10,
            )
            r.raise_for_status()
            # Mirrors consumables_server.py's /consume/<id> -- let Grocy's own
            # below-minimum logic catch anything now low, not just this item.
            r2 = requests.post(
                f"{GROCY_URL}/api/stock/shoppinglist/add-missing-products",
                headers=HEADERS, json={"list_id": 1}, verify=False, timeout=10,
            )
            r2.raise_for_status()
        else:
            r = requests.post(
                f"{GROCY_URL}/api/stock/products/{product_id}/add",
                headers=HEADERS,
                json={"amount": 1, "transaction_type": "purchase"},
                verify=False, timeout=10,
            )
            r.raise_for_status()

        updated = _lookup_by_barcode(barcode)
        new_stock = updated.get("stock_amount") if updated else None

        return jsonify(success=True, found=True, action=mode, product={
            "id": product_id, "name": name, "product_group_id": group_id,
        }, new_stock=new_stock)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(force=True, silent=True) or {}
    barcode = (data.get("barcode") or "").strip()
    if not barcode:
        return jsonify(success=False, error="barcode is required"), 400

    product_id = data.get("product_id")
    try:
        if not product_id:
            name = (data.get("new_product_name") or "").strip()
            if not name:
                return jsonify(success=False, error="product_id or new_product_name is required"), 400
            group_id = data.get("product_group_id")   # may be null -- Household (14) / Emergency Food (13) / none
            body = {
                "name": name,
                "location_id": DEFAULT_LOCATION_ID,
                "qu_id_purchase": PIECE_QU_ID,
                "qu_id_stock": PIECE_QU_ID,
                "min_stock_amount": max(0, int(data.get("min_stock_amount", 0))),
            }
            if group_id:
                body["product_group_id"] = int(group_id)
            r = requests.post(f"{GROCY_URL}/api/objects/products", headers=HEADERS,
                               json=body, verify=False, timeout=10)
            r.raise_for_status()
            product_id = r.json()["created_object_id"]

        r2 = requests.post(f"{GROCY_URL}/api/objects/product_barcodes", headers=HEADERS,
                            json={"product_id": product_id, "barcode": barcode},
                            verify=False, timeout=10)
        r2.raise_for_status()
        return jsonify(success=True, product_id=product_id, barcode=barcode)
    except requests.RequestException as e:
        return jsonify(success=False, error=str(e)), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9500)
