"""Drive the kiosk's Chromium over DevTools (port 9222, localhost only) through a list of steps.

  python3 cdp_steps.py steps.json

Steps: ["js", expr] prints a value; ["tap", expr-returning-element] sends a real touch tap at
its centre; ["wait", expr, seconds] polls until truthy; ["sleep", s]; ["shot", path] saves a JPEG.
"""
import base64
import json
import sys
import time
import urllib.request

import websocket


class CDP:
    def __init__(self):
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
        page = next(t for t in tabs if t.get("type") == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error in {expr!r}: {r['exceptionDetails'].get('exception', {}).get('description')}")
        return r["result"].get("value")

    def tap(self, element_expr):
        x, y = self.js(f"(() => {{ const el = {element_expr}; const r = el.getBoundingClientRect();"
                       f" return [r.x + r.width / 2, r.y + r.height / 2]; }})()")
        point = [{"x": x, "y": y, "radiusX": 4, "radiusY": 4, "force": 1, "id": 1}]
        self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=point)
        time.sleep(0.08)
        self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        return [round(x), round(y)]


def main(path):
    cdp = CDP()
    for step in json.load(open(path, encoding="utf-8")):
        kind = step[0]
        if kind == "js":
            print("js  ", json.dumps(cdp.js(step[1]), ensure_ascii=False))
        elif kind == "tap":
            print("tap ", step[1][:60], "at", cdp.tap(step[1]))
        elif kind == "wait":
            end = time.time() + step[2]
            while not cdp.js(step[1]):
                if time.time() > end:
                    print("wait", "TIMED OUT:", step[1][:60])
                    break
                time.sleep(0.5)
            else:
                print("wait", "ok:", step[1][:60])
        elif kind == "sleep":
            time.sleep(step[1])
        elif kind == "shot":
            data = cdp.call("Page.captureScreenshot", format="jpeg", quality=70)["data"]
            open(step[1], "wb").write(base64.b64decode(data))
            print("shot", step[1])


if __name__ == "__main__":
    main(sys.argv[1])
