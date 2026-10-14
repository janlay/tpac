"""Access point LED state."""
from __future__ import annotations

from . import capwap_control as cc

LED_STATUS = 0x38
STATES = {0: "off", 1: "on"}


def parse(message: bytes) -> str | None:
    if cc.message_type(message) != cc.ECHO_REQUEST:
        return None
    for ptype, data in cc.vendor_payloads(message):
        if ptype == LED_STATUS and len(data) == 4:
            return STATES.get(int.from_bytes(data, "big"))
    return None
