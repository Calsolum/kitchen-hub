#!/usr/bin/env python3
import sys
import os
import io
import base64
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


# Largest-first title sizes to try when auto-fitting content to the label;
# subtitle/extra lines scale proportionally. A short label (most of them --
# an item name and a date) should fill the sticker, not sit in the middle of
# a sea of white space at a fixed small size.
FONT_TIERS = [64, 56, 48, 42, 36, 30]
SUBTITLE_RATIO = 0.6


def _label_blocks(draw, title, subtitle, extra_lines, title_size):
    font_title = ImageFont.truetype(FONT_BOLD, title_size)
    font_sub = ImageFont.truetype(FONT_REGULAR, max(16, int(title_size * SUBTITLE_RATIO)))
    max_w = LABEL_W - MARGIN * 2

    blocks = [(line, font_title) for line in _wrap(draw, title, font_title, max_w)[:3]]
    for line in (extra_lines or []):
        blocks += [(w, font_sub) for w in _wrap(draw, line, font_sub, max_w)]
    if subtitle:
        blocks += [(w, font_sub) for w in _wrap(draw, subtitle, font_sub, max_w)]

    heights = [draw.textbbox((0, 0), t, font=f)[3] - draw.textbbox((0, 0), t, font=f)[1] + 12 for t, f in blocks]
    return blocks, heights


def render_label(title, subtitle=None, extra_lines=None):
    img = Image.new("L", (LABEL_W, LABEL_H), 255)
    draw = ImageDraw.Draw(img)
    max_h = LABEL_H - MARGIN * 2
    max_w = LABEL_W - MARGIN * 2

    blocks, heights = _label_blocks(draw, title, subtitle, extra_lines, FONT_TIERS[-1])
    for size in FONT_TIERS:
        candidate_blocks, candidate_heights = _label_blocks(draw, title, subtitle, extra_lines, size)
        # _wrap() only breaks on word boundaries, so a single long word (e.g.
        # "Japanese") can still come back wider than the label at a large
        # size -- checking total height alone isn't enough, every line's
        # actual rendered width has to fit too, or it just runs off the edge.
        fits_width = all(draw.textlength(t, font=f) <= max_w for t, f in candidate_blocks)
        if sum(candidate_heights) <= max_h and fits_width:
            blocks, heights = candidate_blocks, candidate_heights
            break

    total_h = sum(heights)
    y = max(MARGIN, (LABEL_H - total_h) // 2)
    for (text, font), h in zip(blocks, heights):
        w = draw.textlength(text, font=font)
        x = max(MARGIN, (LABEL_W - w) // 2)
        draw.text((x, y), text, font=font, fill=0)
        y += h

    return img


# --- Print jobs run against NiimPrintX's own asyncio API (rather than
# shelling out to its CLI) on one persistent background event loop, so a
# POST /print/* returns immediately with a job id and the dashboard can poll
# /print/status/<id> for real phase + per-row progress instead of just
# waiting on one long request.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


NIIMBOT_ADDR_CACHE = os.path.expanduser("~/.niimbot_last_address")

# A BLE connection to this printer takes a few seconds to establish (scan +
# handshake). The printer stays connected -- and awake -- for as long as a
# central holds the connection open, so keeping this one alive across prints
# means only the FIRST print after a wake needs the full connect; back-to-back
# prints reuse it and skip straight to printing. Everything printer-related
# runs on this single dedicated loop so the same PrinterClient/BleakClient
# object can safely be reused between requests that arrive on different
# Flask worker threads.
_printer = None
_printer_lock = asyncio.Lock()
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


async def _connect_with_retry(job_id, max_attempts=3):
    # Right after a Pi reboot, BlueZ's D-Bus service can be slow to fully
    # settle -- the very first connect attempt can time out even though the
    # printer was found fine, and a retry moments later just works. Clear the
    # cached address on failure too, in case a stale cached address (rather
    # than boot timing) is what's actually wrong.
    last_exc = RuntimeError("Failed to connect to the printer")
    for attempt in range(1, max_attempts + 1):
        try:
            _set_job(job_id, phase="scanning")
            device = await find_device("b1")
            _set_job(job_id, phase="connecting")
            printer = PrinterClient(device)
            if await printer.connect():
                return printer
        except Exception as e:
            last_exc = e
        try:
            os.remove(NIIMBOT_ADDR_CACHE)
        except OSError:
            pass
        if attempt < max_attempts:
            await asyncio.sleep(2)
    raise last_exc


def _is_connected(printer):
    try:
        return bool(printer and printer.transport.client and printer.transport.client.is_connected)
    except Exception:
        return False


def _is_cancelled(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancelled"))


async def _run_print(job_id, img, quantity, _retried=False):
    global _printer
    # A job can only be cancelled while it's still queued -- once it's
    # actually talking to the printer there's no clean way to abort a
    # physical print already underway, so the checks below are the only
    # two points where "cancelled" can still take effect.
    if _is_cancelled(job_id):
        _set_job(job_id, phase="cancelled", done=True, success=False, error="Cancelled")
        return
    async with _printer_lock:
        if _is_cancelled(job_id):
            _set_job(job_id, phase="cancelled", done=True, success=False, error="Cancelled")
            return
        try:
            if not _is_connected(_printer):
                _printer = await _connect_with_retry(job_id)
            printer = _printer
            _set_job(job_id, phase="printing", current=0, total=img.height)

            def progress_cb(current, total):
                _set_job(job_id, current=current, total=total)

            await printer.print_image(img, quantity=quantity, progress_cb=progress_cb)
            _set_job(job_id, phase="done", done=True, success=True)
            return
        except Exception as e:
            # The cached connection may have looked alive but wasn't (the
            # printer can drop a connection without us noticing right away)
            # -- one automatic retry with a forced fresh connect covers that
            # transparently before actually failing the job.
            _printer = None
            if _retried:
                detail = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (no message)\n{traceback.format_exc()}"
                _set_job(job_id, phase="failed", done=True, success=False, error=detail)
                return
    await _run_print(job_id, img, quantity, _retried=True)


def start_print_job(img, quantity):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "phase": "queued", "current": 0, "total": 0,
            "done": False, "success": None, "error": None, "cancelled": False,
        }
    asyncio.run_coroutine_threadsafe(_run_print(job_id, img, quantity), _loop)
    return job_id


@app.route('/preview', methods=['POST'])
def preview():
    data = request.get_json(force=True, silent=True) or {}
    lines = [str(l).strip() for l in data.get('lines', []) if str(l).strip()]
    if not lines:
        return jsonify(error="No text provided"), 400
    img = render_label(lines[0], extra_lines=lines[1:])
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return jsonify(image=f"data:image/png;base64,{b64}")


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


@app.route('/print/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="unknown job"), 404
        if job["done"]:
            return jsonify(success=False, error="Job already finished"), 400
        job["cancelled"] = True
    return jsonify(success=True)


@app.route('/printer/status')
def printer_status():
    return jsonify(connected=_is_connected(_printer))


@app.route('/status')
def status():
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9300, threaded=True)
