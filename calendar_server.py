from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from flask import Flask, jsonify, request
import datetime
import json
import requests
import subprocess

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_FILE = '/home/kitchenpi/calendar_token.json'
FLIPP_POSTAL = "YOUR_POSTAL_CODE"  # e.g. "L6P1A1" -- used for local flyer deal lookups

def get_calendar_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

@app.route('/events')
def get_events():
    try:
        service = get_calendar_service()
        now = datetime.datetime.utcnow()
        start = now.replace(hour=0, minute=0, second=0).isoformat() + 'Z'
        end = now.replace(hour=23, minute=59, second=59).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=start,
            timeMax=end,
            maxResults=10,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        result = []
        for event in events:
            start_time = event['start'].get('dateTime', event['start'].get('date', ''))
            if 'T' in start_time:
                dt = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                time_str = dt.strftime('%H:%M')
            else:
                time_str = 'All day'
            result.append({
                'time': time_str,
                'title': event.get('summary', 'Untitled'),
                'location': event.get('location', '')
            })

        return jsonify({"events": result})
    except Exception as e:
        return jsonify({"error": str(e), "events": []})

@app.route('/flipp')
def flipp_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"items": []})
    try:
        url = f"https://backflipp.wishabi.com/flipp/items/search?locale=en-ca&postal_code={FLIPP_POSTAL}&q={query}"
        res = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36'
        }, timeout=10)
        data = res.json()
        items = data.get('items', [])
        flyer_items = [i for i in items if i.get('item_type') == 'flyer' and i.get('current_price')]
        flyer_items.sort(key=lambda x: x.get('current_price', 999))
        result = [{
            'name': i.get('name', ''),
            'price': i.get('current_price'),
            'original_price': i.get('original_price'),
            'merchant': i.get('merchant_name', ''),
            'valid_to': i.get('valid_to', ''),
            'sale_story': i.get('sale_story', '')
        } for i in flyer_items[:5]]
        return jsonify({"items": result})
    except Exception as e:
        return jsonify({"error": str(e), "items": []})

@app.route('/temp')
def get_temp():
    try:
        result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=5)
        raw = result.stdout.strip()  # e.g. "temp=76.3'C"
        value = float(raw.split('=')[1].split("'")[0])
        return jsonify({"temp": value})
    except Exception as e:
        return jsonify({"error": str(e), "temp": None})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9200)
