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

# LABEL_W was wrong from the start -- 240 assumed a 30mm-wide label, but a
# real printed frame test measured against the actual physical label (border
# vs. label edges, both photographed and pixel-measured) showed the true
# usable width is ~2.76x that, ~664px -- LABEL_H=400 was already correct
# (the same test's vertical border sat within 1px of the label's real top/
# bottom edges). No amount of font-size tuning inside the old 240px canvas
# could ever have used the label's real width, since the canvas itself was
# only ever a third as wide as the physical print area.
LABEL_W, LABEL_H = 640, 394
MARGIN = 20
# Text rendered dead-center in the canvas printed visibly right-of-center on
# the physical label (confirmed by direct printed test, not assumed) --
# likely a small registration offset between this print head and where the
# die-cut label's own left edge actually sits. Software can't correct the
# printer's physical alignment, so nudge our own centering left to
# compensate empirically.
HORIZONTAL_SHIFT = -16


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


# Point sizes to try when auto-fitting content to the label, largest first.
# Fine-grained (every point, not a handful of fixed tiers) because the tier
# gaps matter: the largest size that achieves a given line count is often
# between two coarse tiers, and picking a tier below it wastes size for no
# reason.
# 72 was an incidental ceiling from when LABEL_W=240 made width the binding
# constraint almost every time -- with the corrected, much wider canvas,
# max_h (unchanged) is what should limit size now, so raise the range's top
# well above anything reachable and let _best_fit's own max_w/max_h checks
# do the limiting.
FONT_SIZE_RANGE = range(200, 15, -1)


def _ink_size(draw, text, font):
    """Actual rendered pixel size of text, relative to a (0,0) draw origin --
    unlike textlength()'s advance width, this reflects real glyph extent
    (bold/hinting overhang included), which is what determines whether text
    actually gets clipped by the canvas edge. The gap between the two is only
    a pixel or two, which was invisible back when fonts topped out at 72pt
    inside a much narrower canvas; at the larger sizes now reachable in the
    wider corrected canvas, that same pixel or two is enough to center text
    slightly past the canvas edge and clip the last glyph."""
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return (r - l), (b - t)


# Real physical prints of large bold text clipped at the right edge even
# though textbbox()-measured ink sat 20+px inside max_w with room to spare --
# confirmed via direct printed tests, not assumed. Large solid-fill bold
# glyphs apparently spread further under this thermal print head than the
# antialiased software rendering predicts (heat/dot-bleed proportional to how
# much of the head is firing at once, which software measurement has no way
# to model). A flat pixel buffer isn't enough since the gap scales with how
# big/bold the text is, so fit-checks require a percentage of max_w free
# rather than trusting the measured ink width right up to the limit.
WIDTH_FIT_SAFETY = 0.90


def _lines_fit(draw, lines, font, max_w):
    return all(_ink_size(draw, t, font)[0] <= max_w * WIDTH_FIT_SAFETY for t in lines)


def _block_height(draw, text, font):
    # Breathing room between stacked lines, scaled with size rather than a
    # flat pixel constant -- a flat +12px was fine when fonts topped out at
    # 72pt, but became a rounding error at the much larger sizes now
    # reachable, letting stacked blocks drift past the canvas bottom.
    return _ink_size(draw, text, font)[1] + max(6, font.size // 8)


def _best_fit(draw, texts, font_path, max_w, max_h):
    """Largest size whose wrapped lines all fit max_w and whose total height
    fits max_h, using each size's own natural word-wrap line count.

    Tried preferring the fewest wrapped lines above all (biasing toward wider
    lines when a smaller size could still fit everything on fewer of them),
    but that's an overcorrection: for a title long enough to need 2-3 lines
    at any legible size, "fewest lines" is satisfied only by shrinking all
    the way down to whatever tiny size crams it onto one line -- e.g. a
    3-word title forced to ~17pt just to avoid a second line, when it reads
    perfectly fine as 3 short lines at 45pt. Plain "largest that fits" doesn't
    have that failure mode and still avoids the original clipping bug via the
    width check below."""
    candidates = []
    for candidate in FONT_SIZE_RANGE:
        font = ImageFont.truetype(font_path, candidate)
        wrapped = []
        for text in texts:
            wrapped.extend(_wrap(draw, text, font, max_w))
        if not _lines_fit(draw, wrapped, font, max_w):
            continue
        heights = [_block_height(draw, t, font) for t in wrapped]
        if sum(heights) <= max_h:
            candidates.append((len(wrapped), candidate, wrapped, heights))

    if not candidates:
        font = ImageFont.truetype(font_path, FONT_SIZE_RANGE[-1])
        lines = []
        for text in texts:
            lines.extend(_wrap(draw, text, font, max_w))
        heights = [_block_height(draw, t, font) for t in lines]
        return lines, font, heights

    best = max(candidates, key=lambda c: c[1])
    _, size, lines, heights = best
    font = ImageFont.truetype(font_path, size)
    return lines, font, heights


def _grow_lines_independently(draw, lines, base_size, font_path, max_w, slack):
    """_best_fit picks one shared size for a whole block, sized to the
    *widest* line -- a short line on its own row (e.g. "salad" next to
    "hicken"/"ceasar") ends up with unused width on both sides purely
    because it's shorter, not because the block is mis-sized. Let each line
    grow independently past the shared base size as far as it individually
    fits max_w, spending from the block's own unused height (`slack`) as it
    goes -- bounded on both axes, so growth can't run away the way "prefer
    fewest lines" or hard-breaking a word did in earlier attempts."""
    results = []
    for text in lines:
        base_font = ImageFont.truetype(font_path, base_size)
        base_h = _block_height(draw, text, base_font)
        chosen_size, chosen_h = base_size, base_h
        for size in range(base_size + 1, FONT_SIZE_RANGE[0] + 1):
            font = ImageFont.truetype(font_path, size)
            if _ink_size(draw, text, font)[0] > max_w * WIDTH_FIT_SAFETY:
                break
            h = _block_height(draw, text, font)
            if h - base_h > slack:
                break
            chosen_size, chosen_h = size, h
        slack -= (chosen_h - base_h)
        results.append((text, ImageFont.truetype(font_path, chosen_size), chosen_h))
    return results, slack


def render_label(title, subtitle=None, extra_lines=None):
    img = Image.new("L", (LABEL_W, LABEL_H), 255)
    draw = ImageDraw.Draw(img)
    # A small fixed safety margin absorbs sub-pixel rounding between what
    # textbbox() measures and what the rasterizer actually draws -- without
    # it, growing text right up to the theoretical limit occasionally clips
    # a glyph by a pixel or two at the sizes now reachable in the wider
    # corrected canvas (invisible at the old canvas's much smaller sizes).
    EDGE_SAFETY = 4
    # A real printed label (short title -> subtitle claimed most of the
    # remaining height -> its last line clipped at the bottom edge) showed
    # the vertical dimension has the same gap between theoretical pixel math
    # and physical output that width already needed a percentage margin
    # for -- growth routinely maximizes to use ~100% of max_h, which is
    # exactly the scenario that leaves no room for that gap. A flat few-px
    # buffer isn't enough for the same reason it wasn't enough for width.
    HEIGHT_FIT_SAFETY = 0.92
    max_h = (LABEL_H - MARGIN * 2 - EDGE_SAFETY) * HEIGHT_FIT_SAFETY
    max_w = LABEL_W - MARGIN * 2 - EDGE_SAFETY

    sub_source = list(extra_lines or [])
    if subtitle:
        sub_source.append(subtitle)

    # Title and subtitle are sized independently rather than one scaling the
    # other by a fixed ratio -- a single long, unsplittable word in the title
    # (e.g. "Japanese") can cap how large the title is allowed to be, but
    # that shouldn't also force short subtitle text down to a tiny size when
    # the subtitle would happily fit much larger on its own. But the title
    # can't be allowed to claim *all* of max_h either: the much wider
    # corrected canvas lets even a short title reach sizes that alone fill
    # the whole height, pushing the subtitle off the canvas entirely (this
    # is what was silently dropping "Prepared on ..." altogether). Cap the
    # title's own search to a share of the height and guarantee the rest --
    # if the title ends up needing less, remaining_h below still hands the
    # subtitle whatever's actually left over, not just its guaranteed share.
    title_max_h = int(max_h * 0.68) if sub_source else max_h
    title_lines, title_font, title_heights = _best_fit(draw, [title], FONT_BOLD, max_w, title_max_h)
    title_slack = title_max_h - sum(title_heights)
    title_blocks, title_slack = _grow_lines_independently(
        draw, title_lines, title_font.size, FONT_BOLD, max_w, title_slack)

    if sub_source:
        remaining_h = max(0, max_h - sum(h for _, _, h in title_blocks))
        sub_lines, sub_font, sub_heights = _best_fit(draw, sub_source, FONT_REGULAR, max_w, remaining_h)
        sub_slack = remaining_h - sum(sub_heights)
        sub_blocks, sub_slack = _grow_lines_independently(
            draw, sub_lines, sub_font.size, FONT_REGULAR, max_w, sub_slack)
    else:
        sub_blocks = []

    blocks = [(t, f) for t, f, h in title_blocks] + [(t, f) for t, f, h in sub_blocks]
    heights = [h for _, _, h in title_blocks] + [h for _, _, h in sub_blocks]

    total_h = sum(heights)
    y = max(MARGIN, (LABEL_H - total_h) // 2)
    for (text, font), h in zip(blocks, heights):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        w = r - l
        x = max(MARGIN, (LABEL_W - w) // 2) + HORIZONTAL_SHIFT
        # Compensate for the font's own left/top bearing so the actual ink
        # starts exactly at (x, y) -- otherwise stacked blocks drift
        # downward (and centering drifts sideways) by however much bearing
        # each font/string combination happens to have. That drift is what
        # was pushing the last title line's bottom past the canvas edge and
        # shoving the subtitle off-canvas entirely once sizes got large.
        draw.text((x - l, y - t), text, font=font, fill=0)
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


async def _manual_connect():
    global _printer
    async with _printer_lock:
        if _is_connected(_printer):
            return True
        try:
            _printer = await _connect_with_retry(None)
            return True
        except Exception:
            _printer = None
            return False


@app.route('/printer/connect', methods=['POST'])
def printer_connect():
    future = asyncio.run_coroutine_threadsafe(_manual_connect(), _loop)
    try:
        connected = future.result(timeout=30)
    except Exception:
        connected = False
    return jsonify(connected=connected)


@app.route('/status')
def status():
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9300, threaded=True)
