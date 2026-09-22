#!/usr/bin/env python3
from flask import Flask, jsonify, request
import json
import os
import time
import threading

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guest_requests.json")
_lock = threading.Lock()


def _load():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError:
            return []


def _save(requests_list):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(requests_list, f, indent=2)


@app.route("/status")
def status():
    return jsonify(ok=True)


@app.route("/list")
def list_requests():
    return jsonify(success=True, requests=_load())


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    recipe_name = (data.get("recipe_name") or "").strip() or None
    note = (data.get("note") or "").strip() or None
    if not name:
        return jsonify(success=False, error="name is required"), 400
    if not recipe_name and not note:
        return jsonify(success=False, error="recipe_name or note is required"), 400
    with _lock:
        reqs = _load()
        new_id = max((r["id"] for r in reqs), default=0) + 1
        reqs.append({
            "id": new_id,
            "name": name,
            "recipe_name": recipe_name,
            "note": note,
            "timestamp": time.time(),
            "status": "pending",
        })
        _save(reqs)
    return jsonify(success=True, id=new_id)


@app.route("/dismiss/<int:req_id>", methods=["POST"])
def dismiss(req_id):
    with _lock:
        reqs = _load()
        found = False
        for r in reqs:
            if r["id"] == req_id:
                r["status"] = "done"
                found = True
                break
        if not found:
            return jsonify(success=False, error="request not found"), 404
        _save(reqs)
    return jsonify(success=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9600)
