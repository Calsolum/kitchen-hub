#!/usr/bin/env python3
"""Restart / shut down the Pi from the kiosk.

Only the kiosk itself may use this. A shutdown cannot be undone remotely (someone has to
press the Pi's power button or replug it), so a phone, a guest on the Wi-Fi or a tailnet
device is refused -- they never even get the buttons (the dashboard asks /status first).

Who is the kiosk: the kiosk's browser talks to nginx on the Pi's own address, so nginx
sees the client as one of the Pi's own IPs. nginx passes that as X-Real-IP (it overwrites
whatever the client sent). We only believe X-Real-IP when the TCP peer is nginx's docker
network or loopback, so nobody can fake it by hitting this port directly from the LAN.
"""
import ipaddress
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request

app = Flask(__name__)

SYSTEMCTL = "/usr/bin/systemctl"
ACTIONS = ("reboot", "poweroff")
DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")   # nginx container -> host.docker.internal
DELAY_S = 2     # let the HTTP response reach the browser before the system goes down

_busy = threading.Lock()


def _is_local_address(ip):
    """True when `ip` is one of this machine's own addresses (we can bind a socket to it)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    s = socket.socket(socket.AF_INET6 if addr.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((str(addr), 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _client():
    """Return (ip, is_kiosk) for the current request."""
    try:
        peer = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return None, False
    if peer.is_loopback:
        ip = str(peer)                                  # a process on the Pi itself
    elif peer.version == 4 and peer in DOCKER_NET:
        ip = (request.headers.get("X-Real-IP") or "").strip()   # set by nginx
    else:
        return str(peer), False                         # straight from the LAN, not via nginx
    return ip or None, bool(ip) and _is_local_address(ip)


def _same_origin():
    origin = request.headers.get("Origin")
    return origin is None or urlparse(origin).netloc == request.host


def _can_sudo(action):
    """True when sudoers lets us run `systemctl <action>` without a password.

    (`sudo -l <cmd>` is no use here: kitchenpi can run anything *with* a password, and
    sudo lists that too -- so read the NOPASSWD entries instead.)"""
    r = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10)
    want = f"{SYSTEMCTL} {action}"
    for line in r.stdout.splitlines():
        if "NOPASSWD:" in line:
            if want in [c.strip() for c in line.split("NOPASSWD:", 1)[1].split(",")]:
                return True
    return False


def _run(action, ip):
    time.sleep(DELAY_S)
    print(f"power: running systemctl {action} (asked by {ip})", flush=True)
    r = subprocess.run(["sudo", "-n", SYSTEMCTL, action], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"power: systemctl {action} FAILED rc={r.returncode}: {r.stderr.strip()}", flush=True)
        _busy.release()     # still alive, so allow another attempt


@app.route("/status")
def status():
    ip, kiosk = _client()
    return jsonify(ok=True, allowed=kiosk)


@app.route("/action", methods=["POST"])
def action():
    ip, kiosk = _client()
    if not kiosk:
        print(f"power: refused request from {ip}", flush=True)
        return jsonify(success=False, error="only the kiosk can do this"), 403
    if request.headers.get("X-Kitchen-Power") != "1" or not _same_origin():
        print(f"power: refused cross-origin / unmarked request from {ip}", flush=True)
        return jsonify(success=False, error="refused"), 403
    data = request.get_json(force=True, silent=True) or {}
    act = data.get("action")
    if act not in ACTIONS:
        return jsonify(success=False, error="unknown action"), 400
    if not _can_sudo(act):
        print(f"power: sudo rule for {act} is missing", flush=True)
        return jsonify(success=False, error="not permitted on this Pi"), 500
    if not _busy.acquire(blocking=False):
        return jsonify(success=False, error="already in progress"), 409
    print(f"power: {act} requested by {ip}", flush=True)
    threading.Thread(target=_run, args=(act, ip), daemon=True).start()
    return jsonify(success=True, action=act)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9700)
