"""Kitchen Hub service for the Teamosa: settings, schedule and buttons for the dashboard.

Runs as kitchen-teamosa.service (`python3 -m teamosa.server`, port 9800) behind nginx at
/teamosa/. It owns the brew schedule (there is no systemd timer): at the chosen minute it
runs `python3 -m teamosa brew`, which does the pre-flight check, the single write, the
30-minute gap and the lock. Settings live in ~/teamosa/settings.json.

Who may use it: the kiosk and Tariq's devices on Tailscale. Anyone else on the Wi-Fi
(guests) gets 403 and never sees the Tea button. The client address comes from nginx's
X-Real-IP, believed only when the TCP peer is nginx's docker network or loopback -- the
same rule as power_server.py, so nobody can fake it by hitting this port directly.

The POST routes only act on a JSON body (Content-Type application/json), which a page on
another site can't send without a CORS preflight this service never answers.
"""
import ipaddress
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request

from . import protocol

log = logging.getLogger("teamosa.server")

PORT = 9800
DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")    # nginx container -> host.docker.internal
TAILNETS = (ipaddress.ip_network("100.64.0.0/10"),     # Tailscale's IPv4 range
            ipaddress.ip_network("fd7a:115c:a1e0::/48"))  # and its IPv6 range
PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # ~/teamosa, where `-m teamosa` resolves
DEFAULT_SETTINGS = {"enabled": False, "days": [0, 1, 2, 3, 4], "time": "06:45",
                    "temp": 60, "minutes": 3.0}
RUN_TIMEOUT_S = 300
SCHEDULE_LOCK_WAIT_S = 240   # a scheduled brew waits this long for a dashboard action to finish
EXIT_MESSAGES = {
    0: "Done.",
    3: "The brewer reported a problem (cup, water or lid), so nothing was sent.",
    4: "Couldn't reach the brewer over Bluetooth. Nothing was sent.",
    5: "The brew command failed partway. Check the brewer.",
    6: "A brew ran less than 30 minutes ago, so this one was refused.",
    7: "Another brew or check was already running.",
}


class SettingsError(ValueError):
    pass


def _now_iso(when):
    return when.isoformat(timespec="seconds")


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


def client_access(remote_addr, real_ip_header):
    """Return (ip, allowed, kiosk) for a request."""
    try:
        peer = ipaddress.ip_address(remote_addr or "")
    except ValueError:
        return None, False, False
    if peer.is_loopback:
        ip = str(peer)                                   # a process on the Pi itself
    elif peer.version == 4 and peer in DOCKER_NET:
        ip = (real_ip_header or "").strip()              # set by nginx
    else:
        ip = str(peer)                                   # straight to this port, not via nginx
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip or None, False, False
    kiosk = _is_local_address(ip)
    tailnet = any(addr.version == net.version and addr in net for net in TAILNETS)
    return ip, kiosk or tailnet, kiosk


def validate_settings(data):
    """Return normalised settings or raise SettingsError with a message for the dashboard."""
    if not isinstance(data, dict):
        raise SettingsError("expected a JSON object")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise SettingsError("enabled must be true or false")
    days = data.get("days")
    if (not isinstance(days, list)
            or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days)):
        raise SettingsError("days must be a list of 0 (Mon) .. 6 (Sun)")
    days = sorted(set(days))
    if enabled and not days:
        raise SettingsError("pick at least one day, or switch the schedule off")
    try:
        hh, mm = (int(part) for part in str(data.get("time")).split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        raise SettingsError("time must be HH:MM (24-hour)") from None
    temp, minutes = data.get("temp"), data.get("minutes")
    for value in (temp, minutes):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SettingsError("temperature and brew time must be numbers")
    try:
        protocol.encode_brew(temp, minutes)
    except protocol.ProtocolError as exc:
        raise SettingsError(str(exc)) from None
    return {"enabled": enabled, "days": days, "time": f"{hh:02d}:{mm:02d}",
            "temp": int(temp), "minutes": float(minutes)}


def fire_key(when):
    return when.strftime("%Y-%m-%d %H:%M")


def next_run(settings, now, last_fired=None):
    """The next scheduled minute that hasn't passed or already fired, or None."""
    if not settings["enabled"] or not settings["days"]:
        return None
    hh, mm = (int(part) for part in settings["time"].split(":"))
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in settings["days"]:
            continue
        when = datetime.combine(day, dtime(hh, mm))
        if when + timedelta(minutes=1) <= now or fire_key(when) == last_fired:
            continue
        return when
    return None


def due(settings, now, last_fired):
    """The scheduled minute to brew for right now, or None."""
    when = next_run(settings, now, last_fired)
    return when if when is not None and when <= now else None


def run_cli(args):
    """Run `python3 -m teamosa <args>` and return its exit code (-1 if it couldn't finish)."""
    cmd = [sys.executable, "-m", "teamosa"] + args
    try:
        proc = subprocess.run(cmd, cwd=PACKAGE_ROOT, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %d s", " ".join(args), RUN_TIMEOUT_S)
        return -1
    for line in (proc.stdout + proc.stderr).splitlines():
        log.info("  %s", line)
    return proc.returncode


class Hub:
    def __init__(self, home, runner=run_cli, clock=datetime.now):
        self.home = Path(home)
        self.state_dir = self.home / "state"
        self.settings_path = self.home / "settings.json"
        self.fired_path = self.state_dir / "scheduler.json"
        self.runner = runner
        self.clock = clock
        self.lock = threading.Lock()   # one Bluetooth action at a time from this service
        self.action = None             # the last check/brew this service ran, for the dashboard
        self.last_fired = self._read_json(self.fired_path, {}).get("last_fired")

    # -- settings ------------------------------------------------------------
    @staticmethod
    def _read_json(path, default):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)

    def load_settings(self):
        try:
            return validate_settings(self._read_json(self.settings_path, DEFAULT_SETTINGS))
        except SettingsError:
            log.warning("settings.json is invalid; using defaults (schedule off)")
            return dict(DEFAULT_SETTINGS)

    def save_settings(self, settings):
        self._write_json(self.settings_path, settings)

    # -- running the CLI -------------------------------------------------------
    def _cli_args(self, command, settings=None):
        args = [command, "--state-dir", str(self.state_dir)]
        if command == "brew":
            args += ["--temp", str(settings["temp"]), "--time", str(settings["minutes"])]
        return args

    def _execute(self, kind, args, retries_on_lock=0):
        """Run the CLI while holding self.lock (the caller acquired it); release it at the end."""
        started = self.clock()
        self.action = {"kind": kind, "started": _now_iso(started), "running": True}
        try:
            code = -1
            for attempt in range(retries_on_lock + 1):
                try:
                    code = self.runner(args)
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", kind)
                    code = -1
                if code != 7 or attempt == retries_on_lock:
                    break
                log.info("%s: another run holds the lock; retrying in 15 s", kind)
                time.sleep(15)
        finally:
            self.action = {"kind": kind, "started": _now_iso(started),
                           "finished": _now_iso(self.clock()), "running": False, "exit": code,
                           "message": EXIT_MESSAGES.get(code, "Something went wrong; see the log.")}
            self.lock.release()
        log.info("%s finished with exit %s", kind, code)
        return code

    def start(self, kind, args):
        """Start a dashboard action in the background; False if one is already running."""
        if not self.lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._execute, args=(kind, args), daemon=True).start()
        return True

    # -- the schedule ----------------------------------------------------------
    def tick(self):
        settings = self.load_settings()
        when = due(settings, self.clock(), self.last_fired)
        if when is None:
            return
        self.last_fired = fire_key(when)
        self._write_json(self.fired_path, {"last_fired": self.last_fired})
        log.info("scheduled brew for %s: %s C, %s min", self.last_fired,
                 settings["temp"], settings["minutes"])
        if not self.lock.acquire(timeout=SCHEDULE_LOCK_WAIT_S):
            log.error("scheduled brew for %s skipped: a dashboard action ran too long",
                      self.last_fired)
            return
        self._execute("scheduled", self._cli_args("brew", settings), retries_on_lock=8)

    def scheduler_loop(self):
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001  never let the scheduler thread die
                log.exception("scheduler tick failed")
            time.sleep(10 - time.time() % 10)

    # -- what the dashboard sees -----------------------------------------------
    def status(self, allowed, kiosk):
        if not allowed:
            return {"allowed": False}
        settings = self.load_settings()
        now = self.clock()
        upcoming = next_run(settings, now, self.last_fired)
        return {
            "allowed": True,
            "kiosk": kiosk,
            "now": _now_iso(now),
            "settings": settings,
            "next_run": upcoming.isoformat(timespec="minutes") if upcoming else None,
            "busy": self.lock.locked(),
            "action": self.action,
            "last_run": self._read_json(self.state_dir / "last_run.json", None),
            "limits": {"temp_min": protocol.MIN_TEMP_C, "temp_max": protocol.MAX_TEMP_C,
                       "temp_step": protocol.TEMP_STEP_C, "minutes_min": protocol.MIN_MINUTES,
                       "minutes_max": protocol.MAX_MINUTES, "minutes_step": 0.5},
        }


def create_app(hub):
    app = Flask(__name__)

    def access():
        return client_access(request.remote_addr, request.headers.get("X-Real-IP"))

    def refused():
        return jsonify({"allowed": False, "error": "not allowed from this device"}), 403

    @app.after_request
    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/status")
    def status():
        _, allowed, kiosk = access()
        return jsonify(hub.status(allowed, kiosk))

    @app.post("/settings")
    def settings():
        ip, allowed, kiosk = access()
        if not allowed:
            return refused()
        try:
            new = validate_settings(request.get_json(silent=True))
        except SettingsError as exc:
            return jsonify({"error": str(exc)}), 400
        hub.save_settings(new)
        log.info("settings changed by %s: %s", ip, new)
        return jsonify(hub.status(allowed, kiosk))

    @app.post("/check")
    def check():
        ip, allowed, kiosk = access()
        if not allowed:
            return refused()
        if (request.get_json(silent=True) or {}).get("go") is not True:
            return jsonify({"error": "send {\"go\": true}"}), 400
        if not hub.start("check", hub._cli_args("check")):
            return jsonify({"error": "busy"}), 409
        log.info("check started by %s", ip)
        return jsonify(hub.status(allowed, kiosk)), 202

    @app.post("/brew")
    def brew():
        ip, allowed, kiosk = access()
        if not allowed:
            return refused()
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            return jsonify({"error": "send {\"confirm\": true}"}), 400
        current = hub.load_settings()
        if not hub.start("brew", hub._cli_args("brew", current)):
            return jsonify({"error": "busy"}), 409
        log.info("brew now started by %s: %s C, %s min", ip, current["temp"], current["minutes"])
        return jsonify(hub.status(allowed, kiosk)), 202

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    hub = Hub(Path(os.environ.get("TEAMOSA_HOME", PACKAGE_ROOT)))
    threading.Thread(target=hub.scheduler_loop, name="scheduler", daemon=True).start()
    create_app(hub).run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
