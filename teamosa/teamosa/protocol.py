"""Teamosa command encoding. Pure: bytes in, bytes out, no I/O.

Source: static reading of the vendor app (see recon/PHASE2_NOTES.md). The one frame
verified on the machine is TEA60,6,25U (2026-10-26). Everything else follows the
same app code but has not been sent yet.
"""

SERVICE_UUID = "0000a002-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"
INDICATE_UUID = "0000c306-0000-1000-8000-00805f9b34fb"

# What the app's own sliders allow: 60..95 C in steps of 5, 3..9.5 min in 0.5 steps.
MIN_TEMP_C, MAX_TEMP_C, TEMP_STEP_C = 60, 95, 5
MIN_MINUTES, MAX_MINUTES = 3.0, 9.5
# Meaning of the "25" before the letter is unknown; the app always sends 25 for a brew.
BREW_SUFFIX = "25U"

# Status frames the machine sends on the notify characteristic (ASCII).
REPLIES = {
    b"CUP": "no cup on the sensor",
    b"EAU": "water tank empty or low",
    b"COV": "lid problem",
}


class ProtocolError(ValueError):
    pass


def encode_brew(temp_c, minutes, extended=False):
    """Return the bytes the app sends to start a brew.

    `extended` lifts the app's 60..95 C / 3..9.5 min limits (the machine's own
    limits are unknown); use it only for deliberate tests.
    """
    if int(temp_c) != temp_c:
        raise ProtocolError(f"temperature must be a whole number, got {temp_c!r}")
    temp_c = int(temp_c)
    half_minutes = minutes * 2
    if half_minutes != int(half_minutes):
        raise ProtocolError(f"time must be a multiple of 0.5 minutes, got {minutes!r}")
    half_minutes = int(half_minutes)
    if not extended:
        if not MIN_TEMP_C <= temp_c <= MAX_TEMP_C or temp_c % TEMP_STEP_C:
            raise ProtocolError(
                f"temperature {temp_c} C outside the app's range "
                f"{MIN_TEMP_C}..{MAX_TEMP_C} in steps of {TEMP_STEP_C}")
        if not MIN_MINUTES <= minutes <= MAX_MINUTES:
            raise ProtocolError(
                f"time {minutes} min outside the app's range {MIN_MINUTES}..{MAX_MINUTES}")
    elif temp_c <= 0 or half_minutes <= 0:
        raise ProtocolError("temperature and time must be positive")
    return f"TEA{temp_c},{half_minutes},{BREW_SUFFIX}".encode("ascii")


def decode_reply(data):
    """Map a notify frame to (name, description); name is None if unrecognised."""
    data = bytes(data)
    if data in REPLIES:
        return data.decode("ascii"), REPLIES[data]
    return None, f"unrecognised frame {data.hex()}"
