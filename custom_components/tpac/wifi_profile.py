"""Wi-Fi profiles and WLAN messages."""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from . import capwap_control as cc

ELEMENT_WLAN = 0x0400
ELEMENT_VENDOR = 0x0025
VENDOR_ID = 0x12345678
VENDOR_PREFIX = bytes.fromhex("00630063")
SECURITY_SUITE = {
    "psk": bytes.fromhex("030001030200015180"),
}
AUTH_CODES = {"auto": 3, "wpa": 1, "wpa2": 2}
CIPHER_CODES = {"auto": 0, "tkip": 1, "aes": 2}


def _suite(network: "WifiNetwork") -> bytes:
    if network.auth not in AUTH_CODES or network.cipher not in CIPHER_CODES:
        raise ValueError(f"unsupported auth/cipher: {network.auth!r}/{network.cipher!r}")
    base = bytearray(SECURITY_SUITE[network.security])
    base[3], base[4] = AUTH_CODES[network.auth], CIPHER_CODES[network.cipher]
    return bytes(base)


@dataclass
class Slot:
    name: str
    query_key: bytes
    radio_id: int
    wlan_id: int
    header: bytes
    band: str


SLOTS: list[Slot] = [
    Slot("radio1_wlan1", bytes.fromhex("0006020200000000"), 0x01, 0x01,
         bytes.fromhex("01018000000000000000000000000000000101"), "2.4GHz"),
    Slot("radio1_wlan3", bytes.fromhex("0006020400000000"), 0x01, 0x03,
         bytes.fromhex("01038000000000000000000000000000000100"), "2.4GHz"),
    Slot("radio2_wlan1", bytes.fromhex("0006030200000000"), 0x02, 0x01,
         bytes.fromhex("02018000000000000000000000000000000101"), "5GHz"),
    Slot("radio2_wlan2", bytes.fromhex("0006030300000000"), 0x02, 0x02,
         bytes.fromhex("02028000000000000000000000000000000101"), "5GHz"),
]

MAX_WLANS_PER_RADIO = 7
_known = {(slot.radio_id, slot.wlan_id) for slot in SLOTS}
for _radio, _band in ((1, "2.4GHz"), (2, "5GHz")):
    for _wlan in range(1, MAX_WLANS_PER_RADIO + 1):
        if (_radio, _wlan) not in _known:
            SLOTS.append(Slot(
                f"radio{_radio}_wlan{_wlan}",
                bytes.fromhex("0006") + bytes((_radio + 1, _wlan + 1)) + bytes(4),
                _radio, _wlan,
                bytes((_radio, _wlan)) + bytes.fromhex("8000000000000000000000000000000101"),
                _band,
            ))
SLOTS.sort(key=lambda slot: (slot.radio_id, slot.wlan_id))
SLOT_BY_NAME = {slot.name: slot for slot in SLOTS}
SLOT_BY_KEY = {slot.query_key: slot for slot in SLOTS}


@dataclass
class WifiNetwork:
    ssid: str
    password: str = ""
    vlan: int = 0
    security: str = "psk"
    auth: str = "auto"
    cipher: str = "aes"
    hidden: bool = False
    enabled: bool = True


def network_from_json(fields: dict) -> WifiNetwork:
    return WifiNetwork(**{k: v for k, v in fields.items() if k in WifiNetwork.__dataclass_fields__})


def _tlv(element_type: int, value: bytes) -> bytes:
    return struct.pack(">HH", element_type, len(value)) + value


def encode(slot: Slot, txid: int, network: WifiNetwork) -> bytes:
    if network.security not in SECURITY_SUITE:
        raise ValueError(f"unsupported security type: {network.security!r}")
    suite = _suite(network)
    ssid = network.ssid.encode("utf-8")
    password = network.password.encode("utf-8")
    if len(password) > 63:
        raise ValueError("password must be 63 bytes or fewer")

    wlan_header = bytearray(slot.header)
    wlan_header[-1] = 0x00 if network.hidden else 0x01
    wlan_value = bytes(wlan_header) + ssid
    vendor_rest = (
        bytes((slot.radio_id, slot.wlan_id))
        + struct.pack(">H", network.vlan)
        + b"\x00\x64"
        + suite
        + b"\x00"
        + bytes((len(password),))
        + password
    )
    vendor_value = (
        struct.pack(">I", VENDOR_ID)
        + VENDOR_PREFIX
        + struct.pack(">H", len(vendor_rest))
        + vendor_rest
    )
    body = _tlv(ELEMENT_WLAN, wlan_value) + _tlv(ELEMENT_VENDOR, vendor_value)

    header = bytearray(cc.CONTROL_HEADER)
    header[0:4] = b"\x00\x10\x43\x00"
    header[9:11] = cc.VENDOR_MARKER
    header[11] = cc.WLAN_CONFIG_REQUEST
    header[12] = txid & 0xFF
    header[13:15] = (len(body) + 3).to_bytes(2, "big")
    return bytes(header) + body


def encode_delete(slot: Slot, txid: int) -> bytes:
    body = _tlv(1027, bytes((slot.radio_id, slot.wlan_id)))
    header = bytearray(cc.CONTROL_HEADER)
    header[0:4] = b"\x00\x10\x43\x00"
    header[9:11] = cc.VENDOR_MARKER
    header[11] = cc.WLAN_CONFIG_REQUEST
    header[12] = txid & 0xFF
    header[13:15] = (len(body) + 3).to_bytes(2, "big")
    return bytes(header) + body


def decode(reply: bytes) -> WifiNetwork | None:
    wlan_value = vendor_value = None
    for element_type, value in cc.elements(reply):
        if element_type == ELEMENT_WLAN:
            wlan_value = value
        elif element_type == ELEMENT_VENDOR:
            vendor_value = value
    if wlan_value is None or vendor_value is None or len(wlan_value) < 19:
        return None
    ssid = wlan_value[19:].decode("utf-8", "replace")
    rest = vendor_value[10:]
    suite_len = len(SECURITY_SUITE["psk"])
    pwlen_at = 1 + 1 + 2 + 1 + 1 + suite_len + 1
    if len(rest) < pwlen_at + 1:
        return None
    vlan = int.from_bytes(rest[2:4], "big")
    password = rest[pwlen_at + 1 : pwlen_at + 1 + rest[pwlen_at]].decode("utf-8", "replace")
    hidden = wlan_value[18] == 0x00
    suite = rest[6:6 + suite_len]
    auth = next((k for k, v in AUTH_CODES.items() if v == suite[3]), "auto")
    cipher = next((k for k, v in CIPHER_CODES.items() if v == suite[4]), "aes")
    return WifiNetwork(
        ssid=ssid, password=password, vlan=vlan, security="psk", hidden=hidden, auth=auth, cipher=cipher,
    )


COUNTRIES = ("CN", "HK", "TW", "SG", "JP", "US", "DE", "FR")

_COUNTRY_ELEMENT_TYPE = 1046


def maybe_patch_country(message: bytes, country: str | None) -> bytes:
    if not country:
        return message
    if country not in COUNTRIES:
        raise ValueError(f"unsupported country: {country!r}")
    suffix = country.encode("ascii") + b" \x00"
    out = bytearray(message)
    offset = cc.CONTROL_HEADER
    while offset + 4 <= len(out):
        element_type = int.from_bytes(out[offset : offset + 2], "big")
        length = int.from_bytes(out[offset + 2 : offset + 4], "big")
        value_at = offset + 4
        if element_type == _COUNTRY_ELEMENT_TYPE and length == 16:
            out[value_at + 12 : value_at + 16] = suffix
            return bytes(out)
        offset = value_at + length
    return message


def has_country_element(message: bytes) -> bool:
    offset = cc.CONTROL_HEADER
    while offset + 4 <= len(message):
        element_type = int.from_bytes(message[offset : offset + 2], "big")
        length = int.from_bytes(message[offset + 2 : offset + 4], "big")
        if element_type == _COUNTRY_ELEMENT_TYPE and length == 16:
            return True
        offset = offset + 4 + length
    return False


def seed_networks() -> dict[str, WifiNetwork]:
    return {
        "radio1_wlan1": WifiNetwork(ssid="TPAC_2.4G", enabled=False),
        "radio1_wlan2": WifiNetwork(ssid="TPAC_Guest_2.4G", vlan=4084, enabled=False),
        "radio1_wlan3": WifiNetwork(ssid="TPAC_IoT", hidden=True, enabled=False),
        "radio2_wlan1": WifiNetwork(ssid="TPAC_Guest", enabled=False),
        "radio2_wlan2": WifiNetwork(ssid="TPAC", enabled=False),
    }


DEFAULT_PROFILE = "default"
DEFAULT_SLOTS = {"radio1_wlan1": 0, "radio1_wlan2": 4084, "radio2_wlan2": 0}
SEEDED_PROFILES = {"iot": "radio1_wlan3", "guest": "radio2_wlan1"}


class ProfileStore:

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()
        self._profiles: dict[str, dict[str, WifiNetwork]] = {}
        self._load()
        self._ensure_builtin()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        profiles_raw = raw["profiles"] if "profiles" in raw else raw
        for name, slots in profiles_raw.items():
            self._profiles[name] = {
                slot_name: network_from_json(fields) for slot_name, fields in slots.items()
            }

    def _ensure_builtin(self) -> None:
        seeds = seed_networks()
        existing = self._profiles.get(DEFAULT_PROFILE, {})
        default: dict[str, WifiNetwork] = {}
        for slot, vlan in DEFAULT_SLOTS.items():
            network = existing.get(slot) or seeds.get(slot)
            if network is None:
                network = WifiNetwork(ssid="TPAC", vlan=vlan, enabled=False)
            network.vlan = vlan
            default[slot] = network
        self._profiles[DEFAULT_PROFILE] = default
        for name, slot in SEEDED_PROFILES.items():
            if name not in self._profiles and slot in seeds:
                self._profiles[name] = {slot: seeds[slot]}
        self._save()

    def _save(self) -> None:
        raw = {
            "profiles": {
                name: {slot_name: vars(network) for slot_name, network in slots.items()}
                for name, slots in self._profiles.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
        self._path.chmod(0o600)

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": name,
                    "builtin": name == DEFAULT_PROFILE,
                    "wlans": {
                        slot_name: {
                            "ssid": n.ssid,
                            "vlan": n.vlan,
                            "security": n.security,
                            "auth": n.auth,
                            "cipher": n.cipher,
                            "hidden": n.hidden,
                            "enabled": n.enabled,
                            "band": SLOT_BY_NAME[slot_name].band if slot_name in SLOT_BY_NAME else None,
                        }
                        for slot_name, n in slots.items()
                    },
                }
                for name, slots in sorted(self._profiles.items())
            ]

    def all(self) -> dict[str, dict[str, WifiNetwork]]:
        with self._lock:
            return {name: dict(slots) for name, slots in sorted(self._profiles.items())}

    def get(self, name: str) -> dict[str, WifiNetwork] | None:
        with self._lock:
            return self._profiles.get(name)

    def save(self, name: str, networks: dict[str, WifiNetwork]) -> None:
        if name == DEFAULT_PROFILE:
            raise ValueError("the default profile is edited, not replaced")
        if not name or not all(slot in SLOT_BY_NAME for slot in networks):
            raise ValueError("invalid profile name or slot")
        with self._lock:
            self._profiles[name] = dict(networks)
            self._save()

    def merge(self, name: str, networks: dict[str, WifiNetwork]) -> None:
        if not name or not all(slot in SLOT_BY_NAME for slot in networks):
            raise ValueError("invalid profile name or slot")
        with self._lock:
            existing = self._profiles.get(name, {})
            merged = dict(networks)
            if name == DEFAULT_PROFILE:
                if not set(merged) <= set(DEFAULT_SLOTS):
                    raise ValueError("the default profile has only its three built-in networks")
                merged = {**existing, **merged}
                for slot, network in merged.items():
                    network.vlan = DEFAULT_SLOTS[slot]
                    network.security = "psk"
            for slot, network in merged.items():
                if not network.password and slot in existing:
                    network.password = existing[slot].password
            self._profiles[name] = merged
            self._save()

    def duplicate(self, name: str, new_name: str) -> bool:
        if not new_name or new_name == DEFAULT_PROFILE:
            raise ValueError("new_name is required and cannot be the default profile")
        with self._lock:
            networks = self._profiles.get(name)
            if networks is None:
                return False
            self._profiles[new_name] = dict(networks)
            self._save()
            return True

    def delete(self, name: str) -> bool:
        if name == DEFAULT_PROFILE:
            raise ValueError("the default profile cannot be deleted")
        with self._lock:
            if name not in self._profiles:
                return False
            del self._profiles[name]
            self._save()
            return True
