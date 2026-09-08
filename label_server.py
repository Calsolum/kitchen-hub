#!/usr/bin/env python3
import sys
import os
import time
import uuid
import asyncio
import threading
import traceback

from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
import requests
import urllib3

NIIMPRINTX_DIR = "/home/kitchenpi/NiimPrintX"
sys.path.insert(0, NIIMPRINTX_DIR)
from NiimPrintX.nimmy.bluetooth import find_device
from NiimPrintX.nimmy.printer import PrinterClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

GROCY_URL = "https://localhost"
GROCY_KEY = "YOUR_GROCY_API_KEY"

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

LABEL_W, LABEL_H = 240, 400
MARGIN = 20


def _wrap(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if not cur or draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_label(title, subtitle=None, extra_lines=None):
    img = Image.new("L", (LABEL_W, LABEL_H), 255)
    draw = ImageDraw.Draw(img)
    font_title = ImageFont.truetype(FONT_BOLD, 30)
    font_sub = ImageFont.truetype(FONT_REGULAR, 22)
    max_w = LABEL_W - MARGIN * 2

    blocks = [(line, font_title) for line in _wrap(draw, title, font_title, max_w)[:3]]
    for line in (extra_lines or []):
        blocks += [(w, font_sub) for w in _wrap(draw, line, font_sub, max_w)]
    if subtitle:
        blocks += [(w, font_sub) for w in _wrap(draw, subtitle, font_sub, max_w)]

    heights = []
    for text, font in blocks:
        bbox = draw.textbbox((0, 0), text, font=font)
        heights.append(bbox[3] - bbox[1] + 12)

    total_h = sum(heights)
    y = max(MARGIN, (LABEL_H - total_h) // 2)
    for (text, font), h in zip(blocks, heights):
        w = draw.textlength(text, font=font)
        x = max(MARGIN, (LABEL_W - w) // 2)
        draw.text((x, y), text, font=font, fill=0)
        y += h

    return img


# --- Print jobs run in a background thread against NiimPrintX's own asyncio
# API (rather than shelling out to its CLI), so a POST /print/* returns
# immediately with a job id and the dashboard can poll /print/status/<id> for
# real phase + per-row progress instead of just waiting on one long request.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


async def _run_print(job_id, img, quantity):
    printer = None
    try:
        _set_job(job_id, phase="scanning")
        device = await find_device("b1")
        _set_job(job_id, phase="connecting")
        printer = PrinterClient(device)
        if not await printer.connect():
            raise RuntimeError("Failed to connect to the printer")
        _set_job(job_id, phase="printing", current=0, total=img.height)

        def progress_cb(current, total):
            _set_job(job_id, current=current, total=total)

        await printer.print_image(img, quantity=quantity, progress_cb=progress_cb)
        await printer.disconnect()
        _set_job(job_id, phase="done", done=True, success=True)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (no message)\n{traceback.format_exc()}"
        _set_job(job_id, phase="failed", done=True, success=False, error=detail)
        if printer is not None:
            try:
                await printer.disconnect()
            except Exception:
                pass


def start_print_job(img, quantity):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "phase": "queued", "current": 0, "total": 0,
            "done": False, "success": None, "error": None,
        }

    def runner():
        asyncio.run(_run_print(job_id, img, quantity))

    threading.Thread(target=runner, daemon=True).start()
    return job_id


@app.route('/print/text', methods=['POST'])
def print_text():
    data = request.get_json(force=True, silent=True) or {}
    lines = [str(l).strip() for l in data.get('lines', []) if str(l).strip()]
    quantity = max(1, min(10, int(data.get('quantity', 1))))
    if not lines:
        return jsonify(success=False, error="No text provided"), 400
    if len(lines) > 4:
        return jsonify(success=False, error="Max 4 lines"), 400
    img = render_label(lines[0], extra_lines=lines[1:])
    job_id = start_print_job(img, quantity)
    return jsonify(job_id=job_id)


@app.route('/print/product/<int:product_id>', methods=['POST'])
def print_product(product_id):
    data = request.get_json(silent=True) or {}
    quantity = max(1, min(10, int(data.get('quantity', 1))))
    headers = {'GROCY-API-KEY': GROCY_KEY}
    try:
        product = requests.get(f"{GROCY_URL}/api/objects/products/{product_id}", headers=headers, timeout=10, verify=False).json()
        name = product.get('name') or 'Unknown'
        entries = requests.get(f"{GROCY_URL}/api/stock/products/{product_id}/entries", headers=headers, timeout=10, verify=False).json()
        dates = [e['best_before_date'] for e in entries if e.get('best_before_date') and e['best_before_date'] < '2999-01-01']
        best_before = min(dates) if dates else None
    except Exception as e:
        return jsonify(success=False, error=f"Grocy lookup failed: {e}"), 502

    subtitle = f"Exp: {best_before}" if best_before else None
    img = render_label(name, subtitle=subtitle)
    job_id = start_print_job(img, quantity)
    return jsonify(job_id=job_id)


@app.route('/print/status/<job_id>')
def print_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    return jsonify(job)


@app.route('/status')
def status():
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9300, threaded=True)
