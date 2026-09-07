#!/usr/bin/env python3
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
import subprocess
import requests
import time
import os
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

GROCY_URL = "https://localhost"
GROCY_KEY = "YOUR_GROCY_API_KEY"
NIIMPRINTX_DIR = "/home/kitchenpi/NiimPrintX"

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


def do_print(img, quantity=1):
    ts = int(time.time() * 1000)
    path = f"/tmp/label_{ts}.png"
    img.save(path)
    try:
        result = subprocess.run(
            ["python3", "-m", "NiimPrintX.cli", "print", "-m", "b1", "-i", path, "-n", str(quantity)],
            cwd=NIIMPRINTX_DIR,
            capture_output=True, text=True, timeout=90,
        )
        output = (result.stdout or "") + (result.stderr or "")
        ok = "print job completed" in output.lower()
        return ok, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the printer (is it awake?)"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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
    ok, log = do_print(img, quantity=quantity)
    return jsonify(success=ok, log=log)


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
    ok, log = do_print(img, quantity=quantity)
    return jsonify(success=ok, log=log)


@app.route('/status')
def status():
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9300)
