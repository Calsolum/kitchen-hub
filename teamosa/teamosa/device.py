"""BLE side: connect, check the machine is ready, send ONE brew frame, listen briefly.

Safety rules baked in:
  * never pairs or bonds;
  * the brew frame is written at most once per run. Connect/subscribe failures
    before the write are retried; a failure at the write is not, because we
    cannot tell whether the machine received it and a second brew is worse than
    a reported failure;
  * if the machine reports no cup / no water / lid problem in the pre-flight
    window, nothing is written.
"""
import asyncio
import logging
import time

from bleak import BleakClient

from . import protocol

log = logging.getLogger("teamosa")


class Skipped(Exception):
    """The machine reported a problem before the write; nothing was sent."""

    def __init__(self, frames):
        super().__init__(", ".join(sorted(frames)))
        self.frames = sorted(frames)


class WriteFailed(Exception):
    """The write raised; it is unknown whether the machine received it."""


async def _disconnect(client):
    # bleak's disconnect() raises EOFError from dbus_fast on this Pi; the device
    # still disconnects, so swallow everything here.
    try:
        await asyncio.wait_for(client.disconnect(), 10)
    except Exception as exc:  # noqa: BLE001
        log.debug("disconnect raised %s: %s (ignored)", type(exc).__name__, exc)


async def _attempt(address, frame, preflight_s, listen_s, connect_timeout, check_only):
    seen = []  # (monotonic time, frame bytes)

    def on_frame(_, data):
        seen.append((time.monotonic(), bytes(data)))
        log.info("notify %s", bytes(data).hex())

    client = BleakClient(address, timeout=connect_timeout)
    try:
        await asyncio.wait_for(client.connect(), connect_timeout + 10)
        log.info("connected to %s", address)
        char = client.services.get_characteristic(protocol.WRITE_UUID)
        if char is None or "write" not in char.properties:
            raise RuntimeError("write characteristic C304 missing or not writable")
        await client.start_notify(protocol.NOTIFY_UUID, on_frame)
        await client.start_notify(protocol.INDICATE_UUID, on_frame)
        await asyncio.sleep(preflight_s)

        problems = {name for _, f in seen
                    if (name := protocol.decode_reply(f)[0]) is not None}
        if problems:
            raise Skipped(problems)
        if check_only:
            return {"frames": [f.hex() for _, f in seen]}

        log.info("writing %s", frame.decode("ascii"))
        try:
            await client.write_gatt_char(protocol.WRITE_UUID, frame, response=True)
        except Exception as exc:  # noqa: BLE001
            raise WriteFailed(f"{type(exc).__name__}: {exc}") from exc
        log.info("write accepted")

        mark = len(seen)
        await asyncio.sleep(listen_s)
        after = [protocol.decode_reply(f)[0] or f.hex() for _, f in seen[mark:]]
        return {"frames": [f.hex() for _, f in seen], "after_write": after}
    finally:
        await _disconnect(client)


async def run(address, frame, *, preflight_s=4.0, listen_s=10.0, attempts=3,
              backoff_s=5.0, connect_timeout=20.0, check_only=False):
    """Return a result dict. Raises Skipped, WriteFailed, or the last connect error."""
    last = None
    for n in range(1, attempts + 1):
        try:
            result = await _attempt(address, frame, preflight_s, listen_s,
                                    connect_timeout, check_only)
            result["attempts"] = n
            return result
        except (Skipped, WriteFailed):
            raise
        except Exception as exc:  # noqa: BLE001  connect/subscribe failure: safe to retry
            last = exc
            log.warning("attempt %d/%d failed before any write: %s: %s",
                        n, attempts, type(exc).__name__, exc)
            if n < attempts:
                await asyncio.sleep(backoff_s)
    raise last
