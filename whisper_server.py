from faster_whisper import WhisperModel
from flask import Flask, request, jsonify
import tempfile
import os
import re
import requests
import json
from datetime import datetime

app = Flask(__name__)

# whisper and grocy are containers on the same docker-compose network, so the service name
# resolves correctly regardless of the host's LAN IP -- never hardcode a raw IP here.
GROCY_URL = os.environ.get("GROCY_URL", "http://grocy:80")
GROCY_KEY = os.environ["GROCY_API_KEY"]
HEADERS = {"GROCY-API-KEY": GROCY_KEY}
HISTORY_FILE = "/app/voice_history.json"
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")  # optional -- voice YouTube search is disabled without it

print("Loading Whisper model...")
model = WhisperModel("small.en", device="cpu", compute_type="int8")
print("Model loaded!")

WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'a': 1, 'an': 1, 'the': 1, 'half': 1
}

def log_command(transcript, result):
    try:
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "transcript": transcript,
            "result": result
        })
        history = history[-50:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"Log error: {e}")

def words_to_number(text):
    for word, num in WORD_NUMBERS.items():
        text = re.sub(r'\b' + word + r'\b', str(num), text)
    return text

def get_products():
    try:
        r = requests.get(f"{GROCY_URL}/api/objects/products", headers=HEADERS)
        return r.json()
    except:
        return []

def find_product(name, products):
    name = name.lower().strip()
    name = re.sub(r'[^\w\s]', '', name).strip()
    name = re.sub(r'\s*(up|it|them|all|the last|of it|of them)$', '', name).strip()

    for p in products:
        if p['name'].lower() == name:
            return p

    variations = [name]
    if name.endswith('s'):
        variations.append(name[:-1])
    else:
        variations.append(name + 's')
    if name.endswith('es'):
        variations.append(name[:-2])

    for variation in variations:
        for p in products:
            if p['name'].lower() == variation:
                return p

    best = None
    best_score = 0
    for p in products:
        pname = p['name'].lower()
        for variation in variations:
            if pname in variation or variation in pname:
                score = len(pname)
                if score > best_score:
                    best_score = score
                    best = p
    return best

def youtube_search(query):
    """Top YouTube search result for a spoken query. Returns None on any failure
    (missing/invalid key, no results, network error) so callers can fall back cleanly."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "q": query, "type": "video", "maxResults": 1, "key": YOUTUBE_API_KEY},
            timeout=8
        )
        items = r.json().get("items", [])
        if not items:
            return None
        item = items[0]
        return {"id": item["id"]["videoId"], "title": item["snippet"]["title"]}
    except Exception as e:
        print(f"YouTube search error: {e}")
        return None

def parse_command(text):
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', ' ', text).strip()

    # Checked before word-to-number conversion / punctuation-agnostic patterns below,
    # since "play two chainz on youtube" etc. should keep the number as spoken, not be
    # mangled by the grocery-command number parsing meant for quantities.
    youtube_patterns = [
        r"(?:search )?youtube for (.+)",
        r"play (.+) on youtube",
        r"find (.+) on youtube",
        r"(?:play|watch) (.+) video",
    ]
    for pattern in youtube_patterns:
        match = re.search(pattern, text)
        if match:
            query = match.group(1).strip()
            video = youtube_search(query)
            if video:
                return {"result": f"Playing: {video['title']}", "action": "play_youtube",
                        "video_id": video["id"], "title": video["title"]}
            return {"result": f"Could not find a YouTube video for: {query}"}

    text = words_to_number(text)
    products = get_products()

    consume_patterns = [
        r"(?:i )?used? (\d+)?\s*(.+)",
        r"(?:i )?ate? (\d+)?\s*(.+)",
        r"(?:i )?finished? (\d+)?\s*(.+)",
        r"remove (\d+)?\s*(.+)",
        r"consumed? (\d+)?\s*(.+)",
        r"(?:i )?cooked? (?:with )?(\d+)?\s*(.+)",
    ]

    shopping_patterns = [
        r"add (.+) to (?:the )?shopping(?: list)?",
        r"(?:i )?need (?:more )?(.+)",
        r"buy (?:more )?(.+)",
        r"(?:i(?:'m| am) )?out of (.+)",
        r"(?:we(?:'re| are) )?out of (.+)",
        r"(?:i(?:'m| am) )?running low on (.+)",
    ]

    for pattern in consume_patterns:
        match = re.search(pattern, text)
        if match:
            amount = int(match.group(1)) if match.group(1) else 1
            product_name = match.group(2).strip()
            product = find_product(product_name, products)
            if product:
                try:
                    r = requests.post(
                        f"{GROCY_URL}/api/stock/products/{product['id']}/consume",
                        headers={**HEADERS, "Content-Type": "application/json"},
                        json={"amount": amount, "transaction_type": "consume", "allow_substock_change": True}
                    )
                    if r.status_code == 200:
                        return {"result": f"Used {amount} {product['name']}"}
                    else:
                        return {"result": f"Could not update {product['name']} - check stock level"}
                except Exception as e:
                    return {"result": f"Error: {str(e)}"}
            else:
                return {"result": f"Product not found: {product_name}"}

    for pattern in shopping_patterns:
        match = re.search(pattern, text)
        if match:
            product_name = match.group(1).strip()
            product = find_product(product_name, products)
            if product:
                try:
                    r = requests.post(
                        f"{GROCY_URL}/api/objects/shopping_list",
                        headers={**HEADERS, "Content-Type": "application/json"},
                        json={"product_id": product['id'], "amount": 1, "shopping_list_id": 1}
                    )
                    if r.status_code == 200:
                        return {"result": f"Added {product['name']} to shopping list"}
                    else:
                        return {"result": f"Could not add {product['name']}"}
                except Exception as e:
                    return {"result": f"Error: {str(e)}"}
            else:
                return {"result": f"Product not found: {product_name}"}

    return {"result": f"Command not understood: {text}"}

@app.route('/transcribe', methods=['POST'])
def transcribe():
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file"}), 400
    audio_file = request.files['audio']
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name
    try:
        segments, info = model.transcribe(tmp_path, language="en")
        text = " ".join([s.text for s in segments]).strip()
        parsed = parse_command(text)
        log_command(text, parsed["result"])  # history display expects a plain string
        response = {"transcript": text, "result": parsed["result"]}
        if "action" in parsed:
            response["action"] = parsed["action"]
            response["video_id"] = parsed.get("video_id")
        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)

@app.route('/history')
def history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                commands = json.load(f)
        else:
            commands = []
        return jsonify({"commands": commands})
    except:
        return jsonify({"commands": []})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9100)
