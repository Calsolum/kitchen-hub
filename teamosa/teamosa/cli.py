"""Command line for the kitchen hub.

  cd ~/teamosa
  python3 -m teamosa brew --temp 95 --time 3.5 --dry-run   # print the frame, no Bluetooth
  python3 -m teamosa check                                  # connect, report cup/water/lid; sends nothing
  python3 -m teamosa brew                                   # temp/time from TEAMOSA_TEMP / TEAMOSA_TIME
  python3 -m teamosa brew --force ...                       # even within 30 min of the last brew

Exit codes: 0 ok, 2 bad arguments, 3 machine not ready (nothing sent), 4 could not
connect, 5 write failed (unknown whether it brewed), 6 too soon after the last
brew, 7 another run holds the lock.
"""
import argparse
import asyncio
import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import protocol

log = logging.getLogger("teamosa")

DEFAULT_ADDRESS = "AA:BB:CC:DD:EE:FF"   # set TEAMOSA_ADDRESS to your brewer's BLE address
EXIT_OK, EXIT_NOT_READY, EXIT_CONNECT, EXIT_WRITE, EXIT_TOO_SOON, EXIT_LOCKED = 0, 3, 4, 5, 6, 7
# Outcomes after which the machine may have brewed; they count towards --min-gap.
BREW_OUTCOMES = {"brewed", "write_unknown"}
# The machine has no cancel and reports nothing while it brews, so a second brew this soon
# is refused unless --force (the dashboard's Brew anyway; the schedule never forces).
MIN_GAP_MIN = 30


def _env_float(name):
    value = os.environ.get(name, "").strip()
    return float(value) if value else None


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--address", default=os.environ.get("TEAMOSA_ADDRESS", DEFAULT_ADDRESS))
    common.add_argument("--state-dir", type=Path,
                        default=Path(os.environ.get("TEAMOSA_STATE_DIR",
                                                    Path.home() / "teamosa" / "state")))
    common.add_argument("--preflight", type=float, default=4.0,
                        help="seconds to listen for cup/water/lid problems before writing (default 4)")
    common.add_argument("--attempts", type=int, default=3,
                        help="connection attempts, 5 s apart (default 3)")
    common.add_argument("-v", "--verbose", action="store_true")

    ap = argparse.ArgumentParser(prog="teamosa", description="Teamosa brewer over BLE")
    sub = ap.add_subparsers(dest="command", required=True)

    brew = sub.add_parser("brew", parents=[common], help="start one brew")
    brew.add_argument("--temp", type=float, default=_env_float("TEAMOSA_TEMP"),
                      help="degrees C, 60..95 in steps of 5 (env TEAMOSA_TEMP)")
    brew.add_argument("--time", type=float, default=_env_float("TEAMOSA_TIME"),
                      help="minutes, 3..9.5 in steps of 0.5 (env TEAMOSA_TIME)")
    brew.add_argument("--dry-run", action="store_true",
                      help="print the frame and exit without using Bluetooth")
    brew.add_argument("--force", action="store_true", help="brew even within --min-gap of the last brew")
    brew.add_argument("--min-gap", type=float, default=MIN_GAP_MIN,
                      help=f"refuse if the last brew was less than this many minutes ago (default {MIN_GAP_MIN})")
    brew.add_argument("--extended", action="store_true",
                      help="allow values outside the app's range (untested on the machine)")
    brew.add_argument("--listen", type=float, default=10,
                      help="seconds to log replies after the write (default 10)")

    sub.add_parser("check", parents=[common],
                   help="connect and report cup/water/lid problems; sends no command")
    return ap


def _lock(state_dir):
    import fcntl  # Linux only; the hub is Linux

    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "brew.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def last_brew_ts(state_dir):
    """Start time (epoch seconds) of the last run that may have brewed, or None."""
    last = None
    try:
        with open(state_dir / "runs.jsonl") as f:
            for line in f:
                try:
                    run = json.loads(line)
                except ValueError:
                    continue
                if run.get("outcome") in BREW_OUTCOMES:
                    last = run.get("ts")
    except OSError:
        pass
    return last


def _record(state_dir, record):
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record)
    (state_dir / "last_run.json").write_text(line + "\n")
    with open(state_dir / "runs.jsonl", "a") as f:
        f.write(line + "\n")


def _notify(title, message):
    """POST a plain-text message to TEAMOSA_NOTIFY_URL (ntfy-style), if configured."""
    url = os.environ.get("TEAMOSA_NOTIFY_URL", "").strip()
    if not url:
        return
    req = urllib.request.Request(url, data=message.encode("utf-8"),
                                 headers={"Title": title}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:  # noqa: BLE001  a failed notification must not change the outcome
        log.warning("notification failed: %s", exc)


async def _run(args, frame):
    from . import device  # imported here so --dry-run and the tests work without bleak

    check = args.command == "check"
    record = {"ts": time.time(), "time": datetime.now().isoformat(timespec="seconds"),
              "command": args.command, "address": args.address,
              "frame": frame.decode("ascii") if frame else None}
    if not check and args.force:
        record["forced"] = True
    try:
        result = await device.run(args.address, frame or b"", preflight_s=args.preflight,
                                  listen_s=0 if check else args.listen,
                                  attempts=args.attempts, check_only=check)
    except device.Skipped as exc:
        reasons = "; ".join(protocol.REPLIES[f.encode()] for f in exc.frames)
        record.update(outcome="not_ready", problems=exc.frames)
        _record(args.state_dir, record)
        log.error("machine not ready (%s); nothing was sent", reasons)
        if not check:
            _notify("Teamosa: brew skipped", f"No brew: {reasons}. Nothing was sent.")
        return EXIT_NOT_READY
    except device.WriteFailed as exc:
        record.update(outcome="write_unknown", error=str(exc))
        _record(args.state_dir, record)
        log.error("write failed (%s); the machine may or may not be brewing", exc)
        _notify("Teamosa: brew uncertain",
                f"The brew command failed ({exc}). Check the machine.")
        return EXIT_WRITE
    except Exception as exc:  # noqa: BLE001  every connect attempt failed before any write
        record.update(outcome="connect_failed", error=f"{type(exc).__name__}: {exc}")
        _record(args.state_dir, record)
        log.error("could not reach the machine: %s: %s", type(exc).__name__, exc)
        if not check:
            _notify("Teamosa: brew failed",
                    f"Could not connect to the brewer ({type(exc).__name__}). Nothing was sent.")
        return EXIT_CONNECT

    record.update(outcome="ready" if check else "brewed", **result)
    _record(args.state_dir, record)
    if check:
        log.info("machine reachable and reporting no problems (attempt %d)", result["attempts"])
    else:
        log.info("brew started: %s (attempt %d)", record["frame"], result["attempts"])
        problems = [f for f in result.get("after_write", []) if f in ("CUP", "EAU", "COV")]
        if problems:
            log.warning("machine reported %s after the command", ", ".join(problems))
        if os.environ.get("TEAMOSA_NOTIFY_SUCCESS") == "1":
            _notify("Teamosa: brewing", f"Brew started: {record['frame']}.")
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    frame = None
    if args.command == "brew":
        if args.temp is None or args.time is None:
            parser.error("brew needs --temp and --time (or TEAMOSA_TEMP and TEAMOSA_TIME)")
        try:
            frame = protocol.encode_brew(args.temp, args.time, extended=args.extended)
        except protocol.ProtocolError as exc:
            parser.error(str(exc))
        if args.dry_run:
            print(f"would send {frame.decode('ascii')} ({frame.hex()}) to {args.address}")
            return EXIT_OK

    lock = _lock(args.state_dir)
    if lock is None:
        log.error("another teamosa run is in progress")
        return EXIT_LOCKED
    try:
        if args.command == "brew":
            last = last_brew_ts(args.state_dir)
            if last is not None and time.time() - last < args.min_gap * 60:
                ago = (time.time() - last) / 60
                if not args.force:
                    log.error("last brew was %.0f min ago (min gap %g); use --force to brew anyway",
                              ago, args.min_gap)
                    return EXIT_TOO_SOON
                log.warning("last brew was %.0f min ago; brewing anyway (--force)", ago)
        return asyncio.run(_run(args, frame))
    finally:
        lock.close()
