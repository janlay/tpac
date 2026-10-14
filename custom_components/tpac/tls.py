"""Access point control session."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from . import capwap_control
from . import config_file
from . import control
from . import led
from . import stations
from . import upgrade
from . import wifi_profile as wp

LOG = logging.getLogger("tpac.dtls")


CONFIG_RETRY_SECONDS = 3.0
CONFIG_RETRIES = 5

SendBack = Callable[[bytes], None]


class DtlsSession:

    def __init__(
        self,
        remote: tuple[str, int],
        certificate: Path,
        key: Path,
        send_back: SendBack,
        local_ip: str | bytes = "",
        networks_for: Callable[[], dict | None] | None = None,
        country: str | None = None,
        on_wlan_confirmed: Callable[[str], None] | None = None,
        on_country_confirmed: Callable[[], None] | None = None,
        on_wlan_sent: Callable[[str], None] | None = None,
        on_wlan_failed: Callable[[str, int], None] | None = None,
        on_event: Callable[..., None] | None = None,
        on_stations: Callable[[list[dict]], None] | None = None,
        on_led: Callable[[str], None] | None = None,
        on_report: Callable[[], None] | None = None,
        ap_mac: bytes | None = None,
        ac_identity=None,
    ) -> None:
        from .dtls import DtlsServer

        self.remote = remote
        self.last_seen = time.monotonic()
        self.records_in = 0
        self.records_out = 0
        self.established = False
        self._reset_txid: int | None = None
        self._clear_txid: int | None = None
        self._factory_reset: tuple[int | None, int] | None = None
        self._upload: upgrade.Upload | None = None
        self._upload_start = False
        self._upgrade_reset_txid: int | None = None
        self._send_back = send_back
        self._server = DtlsServer(certificate.read_bytes(), key.read_bytes(), remote)
        self._local_ip = local_ip
        self._networks_for = networks_for or (lambda: None)
        self._on_wlan_confirmed = on_wlan_confirmed or (lambda slot: None)
        self._on_country_confirmed = on_country_confirmed or (lambda: None)
        self._on_event = on_event or (lambda event, title, data=None: None)
        self._on_stations = on_stations or (lambda events: None)
        self._on_led = on_led or (lambda state: None)
        self._on_report = on_report or (lambda: None)
        self._on_wlan_sent = on_wlan_sent or (lambda slot: None)
        self._on_wlan_failed = on_wlan_failed or (lambda slot, code: None)
        self._ac_identity = ac_identity
        self._config = control.ConfigSequence(
            local_ip, country=country, networks=self._networks_for(), ap_mac=ap_mac, identity=ac_identity,
        )
        self._plaintext: list[bytes] = []
        self._awaiting: bytes | None = None
        self._awaiting_at = 0.0
        self._awaiting_tries = 0
        self._push_queue: list[bytes] = []
        self._fragment_id = 0
        LOG.info("DTLS session started for %s", remote)

    def _dtls_data(self) -> dict:
        from .dtls import cookie_for

        server = self._server
        client_random = server.client_random.hex()
        master = server.master_secret.hex()
        return {
            "remote": f"{self.remote[0]}:{self.remote[1]}",
            "cookie": cookie_for(self.remote).hex(),
            "client_random": client_random,
            "server_random": server.server_random.hex(),
            "master_secret": master,
            "keylog": f"CLIENT_RANDOM {client_random} {master}",
        }

    def feed(self, record: bytes) -> None:
        self.last_seen = time.monotonic()
        self.records_in += 1
        from .dtls import CONTENT_ALERT, TlsError

        if record[0] == CONTENT_ALERT:
            body = record[13:15]
            if len(body) >= 2:
                level, description = body[0], body[1]
            else:
                level, description = -1, -1
            names = {0: "close_notify", 40: "handshake_failure", 42: "bad_certificate",
                     46: "certificate_unknown", 47: "illegal_parameter", 80: "internal_error"}
            self._on_event("dtls_alert", "Control channel closed by the AP", {
                "alert": names.get(description, description), "level": level,
            })
            LOG.warning(
                "AP %s DTLS alert: level=%d(%s) description=%d(%s)",
                self.remote,
                level, "warning" if level == 1 else "fatal",
                description, names.get(description, "?"),
            )
            return

        try:
            out = self._server.handle(record)
        except TlsError as error:
            LOG.warning("session %s handshake error: %s", self.remote, error)
            return
        if self._server.client_finished_verified and self._server.state == "hello-sent":
            out = out + self._server.finish()
            self.established = True
            LOG.info("DTLS handshake completed with %s", self.remote)
            self._on_event("dtls_established", "Control channel established", self._dtls_data())
        for item in out:
            self.records_out += 1
            LOG.debug(
                "DTLS out to %s: content=%d len=%d",
                self.remote, item[0], len(item),
            )
            self._send_back(item)
        for chunk in self._server.take_plaintext():
            self._plaintext.append(chunk)
            self._answer_control(chunk)

    def _answer_control(self, message: bytes) -> None:
        mtype = capwap_control.message_type(message)
        name = capwap_control.MESSAGE_NAMES.get(mtype, f"type {mtype}")
        if mtype == capwap_control.RESET_RESPONSE and capwap_control.transaction_id(message) == self._upgrade_reset_txid:
            self._upgrade_reset_txid = None
            code = next((int.from_bytes(v[:4], "big") for t, v in capwap_control.elements(message) if t == 33 and len(v) >= 4), None)
            reason = capwap_control.RESULT_CODES.get(code, "no result code") if code is not None else "no result code"
            self._on_event("upgrade_reset_answered", f"Reset response: {reason}", {"result_code": code})
            return
        if self._upload is not None and mtype in (upgrade.CONFIG_UPDATE_RESPONSE, upgrade.IMAGE_DATA_REQUEST, upgrade.IMAGE_DATA_RESPONSE):
            upload = self._upload
            for out in upload.on_message(message):
                self._send_control(out)
            if upload.state == "resetting":
                self._upgrade_reset_txid = upload.reset_txid
                self._on_event("upgrade_resetting", f"Image sent with {upload.retries} retransmissions; reset requested so the AP installs it",
                               {"blocks": upload.blocks, "retries": upload.retries})
                self._upload = None
            elif upload.state == "failed":
                self._on_event("upgrade_failed", f"Firmware upload failed: {upload.error}")
                self._upload = None
            return
        keepalive = mtype in (capwap_control.WTP_EVENT_REQUEST, 8, 13)
        (LOG.debug if keepalive else LOG.info)("CAPWAP %s from %s: %d bytes", name, self.remote, len(message))

        if mtype in (capwap_control.WTP_EVENT_REQUEST, capwap_control.ECHO_REQUEST):
            self._on_report()
        state = led.parse(message)
        if state is not None:
            self._on_led(state)

        if mtype == capwap_control.WTP_EVENT_REQUEST:
            client_events = stations.parse(message)
            if client_events:
                self._on_stations(client_events)
            for etype, value in capwap_control.elements(message):
                payload = value[8:].hex() if etype == 0x0025 and len(value) > 8 else ""
                if payload and not payload.startswith("0014"):
                    LOG.debug("AP payload type %#06x: %s", int.from_bytes(value[6:8], "big"), payload)

        wlan = control.wlan_response_slot(message)
        if wlan is not None:
            slot, code = wlan
            if self._awaiting is None or self._awaiting[12] != message[12]:
                return
            slot = control.wlan_request_slot(self._awaiting) or slot
            LOG.info(
                "WLAN %s %s by %s (result code %d)", slot,
                "accepted" if code == 0 else "REJECTED", self.remote, code,
            )
            if code == 0:
                self._on_wlan_confirmed(slot)
            else:
                self._on_wlan_failed(slot, code)

        if self._factory_reset is not None and self._answer_factory_reset(message):
            return
        if wlan is not None or control.is_config_update_response(message):
            acknowledged = self._awaiting
            self._awaiting = None
            if acknowledged is not None and wp.has_country_element(acknowledged):
                self._on_country_confirmed()
            nxt = self._config.advance(capwap_control.transaction_id(message))
            if nxt is not None:
                self._send_config(nxt)
                return
            LOG.info("CAPWAP configuration sequence complete for %s", self.remote)
            self._on_event("config_complete", "Configuration complete")
            return

        reply = control.response_for(message, self._local_ip, self._ac_identity)
        if reply is not None:
            self._send_control(reply)
        if mtype == capwap_control.WTP_EVENT_REQUEST and not self._config.started:
            first = self._config.start()
            if first is not None:
                LOG.info("CAPWAP starting configuration updates for %s", self.remote)
                self._on_event("joined", "Joined; sending configuration")
                self._send_config(first)

    def _send_control(self, message: bytes) -> None:
        self._fragment_id = (self._fragment_id + 1) & 0xFFFF
        for part in config_file.fragments(message, self._fragment_id):
            self.records_out += 1
            self._send_back(self._server.application_record(part))

    def _send_config(self, message: bytes) -> None:
        self._send_control(message)
        slot = control.wlan_request_slot(message)
        if slot is not None:
            self._on_wlan_sent(slot)
        self._awaiting = message
        self._awaiting_at = time.monotonic()
        self._awaiting_tries = 1

    def queue_push(self, messages: list[bytes]) -> None:
        self._push_queue.extend(messages)

    def reboot(self, txid: int) -> bool:
        if not self.established:
            return False
        self._reset_txid = txid
        return True

    def factory_reset(self, txid: int) -> bool:
        if not self.established:
            return False
        self._clear_txid = txid
        return True

    def _answer_factory_reset(self, message: bytes) -> bool:
        config_txid, reset_txid = self._factory_reset
        mtype, txid = capwap_control.message_type(message), capwap_control.transaction_id(message)
        if mtype == capwap_control.CONFIG_UPDATE_RESPONSE and txid == config_txid:
            self._awaiting = None
            self._factory_reset = (None, reset_txid)
            self._send_control(capwap_control.factory_reset_request(reset_txid))
            return True
        if mtype == capwap_control.RESET_RESPONSE and txid == reset_txid:
            self._factory_reset = None
            self._send_back(self._server.close_notify())
            return True
        return False

    def start_upload(self, upload: upgrade.Upload) -> bool:
        if not self.established or self._upload is not None:
            return False
        self._upload = upload
        self._upload_start = True
        return True

    def _tick_upload(self) -> None:
        upload = self._upload
        if self._upload_start:
            self._upload_start = False
            self._send_control(upload.start())
            self._on_event("upgrade_started", "Firmware upload started")
        for message in upload.on_timeout():
            self._send_control(message)
        if upload.state == "failed":
            self._on_event("upgrade_failed", f"Firmware upload failed: {upload.error}")
            self._upload = None

    def _drain_push(self) -> None:
        if not self._push_queue:
            return
        queued, self._push_queue = self._push_queue, []
        self._config.extend(queued)
        if self._awaiting is None and self._config.started:
            first = self._config.next()
            if first is not None:
                self._send_config(first)

    def tick(self) -> None:
        if self.established:
            self._drain_push()
            if self._reset_txid is not None:
                self._send_control(capwap_control.reset_request(self._reset_txid))
                self._reset_txid = None
                self._on_event("reboot_requested", "Reboot requested")
            if self._clear_txid is not None:
                self._factory_reset = (self._clear_txid, (self._clear_txid + 1) & 0xFF)
                self._send_config(control.factory_reset_config(self._clear_txid))
                self._clear_txid = None
                self._on_event("reset_requested", "Factory reset requested")
            if self._upload is not None:
                self._tick_upload()
        if self._awaiting is None or not self.established:
            return
        if time.monotonic() - self._awaiting_at < CONFIG_RETRY_SECONDS:
            return
        if self._awaiting_tries >= CONFIG_RETRIES:
            LOG.warning(
                "giving up on a configuration update to %s after %d tries",
                self.remote, self._awaiting_tries,
            )
            self._awaiting = None
            return
        self._awaiting_tries += 1
        self._awaiting_at = time.monotonic()
        LOG.info(
            "retransmitting configuration update 0x%02x (%d bytes) to %s (try %d)",
            self._awaiting[12], len(self._awaiting), self.remote, self._awaiting_tries,
        )
        self._send_control(self._awaiting)

    def take_plaintext(self) -> list[bytes]:
        chunks, self._plaintext = self._plaintext, []
        return chunks

    def close(self) -> None:
        self.established = False
