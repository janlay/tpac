"""Access point configuration file."""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from . import capwap_control as cc
from . import wifi_profile as wp

HEAD_SIZE = 84
NAME_SIZE = 30
LZMA_PROPS = (0, 0, 2)
LEN_HIGH_BITS = 5


def lzma_decode(stream: bytes, raw_size: int) -> bytes:
    lc, lp, pb = LZMA_PROPS
    probs: dict = {}
    code = int.from_bytes(stream[1:5], "big")
    rng = 0xFFFFFFFF
    pos = 5
    out = bytearray()

    def bit(key) -> int:
        nonlocal rng, code, pos
        p = probs.get(key, 1024)
        bound = (rng >> 11) * p
        if code < bound:
            rng = bound
            probs[key] = p + ((2048 - p) >> 5)
            result = 0
        else:
            rng -= bound
            code -= bound
            probs[key] = p - (p >> 5)
            result = 1
        if rng < 1 << 24:
            rng = (rng << 8) & 0xFFFFFFFF
            code = ((code << 8) | (stream[pos] if pos < len(stream) else 0)) & 0xFFFFFFFF
            pos += 1
        return result

    def tree(key, bits: int) -> int:
        m = 1
        for _ in range(bits):
            m = (m << 1) | bit((key, m))
        return m - (1 << bits)

    def reverse_tree(key, bits: int) -> int:
        m, value = 1, 0
        for i in range(bits):
            b = bit((key, m))
            m = (m << 1) | b
            value |= b << i
        return value

    def length(key, pos_state: int) -> int:
        if not bit((key, "c")):
            return tree((key, "lo", pos_state), 3)
        if not bit((key, "c2")):
            return 8 + tree((key, "mid", pos_state), 3)
        return 16 + tree((key, "hi"), LEN_HIGH_BITS)

    state, rep = 0, [0, 0, 0, 0]
    while len(out) < raw_size:
        pos_state = len(out) & ((1 << pb) - 1)
        if not bit(("m", state, pos_state)):
            ctx = ((len(out) & ((1 << lp) - 1)) << lc) + ((out[-1] if out else 0) >> (8 - lc))
            sym = 1
            if state >= 7:
                match = out[-rep[0] - 1]
                while sym < 256:
                    match_bit = (match >> 7) & 1
                    match = (match << 1) & 255
                    b = bit(("l", ctx, ((1 + match_bit) << 8) + sym))
                    sym = (sym << 1) | b
                    if match_bit != b:
                        break
            while sym < 256:
                sym = (sym << 1) | bit(("l", ctx, sym))
            out.append(sym & 255)
            state = 0 if state < 4 else state - 3 if state < 10 else state - 6
            continue
        if bit(("rep", state)):
            if not bit(("rep0", state)):
                if not bit(("rep0long", state, pos_state)):
                    state = 9 if state < 7 else 11
                    out.append(out[-rep[0] - 1])
                    continue
            else:
                if not bit(("rep1", state)):
                    dist = rep[1]
                else:
                    if not bit(("rep2", state)):
                        dist = rep[2]
                    else:
                        dist, rep[3] = rep[3], rep[2]
                    rep[2] = rep[1]
                rep[1], rep[0] = rep[0], dist
            size = length("replen", pos_state)
            state = 8 if state < 7 else 11
        else:
            rep[3], rep[2], rep[1] = rep[2], rep[1], rep[0]
            size = length("len", pos_state)
            state = 7 if state < 7 else 10
            slot = tree(("slot", min(size, 3)), 6)
            if slot < 4:
                dist = slot
            else:
                bits = (slot >> 1) - 1
                dist = (2 | (slot & 1)) << bits
                if slot < 14:
                    dist += reverse_tree(("spec", slot), bits)
                else:
                    direct = 0
                    for _ in range(bits - 4):
                        rng >>= 1
                        code = (code - rng) & 0xFFFFFFFF
                        mask = -(code >> 31)
                        code = (code + (rng & mask)) & 0xFFFFFFFF
                        direct = ((direct << 1) + (mask + 1)) & 0xFFFFFFFF
                        if rng < 1 << 24:
                            rng = (rng << 8) & 0xFFFFFFFF
                            code = ((code << 8) | (stream[pos] if pos < len(stream) else 0)) & 0xFFFFFFFF
                            pos += 1
                    dist += (direct << 4) + reverse_tree("align", 4)
            rep[0] = dist
        if rep[0] >= len(out):
            raise ValueError("LZMA distance outside the output")
        for _ in range(size + 2):
            if len(out) >= raw_size:
                break
            out.append(out[-rep[0] - 1])
    return bytes(out)


def lzma_encode(raw: bytes) -> bytes:
    lc, lp, pb = LZMA_PROPS
    probs: dict = {}
    low, rng = 0, 0xFFFFFFFF
    cache, cache_size = 0, 1
    out = bytearray()

    def shift_low() -> None:
        nonlocal low, cache, cache_size
        if low < 0xFF000000 or low >= 1 << 32:
            carry = low >> 32
            byte = cache
            while True:
                out.append((byte + carry) & 255)
                byte = 255
                cache_size -= 1
                if not cache_size:
                    break
            cache = (low >> 24) & 255
        cache_size += 1
        low = (low & 0x00FFFFFF) << 8

    def put(key, value: int) -> None:
        nonlocal low, rng
        p = probs.get(key, 1024)
        bound = (rng >> 11) * p
        if value:
            low += bound
            rng -= bound
            probs[key] = p - (p >> 5)
        else:
            rng = bound
            probs[key] = p + ((2048 - p) >> 5)
        while rng < 1 << 24:
            rng = (rng << 8) & 0xFFFFFFFF
            shift_low()

    state = 0
    for i, byte in enumerate(raw):
        put(("m", state, i & ((1 << pb) - 1)), 0)
        ctx = ((i & ((1 << lp) - 1)) << lc) + ((raw[i - 1] if i else 0) >> (8 - lc))
        sym = 1
        for shift in range(7, -1, -1):
            b = (byte >> shift) & 1
            put(("l", ctx, sym), b)
            sym = (sym << 1) | b
        state = 0
    for _ in range(5):
        shift_low()
    return bytes(out)


def unpack(blob: bytes) -> bytes:
    raw_size, blob_size = struct.unpack(">II", blob[:8])
    if blob[8] != 0x5A or raw_size > 0x10000:
        raise ValueError("unexpected LZMA header")
    return lzma_decode(blob[8 + 13 :], raw_size)


def pack(raw: bytes) -> bytes:
    stream = struct.pack("<BIQ", 0x5A, 4096, len(raw)) + lzma_encode(raw)
    return struct.pack(">II", len(raw), len(stream) + 8) + stream


def parse(raw: bytes) -> tuple[bytes, dict[str, bytes], int]:
    if hashlib.md5(raw[HEAD_SIZE:]).digest() != raw[:16]:
        raise ValueError("MD5 mismatch")
    version, size = struct.unpack(">HH", raw[0x50:0x54])
    if size != len(raw) - HEAD_SIZE:
        raise ValueError("body size mismatch")
    sections, pos = {}, HEAD_SIZE
    while pos < len(raw):
        name = raw[pos : pos + NAME_SIZE].split(b"\0")[0].decode()
        (length,) = struct.unpack(">I", raw[pos + NAME_SIZE : pos + NAME_SIZE + 4])
        pos += NAME_SIZE + 4
        sections[name] = raw[pos : pos + length]
        pos += length
    if pos != len(raw):
        raise ValueError("section overruns the file")
    return raw[16:0x50], sections, version


def build(model: bytes, sections: dict[str, bytes], version: int = 0x10) -> bytes:
    body = b"".join(
        name.encode().ljust(NAME_SIZE, b"\0") + struct.pack(">I", len(data)) + data
        for name, data in sections.items()
    )
    return hashlib.md5(body).digest() + model.ljust(0x40, b"\0") + struct.pack(">HH", version, len(body)) + body


MODEL = "TL-R470GP-AC\u00a07.0".encode()
DEVICE_CLASS = 3
SUB_CONFIG_FILE = 0x1C
FRAGMENT_SIZE = 1280
_TEMPLATE = json.loads(Path(__file__).with_name("wireless_template.json").read_text())
_WLAN_SERV = {
    "desc": "", "ssid_code_type": "1", "radius_ip": "0.0.0.0", "radius_port": "1024",
    "radius_acct_port": "0", "radius_pwd": "", "key_update_intv": "86400", "isolate": "0",
    "network_type": "1", "bw_ctrl_enable": "0", "bw_ctrl_mode": "1", "up_limit": "1",
    "down_limit": "1", "max_sta": "100", "wss_support": "0",
}


def _percent(text: str) -> str:
    return "".join(chr(b) if chr(b).isalnum() and b < 128 else f"%{b:02X}" for b in text.encode())


def wlan_serv(slot: wp.Slot, network: wp.WifiNetwork) -> dict:
    index = slot.wlan_id - 1
    serv_id = (slot.radio_id - 1) * 8 + index + 1
    auth = "0" if network.auth == "auto" else str(wp.AUTH_CODES[network.auth])
    return {f"wlan_serv_{serv_id}": {
        "radio_id": str(slot.radio_id), "apIdx": str(index), "serv_id": str(serv_id),
        "enable": "on", "ssid": _percent(network.ssid), "ssidbrd": "0" if network.hidden else "1",
        "encryption": "1", "auth": auth, "cipher": str(wp.CIPHER_CODES[network.cipher]),
        "key": network.password, "vlan_id": str(network.vlan), **_WLAN_SERV,
    }}


_SYSTEM = {"system": {"global_config": {"mngt_mode": "1", "controller_detect_mode": "1", "work_mode": "0"}}}


_FIREWALL = b'{"redirect":[ ]}'


def wireless_file(networks: dict[str, wp.WifiNetwork], model: bytes | None = None) -> bytes:
    wireless = {"wlan_serv": [wlan_serv(wp.SLOT_BY_NAME[name], n) for name, n in networks.items()], **_TEMPLATE}
    dump = lambda value: json.dumps(value, separators=(",", ":")).encode()
    return build(model or MODEL, {"system": dump(_SYSTEM), "wireless": dump(wireless), "firewall": _FIREWALL})


def message(txid: int, ap_mac: bytes, raw: bytes) -> bytes:
    data = pack(raw)
    body = struct.pack(">I", 3) + ap_mac + struct.pack(">HH", DEVICE_CLASS, len(data)) + data
    vendor = struct.pack(">HHH", 1, SUB_CONFIG_FILE, len(body)) + body
    value = struct.pack(">IHHH", 0, 0, 0x76, len(vendor)) + vendor
    return cc.build(cc.CONFIG_UPDATE_REQUEST, txid, struct.pack(">HH", 0x25, len(value)) + value)


def fragments(msg: bytes, fragment_id: int) -> list[bytes]:
    body = msg[8:]
    if len(body) <= FRAGMENT_SIZE:
        return [msg]
    out = []
    for at in range(0, len(body), FRAGMENT_SIZE):
        last = at + FRAGMENT_SIZE >= len(body)
        head = bytearray(msg[:8])
        head[3] |= 0xC0 if last else 0x80
        head[4:6] = fragment_id.to_bytes(2, "big")
        head[6:8] = ((at // 8) << 3).to_bytes(2, "big")
        out.append(bytes(head) + body[at : at + FRAGMENT_SIZE])
    return out
