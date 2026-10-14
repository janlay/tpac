"""Access point firmware upload."""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import capwap_control as cc

CONFIG_UPDATE_REQUEST = cc.CONFIG_UPDATE_REQUEST
CONFIG_UPDATE_RESPONSE = cc.CONFIG_UPDATE_RESPONSE
IMAGE_DATA_REQUEST = 15
IMAGE_DATA_RESPONSE = 16
RESET_REQUEST = cc.RESET_REQUEST

ELEMENT_IMAGE_DATA = 24
ELEMENT_IMAGE_INFO = 25
ELEMENT_IMAGE_IDENTIFIER = 26
ELEMENT_RESULT_CODE = 33
ELEMENT_VENDOR = 37

BLOCK_SIZE = 1024
PAD = 0xFF
VENDOR_ID = bytes.fromhex("12345678")
RESET_VENDOR_VALUE = bytes.fromhex("12345678002300230004" "00000001")

WINDOW = 1
RETRY_AFTER = 1.0
MAX_TRIES = 5


def element(etype: int, value: bytes) -> bytes:
    return struct.pack(">HH", etype, len(value)) + value


def version_record(patch: int, build_date: str, release: int) -> bytes:
    year, month, day = int(build_date[0:4]), int(build_date[4:6]), int(build_date[6:8])
    return (
        VENDOR_ID
        + struct.pack(">H", 1)
        + struct.pack(">I", patch)
        + struct.pack(">HBB", year, month, day)
        + struct.pack(">H", 0)
        + struct.pack(">H", release)
    )


def parse_version_record(value: bytes) -> dict:
    patch, = struct.unpack_from(">I", value, 6)
    year, month, day = struct.unpack_from(">HBB", value, 10)
    release, = struct.unpack_from(">H", value, 16)
    return {"patch": patch, "build": f"{year:04d}{month:02d}{day:02d}", "release": release}


def config_update_request(txid: int, record: bytes) -> bytes:
    return cc.build(CONFIG_UPDATE_REQUEST, txid, element(ELEMENT_IMAGE_INFO, record))


def image_data_response(txid: int, identifier: bytes) -> bytes:
    return cc.build(
        IMAGE_DATA_RESPONSE, txid,
        element(ELEMENT_RESULT_CODE, struct.pack(">I", 0)) + element(ELEMENT_IMAGE_IDENTIFIER, identifier),
    )


def image_data_request(txid: int, data_type: int, block: bytes, identifier: bytes | None = None) -> bytes:
    payload = element(ELEMENT_IMAGE_DATA, bytes([data_type]) + block)
    if identifier is not None:
        payload += element(ELEMENT_IMAGE_IDENTIFIER, identifier)
    return cc.build(IMAGE_DATA_REQUEST, txid, payload)


def reset_request(txid: int, record: bytes) -> bytes:
    return cc.build(
        RESET_REQUEST, txid,
        element(ELEMENT_IMAGE_INFO, record) + element(ELEMENT_VENDOR, RESET_VENDOR_VALUE),
    )


def _response_codes(message: bytes) -> tuple[int, int]:
    code = vendor = 0
    for etype, value in cc.elements(message):
        if etype == ELEMENT_RESULT_CODE and len(value) >= 4:
            code = int.from_bytes(value[:4], "big")
        elif etype == ELEMENT_VENDOR and len(value) >= 14 and value[:4] == VENDOR_ID:
            vendor = int.from_bytes(value[10:14], "big")
    return code, vendor


@dataclass
class _Slot:
    block: int
    sent_at: float
    tries: int


@dataclass
class Upload:

    image: bytes
    record: bytes
    identifier: bytes
    next_txid: callable
    now: callable = time.monotonic
    window: int = WINDOW
    state: str = "offer"
    next_block: int = 0
    acked: set[int] = field(default_factory=set)
    inflight: dict[int, _Slot] = field(default_factory=dict)
    error: str = ""
    reset_txid: int | None = None
    retries: int = 0

    @property
    def blocks(self) -> int:
        return (len(self.image) + BLOCK_SIZE - 1) // BLOCK_SIZE

    def block_bytes(self, index: int) -> bytes:
        chunk = self.image[index * BLOCK_SIZE : (index + 1) * BLOCK_SIZE]
        return chunk.ljust(BLOCK_SIZE, bytes([PAD]))

    def start(self) -> bytes:
        return config_update_request(self.next_txid(), self.record)

    def on_message(self, message: bytes) -> list[bytes]:
        mtype = cc.message_type(message)
        txid = cc.transaction_id(message)
        if self.state == "offer" and mtype == CONFIG_UPDATE_RESPONSE:
            self.state = "requested"
            return []
        if self.state in ("offer", "requested") and mtype == IMAGE_DATA_REQUEST:
            self.state = "data"
            return [image_data_response(txid, self.identifier)] + self._fill()
        if self.state == "data" and mtype == IMAGE_DATA_RESPONSE:
            code, vendor = _response_codes(message)
            slot = self.inflight.get(txid)
            if slot is not None and (code or vendor):
                self.state = "failed"
                self.error = f"the AP rejected block {slot.block}: result {code}, vendor value {vendor}"
                return []
            return self._acked(txid)
        return []

    def on_timeout(self) -> list[bytes]:
        if self.state != "data":
            return []
        out = []
        now = self.now()
        for txid, slot in list(self.inflight.items()):
            if now - slot.sent_at < RETRY_AFTER:
                continue
            if slot.tries >= MAX_TRIES:
                self.state = "failed"
                self.error = f"block {slot.block} not acknowledged after {MAX_TRIES} tries"
                return []
            slot.tries += 1
            self.retries += 1
            slot.sent_at = now
            out.append(self._block_message(txid, slot.block))
        return out

    def _block_message(self, txid: int, index: int) -> bytes:
        last = index == self.blocks - 1
        return image_data_request(
            txid, 2 if last else 1, self.block_bytes(index), self.identifier if last else None
        )

    def _fill(self) -> list[bytes]:
        out = []
        while self.next_block < self.blocks and len(self.inflight) < self.window:
            index = self.next_block
            self.next_block += 1
            txid = self.next_txid()
            self.inflight[txid] = _Slot(index, self.now(), 1)
            out.append(self._block_message(txid, index))
        return out

    def _acked(self, txid: int) -> list[bytes]:
        slot = self.inflight.pop(txid, None)
        if slot is None:
            return []
        self.acked.add(slot.block)
        if len(self.acked) == self.blocks:
            self.state = "resetting"
            self.reset_txid = self.next_txid()
            return [reset_request(self.reset_txid, self.record)]
        return self._fill()
