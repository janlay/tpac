"""Replies and configuration sequence for the access point control channel."""
from __future__ import annotations

import logging

from . import capwap
from . import capwap_control as cc
from . import config_file as cf
from . import wifi_profile as wp

LOG = logging.getLogger("tpac.control")

RESPONSES = {
    cc.JOIN_REQUEST: cc.JOIN_RESPONSE_TEMPLATE,
    cc.CONFIG_STATUS_REQUEST: cc.CONFIG_STATUS_RESPONSE_TEMPLATE,
    cc.CHANGE_STATE_EVENT_REQUEST: cc.CHANGE_STATE_EVENT_RESPONSE_TEMPLATE,
    cc.WTP_EVENT_REQUEST: cc.WTP_EVENT_RESPONSE_TEMPLATE,
    cc.ECHO_REQUEST: cc.ECHO_RESPONSE_TEMPLATE,
}


def is_well_formed(message: bytes) -> bool:
    if len(message) < cc.CONTROL_HEADER:
        return False
    if cc.message_type(message) not in cc.MESSAGE_NAMES:
        return False
    declared = int.from_bytes(message[13:15], "big")
    return declared == len(message) - 13


def _vendor_field(message: bytes) -> int | None:
    for etype, value in cc.elements(message):
        if etype == 0x0025 and len(value) >= 8:
            return int.from_bytes(value[4:8], "big")
    return None


def is_config_update_response(message: bytes) -> bool:
    return cc.message_type(message) == cc.CONFIG_UPDATE_RESPONSE


def wlan_request_slot(message: bytes) -> str | None:
    if len(message) < 16 or message[9:11] != cc.VENDOR_MARKER or message[11] != cc.WLAN_CONFIG_REQUEST:
        return None
    for etype, value in cc.elements(message):
        if etype in (1024, 1027) and len(value) >= 2:
            return f"radio{value[0]}_wlan{value[1]}"
    return None


def wlan_response_slot(message: bytes) -> tuple[str, int] | None:
    if len(message) < 16 or message[9:11] != cc.VENDOR_MARKER or message[11] != cc.WLAN_CONFIG_RESPONSE:
        return None
    slot = None
    code = -1
    for etype, value in cc.elements(message):
        if etype == 0x0025 and len(value) >= 8:
            found = wp.SLOT_BY_KEY.get(value[8:10] + value[10:])
            if found is not None:
                slot = found.name
        elif etype == 33 and len(value) >= 4:
            code = int.from_bytes(value[:4], "big")
    return (slot or "", code)


STA_ASSOC_REQ = 2
_RESPONSE_MAC = bytes.fromhex("020000000001")


def assoc_response(txid: int, radio: int, wlan: int, mac: bytes) -> bytes:
    reply = bytearray(cc.ASSOC_RESPONSE_TEMPLATE)
    reply[12] = txid
    at = reply.find(_RESPONSE_MAC)
    reply[at - 2 : at + 6] = bytes((radio, wlan)) + mac
    return bytes(reply)


def response_for(
    message: bytes,
    local_ip: str | bytes = "",
    identity=None,
) -> bytes | None:
    mtype = cc.message_type(message)
    if mtype is None:
        return None
    if mtype == cc.WTP_EVENT_REQUEST:
        for ptype, data in cc.vendor_payloads(message):
            if ptype == STA_ASSOC_REQ and len(data) >= 8:
                return assoc_response(cc.transaction_id(message), data[0], data[1], data[2:8])
    template = RESPONSES.get(mtype)
    if mtype == cc.WTP_EVENT_REQUEST and _vendor_field(message) != cc.WTP_EVENT_FULL_FIELD:
        template = cc.WTP_EVENT_RESPONSE_SHORT
    name = cc.MESSAGE_NAMES.get(mtype, f"type {mtype}")
    if template is None:
        LOG.info("CAPWAP %s: no reply needed", name)
        return None
    if not is_well_formed(template):
        LOG.error(
            "refusing to send a malformed %s: %d bytes, length field %d",
            name, len(template), int.from_bytes(template[13:15], "big"),
        )
        return None
    reply = bytearray(cc.retag(template, cc.transaction_id(message)))
    if local_ip:
        _patch_ipv4(reply, local_ip)
    if identity is not None and template is cc.JOIN_RESPONSE_TEMPLATE:
        reply = bytearray(capwap.retag_identity(bytes(reply), identity))
    (LOG.debug if mtype in (cc.WTP_EVENT_REQUEST, 13) else LOG.info)("CAPWAP %s -> %s (%d bytes)", name,
             cc.MESSAGE_NAMES.get(cc.message_type(reply), "?"), len(reply))
    return bytes(reply)


class ConfigSequence:

    def __init__(
        self,
        local_ip: str | bytes = "",
        country: str | None = None,
        networks: dict[str, wp.WifiNetwork] | None = None,
        ap_mac: bytes | None = None,
        identity=None,
    ) -> None:
        self._pending = _build_sequence(networks)
        if networks and ap_mac:
            self._pending.append(config_file_message(_FILE_TXID, ap_mac, networks, identity))
        self._local_ip = local_ip
        self._country = country
        self._started = False
        self.sent = 0
        self._answered: set[int] = set()

    def advance(self, txid: int) -> bytes | None:
        if txid in self._answered:
            return None
        self._answered.add(txid)
        return self.next()

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> bytes | None:
        self._started = True
        return self.next()

    def extend(self, messages: list[bytes]) -> None:
        self._pending.extend(messages)
        self._answered.difference_update(m[12] for m in messages)

    def next(self) -> bytes | None:
        if not self._pending:
            return None
        template = bytearray(self._pending.pop(0))
        if self._local_ip:
            _patch_ipv4(template, self._local_ip)
        message = wp.maybe_patch_country(bytes(template), self._country)
        if not is_well_formed(message):
            LOG.error(
                "skipping a malformed configuration update: %d bytes, length field %d",
                len(message), int.from_bytes(message[13:15], "big"),
            )
            return self.next()
        self.sent += 1
        return message


def config_file_message(txid: int, ap_mac: bytes, networks: dict[str, wp.WifiNetwork], identity=None) -> bytes:
    model = identity.config_model if identity is not None else None
    return cf.message(txid, ap_mac, cf.wireless_file(networks, model))


_SYNC_NAME = b"admin/system/nonmesh_config"


def sync_notify_messages(txids: tuple[int, int], version: int) -> list[bytes]:
    messages = []
    for txid in txids:
        message = bytearray(cc.CONFIG_SYNC_TEMPLATE)
        message[12] = txid
        at = message.find(_SYNC_NAME) - 24
        message[at : at + 4] = (version & 0xFFFFFFFF).to_bytes(4, "big")
        messages.append(bytes(message))
    return messages


FACTORY_RESET_VERSION = 0x10


def factory_reset_config(txid: int) -> bytes:
    return sync_notify_messages((txid,), FACTORY_RESET_VERSION)[0]


_FILE_TXID = 0xF0


def live_changes(
    old: dict[str, wp.WifiNetwork],
    new: dict[str, wp.WifiNetwork],
    txids,
) -> list[tuple[str, str, list[bytes]]]:
    changes: list[tuple[str, str, list[bytes]]] = []
    for slot in old:
        if slot not in new:
            changes.append(("delete", slot, [wp.encode_delete(wp.SLOT_BY_NAME[slot], next(txids))]))
    for slot, network in new.items():
        if slot not in old:
            info = wp.SLOT_BY_NAME[slot]
            messages = [wp.encode(info, next(txids), network)]
            first = next(txids)
            messages += _wlan_update_pair(info, first, second=next(txids))
            changes.append(("add", slot, messages))
        elif old[slot] != network:
            info = wp.SLOT_BY_NAME[slot]
            messages = [wp.encode_delete(info, next(txids)), wp.encode(info, next(txids), network)]
            first = next(txids)
            messages += _wlan_update_pair(info, first, second=next(txids))
            changes.append(("update", slot, messages))
    return changes


def _wlan_update_pair(slot: wp.Slot, txid: int, second: int | None = None) -> list[bytes]:
    enable, detail = (bytearray(cc.CONFIG_UPDATE_SEQUENCE[i]) for i in (_PAIR_AT, _PAIR_AT + 1))
    enable[12] = txid
    enable[-2], enable[-1] = slot.radio_id, slot.wlan_id
    detail[12] = txid + 1 if second is None else second
    detail[-13], detail[-8] = slot.radio_id, slot.wlan_id
    return [bytes(enable), bytes(detail)]


_PAIR_AT = next(i for i, m in enumerate(cc.CONFIG_UPDATE_SEQUENCE) if m[12] == 0x1C)
_EXTRA_TXID = 0x80


def _build_sequence(networks: dict[str, wp.WifiNetwork] | None) -> list[bytes]:
    sequence: list[bytes] = []
    templated = {slot for slot, _ in cc.WLAN_PUSH_BEFORE.values()}
    for message in cc.CONFIG_UPDATE_SEQUENCE:
        push = cc.WLAN_PUSH_BEFORE.get(message[12])
        if push and networks and push[0] in networks:
            sequence.append(wp.encode(wp.SLOT_BY_NAME[push[0]], push[1], networks[push[0]]))
        if networks and message[12] == 0x27:
            txid = _EXTRA_TXID
            for name, network in networks.items():
                if name not in templated:
                    slot = wp.SLOT_BY_NAME[name]
                    sequence.append(wp.encode(slot, txid, network))
                    sequence.extend(_wlan_update_pair(slot, txid + 1))
                    txid += 3
        sequence.append(message)
    return sequence


def _patch_ipv4(message: bytearray, local_ip: str | bytes) -> None:
    if isinstance(local_ip, (bytes, bytearray)):
        packed = bytes(local_ip)[:4]
    else:
        packed = bytes(int(part) for part in local_ip.split("."))
    off = cc.CONTROL_HEADER
    while off + 4 <= len(message):
        etype = int.from_bytes(message[off : off + 2], "big")
        elen = int.from_bytes(message[off + 2 : off + 4], "big")
        if off + 4 + elen > len(message):
            return
        if etype == 0x000A and elen >= 4:
            message[off + 4 : off + 8] = packed
        off += 4 + elen
