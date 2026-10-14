"""CAPWAP discovery and DTLS framing."""
from __future__ import annotations

import re
import struct

from . import capwap_control as cc

DISCOVERY_REQUEST = 1
DISCOVERY_RESPONSE = 2
JOIN_REQUEST = 3
JOIN_RESPONSE = 4
ECHO_REQUEST = 13
ECHO_RESPONSE = 14

MESSAGE_NAMES = {
    DISCOVERY_REQUEST: "discovery_request",
    DISCOVERY_RESPONSE: "discovery_response",
    JOIN_REQUEST: "join_request",
    JOIN_RESPONSE: "join_response",
    5: "configuration_status_request",
    6: "configuration_status_response",
    7: "configuration_update_request",
    8: "configuration_update_response",
    9: "wtp_event_request",
    10: "wtp_event_response",
    11: "change_state_event_request",
    12: "change_state_event_response",
    ECHO_REQUEST: "echo_request",
    ECHO_RESPONSE: "echo_response",
}

ELEMENT_DISCOVERY_TYPE = 1
ELEMENT_AC_NAME = 5
ELEMENT_AC_DESCRIPTOR = 6
ELEMENT_AC_TIMESTAMP = 7
ELEMENT_AC_ADDRESS_IPV4 = 10
ELEMENT_AC_ADDRESS_IPV6 = 11
ELEMENT_CAPWAP_TIMERS = 18
ELEMENT_IMAGE_IDENTIFIER = 26
ELEMENT_LOCATION_DATA = 28
ELEMENT_AC_DTLS_POLICY = 31
ELEMENT_WTP_BOARD_DATA = 38
ELEMENT_WTP_DESCRIPTOR = 39
ELEMENT_WTP_FRAME_TUNNEL_MODE = 41
ELEMENT_AC_PRIMARY_IPV4 = 43
ELEMENT_WTP_MAC_TYPE = 44
ELEMENT_WTP_NAME = 45
ELEMENT_WTP_RADIO_STATISTICS = 46

WTP_DISCOVERY_TYPE = 0
ELEMENT_VENDOR_CONFIG = 0x0025

AP_TYPE = 2
AP_TYPE_VALUE = 0x00040032

TP_LINK_VENDOR = 0x00001234

BOARD_MODEL = 0
BOARD_SERIAL = 1
BOARD_HARDWARE = 2
BOARD_FIRMWARE = 3
BOARD_MAC = 4

DESCRIPTOR_HARDWARE = 0
DESCRIPTOR_VENDOR = 1
DESCRIPTOR_MODEL = 4
DESCRIPTOR_FIRMWARE = 5

ELEMENTS_OFFSET_REQUEST = 21
ELEMENTS_OFFSET_RESPONSE = 16

AC_ADDRESS_INDEX = 6
AC_ADDRESS_COUNT = 5


def control_offset(packet: bytes) -> int | None:
    if len(packet) < ELEMENTS_OFFSET_REQUEST or packet[0] >> 4 != 0 or packet[0] & 0x0F != 0:
        return None
    return 8 if (packet[1] >> 3) * 4 == 8 else None


def capwap_message_type(packet: bytes) -> int | None:
    offset = control_offset(packet)
    if offset is None or len(packet) < offset + 8:
        return None
    return packet[offset + 3] or None


def is_discovery_request(packet: bytes) -> bool:
    return capwap_message_type(packet) == DISCOVERY_REQUEST


RESPONSE_HEADER = bytes.fromhex("001043000000000000000002")
ELEMENTS_OFFSET_RESPONSE = 16


def validate_discovery_response(packet: bytes) -> bool:
    return (
        len(packet) >= ELEMENTS_OFFSET_RESPONSE
        and packet[:12] == RESPONSE_HEADER
    )


def message_name(packet: bytes) -> str:
    if not packet or packet[0] >> 4 != 0:
        return "unclassified"
    if packet[0] & 0x0F == 1:
        return "capwap_dtls"
    message_type = capwap_message_type(packet)
    if message_type is None:
        return "capwap_control_malformed"
    return "capwap_" + MESSAGE_NAMES.get(message_type, "control_unknown")


def message_elements(packet: bytes, *, response: bool = False) -> bytes:
    if control_offset(packet) is None:
        return b""
    offset = ELEMENTS_OFFSET_RESPONSE if response else ELEMENTS_OFFSET_REQUEST
    return packet[offset:]


def parse_elements(payload: bytes) -> list[tuple[int, bytes]]:
    elements = []
    offset = 0
    while offset + 4 <= len(payload):
        element_type = int.from_bytes(payload[offset : offset + 2], "big")
        length = int.from_bytes(payload[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + length > len(payload):
            break
        elements.append((element_type, payload[offset : offset + length]))
        offset += length
    return elements


def parse_vendor_data(value: bytes) -> tuple[int, list[tuple[int, bytes]]]:
    if len(value) < 4:
        return 0, []
    vendor = int.from_bytes(value[:4], "big")
    offset = 4
    fields = []
    while offset + 4 <= len(value):
        data_type = int.from_bytes(value[offset : offset + 2], "big")
        length = int.from_bytes(value[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + length > len(value):
            break
        fields.append((data_type, value[offset : offset + length]))
        offset += length
    return vendor, fields


def parse_wtp_descriptor(value: bytes) -> dict:
    if len(value) < 2:
        return {}
    fields = {"max_radios": value[0], "radios_in_use": value[1]}
    at = 6
    while at + 8 <= len(value):
        kind, length = int.from_bytes(value[at + 4 : at + 6], "big"), int.from_bytes(value[at + 6 : at + 8], "big")
        if at + 8 + length > len(value):
            break
        if kind == 0:
            fields["hardware"] = decode_hardware(value[at + 8 : at + 8 + length])
        elif kind == 1:
            fields["firmware"] = decode_software(value[at + 8 : at + 8 + length])
        at += 8 + length
    return fields


def decode_hardware(value: bytes) -> str:
    if len(value) != 4:
        return ""
    major, minor = struct.unpack(">HH", value)
    return f"{major}.{minor}"


def decode_software(value: bytes) -> str:
    if len(value) != 14:
        return ""
    major, minor, patch, year, month, day, _, release = struct.unpack(">HHHHBBHH", value)
    return f"{major}.{minor}.{patch} Build {year:04d}{month:02d}{day:02d} Rel.{release}"


MAC_TYPE_NAMES = {0: "local", 1: "split", 2: "both"}


def _text(value: bytes) -> str:
    raw = value.split(b"\0", 1)[0]
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace").strip()


def vendor_model(value: bytes) -> str:
    start = value.find(b"TL-")
    if start < 0:
        return ""
    end = start
    while end < len(value) and (value[end] >= 0x20 or value[end] >= 0x80):
        end += 1
    return _text(value[start:end])


def _byte_tlvs(value: bytes) -> list[tuple[int, bytes]]:
    items = []
    offset = 0
    while offset + 2 <= len(value):
        kind, length = value[offset], value[offset + 1]
        offset += 2
        if offset + length > len(value):
            break
        items.append((kind, value[offset : offset + length]))
        offset += length
    return items


MOUNT_TYPE_TLV = 0x0A
CHANNEL_LIST_TLV = 0x07


def _printable_ascii(value: bytes) -> str | None:
    if value and all(0x20 <= b < 0x7F for b in value):
        return value.decode("ascii")
    return None


def parse_vendor_capabilities(value: bytes) -> dict:
    result: dict = {}
    start = value.find(b"TL-")
    if start < 2:
        return result
    for kind, data in _byte_tlvs(value[start - 2 :]):
        if kind == MOUNT_TYPE_TLV and "mount_type" not in result:
            text = _printable_ascii(data)
            if text and text.isalpha() and text.isupper():
                result["mount_type"] = text
        elif kind == CHANNEL_LIST_TLV:
            for inner_kind, inner_data in _byte_tlvs(data):
                if inner_kind != CHANNEL_LIST_TLV or "channels" in result:
                    continue
                text = _printable_ascii(inner_data)
                if not text or not re.fullmatch(r"\d{1,3}(,\d{1,3})*", text):
                    continue
                channels = [int(part) for part in text.split(",")]
                if all(1 <= channel <= 233 for channel in channels):
                    result["channels"] = channels
    return result


def _mac(value: bytes) -> str:
    return ":".join(f"{part:02x}" for part in value) if len(value) == 6 else ""


def parse_discovery_request(packet: bytes, remote: tuple[str, int]) -> dict | None:
    if not is_discovery_request(packet):
        return None
    device = {
        "protocol": "capwap",
        "source_ip": remote[0],
        "source_port": remote[1],
        "wtp_name": "",
        "model": "",
        "serial_number": "",
        "base_mac": "",
        "firmware": "",
        "hardware": "",
        "max_radios": 0,
        "radios_in_use": 0,
        "mac_type": "",
        "mount_type": "",
        "channels": [],
    }
    for element_type, value in parse_elements(message_elements(packet)):
        if element_type == ELEMENT_VENDOR_CONFIG:
            model = vendor_model(value)
            if model:
                device["model"] = model
            capabilities = parse_vendor_capabilities(value)
            if capabilities.get("mount_type") and not device["mount_type"]:
                device["mount_type"] = capabilities["mount_type"]
            if capabilities.get("channels") and not device["channels"]:
                device["channels"] = capabilities["channels"]
            continue
        if element_type in (WTP_DISCOVERY_TYPE, ELEMENT_WTP_BOARD_DATA):
            _, fields = parse_vendor_data(value)
            for data_type, data in fields:
                if data_type == BOARD_SERIAL and not device["serial_number"]:
                    device["serial_number"] = _text(data)
                elif data_type == BOARD_HARDWARE and not device["hardware"]:
                    device["hardware"] = _text(data)
                elif data_type == BOARD_FIRMWARE and not device["firmware"]:
                    device["firmware"] = _text(data)
                elif data_type == BOARD_MAC and not device["base_mac"]:
                    device["base_mac"] = _mac(data)
        elif element_type == ELEMENT_WTP_DESCRIPTOR:
            descriptor = parse_wtp_descriptor(value)
            device["max_radios"] = descriptor.get("max_radios", 0)
            device["radios_in_use"] = descriptor.get("radios_in_use", 0)
            if descriptor.get("hardware") and not device["hardware"]:
                device["hardware"] = descriptor["hardware"]
            if descriptor.get("firmware") and not device["firmware"]:
                device["firmware"] = descriptor["firmware"]
        elif element_type == ELEMENT_WTP_MAC_TYPE and len(value) >= 1:
            device["mac_type"] = MAC_TYPE_NAMES.get(value[0], "")
        elif element_type == ELEMENT_WTP_NAME and not device["wtp_name"]:
            device["wtp_name"] = _text(value)
    if not any((device["wtp_name"], device["model"], device["serial_number"], device["base_mac"])):
        return None
    return device


def _element(element_type: int, value: bytes, *, pad: bool = False) -> bytes:
    padding = b"\0" * (-(len(value) + 2) % 4) if pad else b""
    return struct.pack(">HH", element_type, len(value) + len(padding)) + value + padding


PROLOGUE_LENGTH = 16

ELEMENT_RESPONSE_BOARD_DATA = 0x0001
ELEMENT_RESPONSE_WTP_NAME = 0x0004
ELEMENT_RESPONSE_DESCRIPTOR = 0x0005
ELEMENT_VENDOR_TIMERS = 0x0418

NAME_TYPE = bytes.fromhex("0004")
BOARD_TYPE = bytes.fromhex("0001")
ADDRESS_TYPE = bytes.fromhex("000a")
DESCRIPTOR_TYPE = bytes.fromhex("0005")
TIMERS = bytes.fromhex("04180005000000002f04180005010000003f")


def _tlv(element_type: bytes, value: bytes) -> bytes:
    return element_type + struct.pack(">H", len(value)) + value


PROLOGUE_PREFIX = bytes.fromhex("001043000000000000000002")
PROLOGUE_SUFFIX = bytes.fromhex("8e00")
BODY_LENGTH = 0x8E

REQUEST_TOKEN_OFFSET = 12
RESPONSE_TOKEN_OFFSET = 12


def ac_descriptor(ac_firmware: str, model_string: str, ac_mac: bytes = b"", head: bytes = bytes.fromhex("001201000004003202010004")) -> bytes:
    firmware = ac_firmware.encode()[:28].ljust(28, b"\0")
    mac = ac_mac[:6].ljust(6, b"\0") if ac_mac else b"\x12\x34\x56\x78"
    budget = 73 - 12 - 4 - 28 - 8
    model = model_string.encode()
    if len(mac) < budget:
        model = model[: budget - len(mac)].ljust(budget - len(mac), b"\0")
    else:
        mac = mac[:budget]
        model = b""
    return (
        head
        + mac
        + bytes.fromhex("0005001c")
        + firmware
        + bytes.fromhex("1234567800040011")
        + model
    )


def retag_identity(message: bytes, identity) -> bytes:
    payload = bytearray()
    for etype, value in cc.elements(message):
        if etype == 4:
            value = identity.name.encode()[:12].ljust(12, b"\0")
        elif etype == 1:
            value = ac_descriptor(identity.firmware, identity.model_string, head=value[:12])
        payload += struct.pack(">HH", etype, len(value)) + value
    return cc.build(message[11], message[12], bytes(payload))


def build_discovery_response(
    request: bytes,
    *,
    ac_name: str,
    ac_firmware: str,
    ac_mac: bytes,
    ac_address: bytes = b"\x00\x00\x00\x00",
    ac_model: str | None = None,
) -> bytes:
    name = ac_name.encode()[:12].ljust(12, b"\0")
    board = ac_descriptor(ac_firmware, ac_model or f"{ac_name} v7.0", ac_mac)
    address = ac_address[:4].ljust(4, b"\0") + struct.pack(">H", 4)
    if len(request) >= REQUEST_TOKEN_OFFSET + 2:
        token = request[REQUEST_TOKEN_OFFSET] .to_bytes(1, "big") + b"\x00"
    else:
        token = b"\x01\x00"
    prologue = PROLOGUE_PREFIX + token + PROLOGUE_SUFFIX
    return (
        bytes(prologue)
        + _tlv(NAME_TYPE, name)
        + _tlv(BOARD_TYPE, board)
        + TIMERS
        + _tlv(ADDRESS_TYPE, address)
        + _tlv(DESCRIPTOR_TYPE, b"\0" + name + b"\0")
    )


DTLS_PREAMBLE = b"\x01\x00"
DTLS_PAYLOAD_OFFSET = 4

DTLS_CHANGE_CIPHER_SPEC = 20
DTLS_ALERT = 21
DTLS_HANDSHAKE = 22
DTLS_APPLICATION_DATA = 23
DTLS_CONTENT_TYPES = (
    DTLS_CHANGE_CIPHER_SPEC,
    DTLS_ALERT,
    DTLS_HANDSHAKE,
    DTLS_APPLICATION_DATA,
)


def dtls_record_header(payload_length: int) -> bytes:
    return DTLS_PREAMBLE + b"\x00\x00"


def dtls_records(packet: bytes) -> list[bytes]:
    payload = packet[DTLS_PAYLOAD_OFFSET:]
    records: list[bytes] = []
    offset = 0
    while offset + 13 <= len(payload):
        if payload[offset] not in DTLS_CONTENT_TYPES:
            break
        length = int.from_bytes(payload[offset + 11 : offset + 13], "big")
        end = offset + 13 + length
        if end > len(payload):
            break
        records.append(payload[offset:end])
        offset = end
    return records


def dtls_record(packet: bytes) -> bytes | None:
    offset = DTLS_PAYLOAD_OFFSET
    if len(packet) < offset + 13 or not packet.startswith(DTLS_PREAMBLE):
        return None
    record = packet[offset:]
    return record if record[0] in DTLS_CONTENT_TYPES else None
