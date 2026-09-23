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


@app.route("/lookup_external/<barcode>")
def lookup_external(barcode):
    """Best-effort product-name suggestion from free public barcode databases,
    for the unknown-barcode registration flow -- confirmed live against this
    household's own real, already-registered barcodes (exact match on a
    Subway sauce, a Knorr pasta side, etc.) before relying on it in the UI.
    Never raises: a failed/empty lookup here should just leave the "New
    product name" field blank for the user to type themselves, not break
    the scan flow."""
    barcode = barcode.strip()

    # Open Food Facts' API rejects requests with a generic/default User-Agent
    # (403, non-JSON body) -- confirmed live: curl worked, Python's requests
    # with no headers didn't. Their usage policy requires a descriptive UA
    # identifying the app, not just any string.
    OFF_HEADERS = {"User-Agent": "KitchenHub/1.0 (personal home dashboard; not for redistribution)"}

    try:
        r = requests.get(
            f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
            params={"fields": "product_name,product_name_en,brands"},
            headers=OFF_HEADERS,
            timeout=6,
        )
        data = r.json()
        if data.get("status") == 1:
            product = data.get("product") or {}
            name = product.get("product_name_en") or product.get("product_name")
            brand = product.get("brands")
            if name:
                name = name.strip()
                if brand and brand.strip().lower() not in name.lower():
                    name = f"{brand.strip()} {name}"
                return jsonify(success=True, found=True, name=name, source="openfoodfacts")
    except requests.RequestException:
        pass

    # Fall back to UPCitemdb's free trial endpoint -- broader general-retail
    # coverage (alcohol, household goods) than the food-focused Open Food
    # Facts, but rate-limited (100 lookups/day, no key), so it's the second
    # try, not the first.
    try:
        r = requests.get(
            "https://api.upcitemdb.com/prod/trial/lookup",
            params={"upc": barcode}, timeout=6,
        )
        data = r.json()
        items = data.get("items") or []
        if items and items[0].get("title"):
            return jsonify(success=True, found=True, name=items[0]["title"].strip(), source="upcitemdb")
    except requests.RequestException:
        pass

    return jsonify(success=True, found=False)


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
            # A brand-new product just linked via /register (or any product Grocy currently
            # has at 0) has nothing to consume yet -- Grocy's own consume endpoint rejects
            # that with a 400, which is correct behavior on Grocy's part, but not what a
            # physical scan means here: the item is in the scanner's hand right now, so it
            # existed a moment ago. Add 1 first so the consume that follows actually has
            # stock to draw down (net effect: stays at 0, but both a purchase and a
            # consumption transaction land in Grocy's history instead of a hard error).
            if float(product.get("stock_amount") or 0) < 1:
                r0 = requests.post(
                    f"{GROCY_URL}/api/stock/products/{product_id}/add",
                    headers=HEADERS,
                    json={"amount": 1, "transaction_type": "purchase"},
                    verify=False, timeout=10,
                )
                r0.raise_for_status()
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
