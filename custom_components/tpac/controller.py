"""TPAC access controller: discovery, adoption, control sessions, profiles, and clients."""
from __future__ import annotations

import itertools
import json
from collections import deque
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import capwap
from . import capwap_control as cc
from . import control
from . import stations
from . import upgrade
from . import wifi_profile as wp
from .identity import AcIdentity
from .tls import DtlsSession

LOG = logging.getLogger("tpac.controller")

ROAM_ADDRESS_WINDOW = 60.0


def interface_mac(interface: str) -> bytes:
    try:
        raw = Path(f"/sys/class/net/{interface}/address").read_text().strip()
        return bytes(int(part, 16) for part in raw.split(":"))
    except (OSError, ValueError):
        LOG.warning("cannot read MAC of %s", interface)
        return b"\x00" * 6


def interface_ipv4(interface: str) -> bytes:
    try:
        import socket as _socket
        import fcntl
        import struct as _struct

        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as probe:
            packed = fcntl.ioctl(
                probe.fileno(),
                0x8915,
                _struct.pack("256s", interface.encode()[:15]),
            )
        return packed[20:24]
    except (OSError, ImportError):
        LOG.warning("cannot read IPv4 of %s", interface)
        return b"\x00\x00\x00\x00"

DEVICE_TTL = 300.0
STATE_SAVE_INTERVAL = 60.0
JOURNAL_MAX_BYTES = 5 * 1024 * 1024
LED_SETTLE_SECONDS = 45.0
REQUEST_TTL = 120.0
CONTROL_TTL = 120.0
APPLY_TIMEOUT = 240.0
LIVE_APPLY_TIMEOUT = 45.0


def _is_client_hello(record: bytes) -> bool:
    return (
        len(record) > 14
        and record[0] == 22
        and int.from_bytes(record[3:5], "big") == 0
        and record[13] == 1
    )


class Controller:

    def __init__(
        self,
        *,
        ac_name: str,
        ac_firmware: str,
        certificate: Path,
        key: Path,
        interface: str,
        port: int = 5246,
        control_ttl: float = CONTROL_TTL,
        state_dir: Path | None = None,
    ) -> None:
        self.ac_name = ac_name
        self.ac_identity = AcIdentity(model=ac_name, firmware=ac_firmware)
        self.ac_firmware = ac_firmware
        self._certificate = certificate
        self._key = key
        self._control_ttl = control_ttl
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._devices: dict[str, dict] = {}
        self._sessions: dict[tuple[str, int], DtlsSession] = {}
        self._requests: dict[tuple, float] = {}
        self._outbound: dict[bytes, float] = {}
        self._ip_identity: dict[str, str] = {}
        self.on_change = None
        self.on_event = None
        self.last_request_at: str | None = None
        self.counters = {
            "discovery_requests": 0,
            "discovery_responses": 0,
            "duplicate_requests": 0,
            "dtls_records_in": 0,
            "dtls_records_out": 0,
        }
        self._state_path = (state_dir / "tpac-state.json") if state_dir else None
        self._adopted: set[str] = set()
        self._device_profile: dict[str, list[str]] = {}
        self._device_networks: dict[str, dict[str, wp.WifiNetwork]] = {}
        self._device_country: dict[str, str] = {}
        self._device_info: dict[str, dict] = {}
        self._client_ap: dict[str, str] = {}
        self._left_at: dict[str, float] = {}
        self._last_saved = 0.0
        self._presence: dict[str, str] = {}
        self.disassoc_grace = stations.DISASSOC_GRACE
        self.client_grace: dict[str, float] = {}
        self.retention_days = 7
        self._roam_until: dict[str, float] = {}
        self._sync_version = 0x23
        self._apply_status: dict[str, dict] = {}
        self._live_txid: dict[str, int] = {}
        self._stations = stations.StationTable()
        self._led: dict[str, str] = {}
        self._led_desired: dict[str, bool] = {}
        self._led_written: dict[str, float] = {}
        self._joining: set[str] = set()
        self._events: deque[dict] = deque(maxlen=500)
        self._event_seq = 0
        self._load_state()
        self.profiles = wp.ProfileStore(
            (state_dir / "tpac-profiles.json") if state_dir else Path("tpac-profiles.json")
        )
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", port))
        self._local_ip = interface_ipv4(interface)


    def _load_state(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        self._adopted = set(raw.get("adopted", []))
        self._device_profile = {
            identity: [names] if isinstance(names, str) else list(names)
            for identity, names in raw.get("device_profile", {}).items()
        }
        self._device_networks = {
            identity: {slot: wp.network_from_json(fields) for slot, fields in slots.items()}
            for identity, slots in raw.get("device_networks", {}).items()
        }
        self._device_country = dict(raw.get("device_country", {}))
        self._led_desired = {identity: bool(on) for identity, on in raw.get("led_desired", {}).items()}
        self._device_info = dict(raw.get("device_info", {}))
        self._sync_version = int(raw.get("sync_version", self._sync_version))
        self._stations.load_last_ips(raw.get("client_ip", {}))
        stations_saved = raw.get("stations", {})
        self._stations.load(stations_saved)
        for identity, entries in stations_saved.items():
            for entry in entries:
                if entry.get("mac"):
                    self._client_ap.setdefault(entry["mac"], identity)
        self._presence = dict(raw.get("presence", {}))
        if "presence" not in raw:
            self._presence = {entry["mac"]: identity for identity, entries in stations_saved.items()
                              for entry in entries if entry.get("mac")}
        now = time.monotonic()
        for identity in self._adopted:
            info = self._device_info.get(identity)
            if info is not None:
                self._devices[identity] = {
                    **info,
                    "identity": identity,
                    "status": "adopted",
                    "profiles": self._device_profile.get(identity, []),
                    "first_seen": now,
                    "last_seen": now,
                }

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        raw = {
            "adopted": sorted(self._adopted),
            "device_profile": self._device_profile,
            "device_networks": {
                identity: {slot: vars(net) for slot, net in slots.items()}
                for identity, slots in self._device_networks.items()
            },
            "device_country": self._device_country,
            "led_desired": self._led_desired,
            "device_info": self._device_info,
            "sync_version": self._sync_version,
            "client_ip": self._stations.last_ips(),
            "stations": self._stations.export(),
            "presence": dict(self._presence),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(raw, indent=2))
        self._state_path.chmod(0o600)

    def adopt(self, identity: str) -> bool:
        with self._lock:
            if identity not in self._devices:
                return False
            self._adopted.add(identity)
            self._devices[identity]["status"] = "adopted"
            self._snapshot_device_info(identity)
            self._save_state()
        LOG.info("adopted %s", identity)
        self.record(identity, "adopted", "Adopted")
        return True

    _INFO_FIELDS = (
        "protocol", "source_ip", "source_port", "wtp_name", "model",
        "serial_number", "base_mac", "firmware", "hardware",
        "max_radios", "radios_in_use", "mac_type", "mount_type", "channels",
    )

    def _snapshot_device_info(self, identity: str) -> None:
        device = self._devices.get(identity)
        if device is None:
            return
        self._device_info[identity] = {key: device.get(key, "") for key in self._INFO_FIELDS}

    def adopt_or_raise(self, identity: str) -> None:
        from homeassistant.exceptions import ServiceValidationError

        with self._lock:
            discovered = identity in self._devices
        if not discovered:
            raise ServiceValidationError(f"no discovered access point {identity}")
        if not self.adopt(identity):
            raise ServiceValidationError(f"could not adopt {identity}")

    def forget_or_raise(self, identity: str) -> None:
        from homeassistant.exceptions import ServiceValidationError

        if not self.forget(identity):
            raise ServiceValidationError(f"{identity} is not adopted")

    def forget(self, identity: str) -> bool:
        with self._lock:
            if identity not in self._adopted:
                return False
            self._adopted.discard(identity)
            self._device_profile.pop(identity, None)
            self._device_networks.pop(identity, None)
            self._device_country.pop(identity, None)
            self._apply_status.pop(identity, None)
            self._stations.forget(identity)
            self._led.pop(identity, None)
            self._led_desired.pop(identity, None)
            self._joining.discard(identity)
            if identity in self._devices:
                self._devices[identity]["status"] = "pending"
                self._devices[identity]["profiles"] = []
            remote = self._remote_for(identity)
            self._save_state()
        if remote is not None:
            with self._lock:
                session = self._sessions.pop(remote, None)
            if session is not None:
                session.close()
        LOG.info("forgot %s", identity)
        self.record(identity, "forgotten", "Forgotten")
        return True

    def _close_stale_sessions(self, remote: tuple[str, int]) -> None:
        for stale in [r for r in self._sessions if r[0] == remote[0] and r != remote]:
            LOG.info("closing the session of %s on the old port %s", remote[0], stale[1])
            self._sessions.pop(stale).close()

    def _remote_for(self, identity: str) -> tuple[str, int] | None:
        device = self._devices.get(identity)
        if device is None or not device.get("source_ip"):
            return None
        for remote in self._sessions:
            if remote[0] == device["source_ip"]:
                return remote
        return None

    def _merge_profiles(self, names: list[str]) -> dict[str, wp.WifiNetwork] | None:
        networks: dict[str, wp.WifiNetwork] = {}
        for name in names:
            profile = self.profiles.get(name)
            if profile is None:
                return None
            for slot, network in profile.items():
                if slot in networks:
                    band = wp.SLOT_BY_NAME[slot].band
                    slot = next(
                        (c.name for c in wp.SLOTS if c.band == band and c.name not in networks), None
                    )
                    if slot is None:
                        raise ValueError(f"no free {band} slot left for profile {name!r}")
                networks[slot] = network
        return networks

    def delete_profile(self, name: str) -> bool:
        if not self.profiles.delete(name):
            return False
        with self._lock:
            affected = {identity: list(names) for identity, names in self._device_profile.items() if name in names}
        for identity, names in affected.items():
            remaining = [n for n in names if n != name]
            self.assign_profiles(identity, remaining)
            self.record(identity, "profiles_selected", "Profile removed", {"profile": name, "profiles": remaining})
        return True

    def assign_profiles(self, identity: str, names: list[str]) -> bool:
        return self._apply(identity, names, self._device_country.get(identity))

    def set_country(self, identity: str, country: str) -> bool:
        if country not in wp.COUNTRIES:
            raise ValueError(f"unsupported country: {country!r}")
        with self._lock:
            names = list(self._device_profile.get(identity, []))
        return self._apply(identity, names, country)

    def _apply(self, identity: str, names: list[str], country: str | None) -> bool:
        names = [wp.DEFAULT_PROFILE] + [n for n in names if n != wp.DEFAULT_PROFILE]
        networks = self._merge_profiles(names)
        if networks is None:
            return False
        enabled = {slot: n for slot, n in networks.items() if n.enabled}
        old = self._networks_for_identity(identity)
        with self._lock:
            if identity not in self._adopted:
                return False
            old_country = self._device_country.get(identity)
            self._device_profile[identity] = list(names)
            self._device_networks[identity] = networks
            if country:
                self._device_country[identity] = country
            else:
                self._device_country.pop(identity, None)
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            live = session is not None and session.established and country == old_country
            messages: list[bytes] = []
            txids = self._txid_stream(identity)
            if live:
                slots = {
                    slot: {"ssid": n.ssid, "op": "unchanged", "state": "unchanged", "code": None}
                    for slot, n in enabled.items()
                }
                for op, slot, msgs in control.live_changes(old, enabled, txids):
                    network = enabled.get(slot) or old[slot]
                    slots[slot] = {
                        "ssid": network.ssid, "op": op, "state": "configuring", "code": None,
                        "acks": 2 if op == "update" else 1,
                    }
                    messages += msgs
            else:
                slots = {
                    slot: {"ssid": n.ssid, "op": "add", "state": "configuring", "code": None}
                    for slot, n in enabled.items()
                }
            self._apply_status[identity] = {
                "pending_slots": {slot for slot, info in slots.items() if info["state"] == "configuring"},
                "slots": slots,
                "country_pending": bool(country) and not live,
                "started_at": time.monotonic(),
                "timeout": LIVE_APPLY_TIMEOUT if live else APPLY_TIMEOUT,
            }
            if identity in self._devices:
                self._devices[identity]["profiles"] = list(names)
            if not live and session is not None:
                self._sessions.pop(remote, None)
            self._save_state()
        self.record(identity, "profiles_selected", "Profiles selected", {
            "profiles": list(names),
            "mode": "live" if live and messages else "no_change" if live
            else "next_join" if session is None else "rejoin",
        })
        if live:
            LOG.info("pushing %d WLAN message(s) to %s on its live session", len(messages), identity)
            if messages:
                mac = self._ap_mac(identity)
                if mac:
                    self._sync_version += 1
                    self._save_state()
                    messages += control.sync_notify_messages((next(txids), next(txids)), self._sync_version)
                    messages.append(control.config_file_message(next(txids), mac, enabled))
                session.queue_push(messages)
        elif session is not None:
            LOG.info("reconnecting %s to apply profiles %s", identity, names)
            session.close()
        LOG.info("assigned profiles %s to %s", names, identity)
        return True

    def reboot_refusal(self, identity: str) -> str | None:
        with self._lock:
            if identity not in self._adopted:
                return "AP not adopted"
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            if session is None or not session.established:
                return "AP not connected"
        return None

    def reboot(self, identity: str) -> bool:
        with self._lock:
            if identity not in self._adopted:
                return False
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            if session is None or not session.reboot(next(self._txid_stream(identity))):
                return False
        LOG.info("reboot requested for %s", identity)
        self.record(identity, "reboot_requested", "Reboot requested")
        return True

    def push_profile(self, name: str) -> dict:
        results = {}
        with self._lock:
            identities = sorted(self._adopted)
        for identity in identities:
            names = list((self._devices.get(identity) or {}).get("profiles") or [])
            if name != wp.DEFAULT_PROFILE and name not in names:
                continue
            names = [wp.DEFAULT_PROFILE] + [n for n in names if n != wp.DEFAULT_PROFILE]
            try:
                results[identity] = self.assign_profiles(identity, names)
            except ValueError as error:
                results[identity] = str(error)
        LOG.info("profile %s pushed to %d device(s)", name, len(results))
        return results

    PROFILE_FILE_FORMAT = "tpac-profiles"
    PROFILE_FILE_VERSION = 1

    BAND_FLAGS = {"2.4G": "2.4GHz", "5G": "5GHz"}

    @staticmethod
    def _slot_order(name: str) -> list[str]:
        if name == wp.DEFAULT_PROFILE:
            return list(wp.DEFAULT_SLOTS)
        return [slot.name for slot in wp.SLOTS]

    def check_new_profile(self, name: str, networks: dict[str, wp.WifiNetwork]) -> None:
        if self.profiles.get(name) is not None:
            return
        if not networks:
            raise ValueError("a profile needs at least one network")
        for slot, network in networks.items():
            if not network.ssid.strip() or not network.password:
                raise ValueError("every network of a new profile needs an SSID and a password")

    def export_profiles(self) -> dict:
        profiles = {}
        for name, slots in self.profiles.all().items():
            wlans: dict[str, dict] = {}
            for slot in self._slot_order(name):
                if slot not in slots:
                    continue
                network = slots[slot]
                fields = {key: value for key, value in vars(network).items() if key != "ssid"}
                band = wp.SLOT_BY_NAME[slot].band
                entry = wlans.setdefault(network.ssid, {"fields": fields, "bands": set()})
                if entry["fields"] != fields:
                    raise ValueError(f"profile {name!r}: the two networks named {network.ssid!r} differ")
                entry["bands"].add(band)
            profiles[name] = {
                ssid: {
                    "2.4G": "2.4GHz" in entry["bands"],
                    "5G": "5GHz" in entry["bands"],
                    **entry["fields"],
                }
                for ssid, entry in wlans.items()
            }
        return {"format": self.PROFILE_FILE_FORMAT, "version": self.PROFILE_FILE_VERSION, "profiles": profiles}

    def import_profiles(self, data: dict) -> dict:
        if data.get("format") != self.PROFILE_FILE_FORMAT or data.get("version") != self.PROFILE_FILE_VERSION:
            raise ValueError("not a TPAC profile file of a supported version")
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            raise ValueError("profiles must be an object keyed by profile name")
        incoming: dict[str, dict[str, wp.WifiNetwork]] = {}
        for name, wlans in profiles.items():
            if not name or not isinstance(wlans, dict):
                raise ValueError("every profile needs a name and an object of networks keyed by SSID")
            free = self._slot_order(name)
            networks: dict[str, wp.WifiNetwork] = {}
            for ssid, fields in wlans.items():
                if not isinstance(fields, dict) or not ssid:
                    raise ValueError(f"profile {name!r} network {ssid!r} must be an object with an SSID")
                flags = [flag for flag in self.BAND_FLAGS if fields.get(flag) is True]
                if not flags:
                    raise ValueError(f"profile {name!r} network {ssid!r} is on no band")
                if name == wp.DEFAULT_PROFILE and len(flags) > 1:
                    raise ValueError("a network of the default profile is on one band only")
                settings = {k: v for k, v in fields.items() if k not in self.BAND_FLAGS}
                for flag in flags:
                    band = self.BAND_FLAGS[flag]
                    slot = next((s for s in free if wp.SLOT_BY_NAME[s].band == band), None)
                    if slot is None:
                        raise ValueError(f"profile {name!r} has no free {band} slot for {ssid!r}")
                    free.remove(slot)
                    try:
                        networks[slot] = wp.network_from_json({**settings, "ssid": str(ssid)})
                    except TypeError as error:
                        raise ValueError(f"profile {name!r} network {ssid!r}: {error}") from error
            incoming[name] = networks
        written = []
        for name, networks in incoming.items():
            if name == wp.DEFAULT_PROFILE:
                self.profiles.merge(name, networks)
            else:
                self.profiles.save(name, networks)
            written.append(name)
        for name in written:
            self.push_profile(name)
        LOG.info("imported %d profile(s): %s", len(written), ", ".join(written))
        return {"imported": written}

    def set_led(self, identity: str, on: bool) -> bool:
        with self._lock:
            if identity not in self._adopted:
                return False
            self._led_desired[identity] = on
            self._led_written[identity] = time.monotonic()
            self._save_state()
        return self._send_led(identity, on)

    def _session_event(self, identity: str, event: str, title: str, data: dict | None = None) -> None:
        self.record(identity, event, title, data)
        with self._lock:
            if event == "joined":
                self._joining.add(identity)
                return
            if event != "config_complete" or identity not in self._joining:
                return
            self._joining.discard(identity)
            desired = self._led_desired.get(identity)
        if desired is not None:
            LOG.info("restoring the LED of %s to %s after the join", identity, "on" if desired else "off")
            self._send_led(identity, desired)

    def _send_led(self, identity: str, on: bool) -> bool:
        with self._lock:
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            if session is None:
                return False
            session.queue_push([cc.led_write_message(next(self._txid_stream(identity)), on)])
        LOG.info("LED %s requested for %s", "on" if on else "off", identity)
        return True

    def factory_reset(self, identity: str) -> bool:
        with self._lock:
            if identity not in self._adopted:
                return False
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            if session is None or not session.factory_reset(next(self._txid_stream(identity))):
                return False
        LOG.info("factory reset requested for %s", identity)
        self.record(identity, "reset_requested", "Factory reset requested")
        return True

    def upgrade(self, identity: str, image: bytes, record: bytes, identifier: bytes) -> bool:
        txids = itertools.cycle(range(1, 256))
        with self._lock:
            if identity not in self._adopted:
                return False
            remote = self._remote_for(identity)
            session = self._sessions.get(remote) if remote else None
            upload = upgrade.Upload(image, record, identifier, next_txid=lambda: next(txids))
            if session is None or not session.start_upload(upload):
                return False
        LOG.info("firmware upload requested for %s", identity)
        self.record(identity, "upgrade_requested", "Firmware upload requested")
        return True

    def record(self, identity: str, event: str, title: str, data: dict | None = None, at: float | None = None) -> None:
        with self._lock:
            self._event_seq += 1
            record = {"id": self._event_seq, "identity": identity, "event": event, "title": title, "data": data or {}}
            self._events.append(record)
        LOG.info("event %s: %s", identity, title)
        self._journal(identity, event, title, data or {}, at)
        if self.on_change is not None:
            self.on_change()
        if self.on_event is not None:
            self.on_event(record)

    def _journal(self, identity: str, event: str, title: str, data: dict, at: float | None = None) -> None:
        if self._state_path is None:
            return
        line = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at or time.time())), "identity": identity,
                "event": event, "title": title, "data": data}
        try:
            journal = self._state_path.parent / "tpac-events.jsonl"
            if journal.exists() and journal.stat().st_size > JOURNAL_MAX_BYTES:
                journal.replace(journal.parent / "tpac-events.jsonl.1")
            with journal.open("a") as handle:
                handle.write(json.dumps(line, default=str) + "\n")
        except OSError as exc:
            LOG.warning("could not write the event journal: %s", exc)

    def events(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "events": [e for e in self._events if e["id"] > since],
                "last": self._event_seq,
            }

    def _ap_mac(self, identity: str) -> bytes | None:
        mac = (self._devices.get(identity) or {}).get("base_mac", "")
        return bytes.fromhex(mac.replace(":", "")) if len(mac) == 17 else None

    def _txid_stream(self, identity: str):
        while True:
            n = self._live_txid.get(identity, 0)
            self._live_txid[identity] = (n + 1) % 0x50
            yield 0xA0 + n

    def _set_slot(self, identity: str, slot: str, state: str, code: int | None = None) -> None:
        with self._lock:
            status = self._apply_status.get(identity)
            if status is not None and slot in status["slots"]:
                status["slots"][slot].update(state=state, code=code)
                if state == "succeeded":
                    status["pending_slots"].discard(slot)

    def _slot_data(self, identity: str, slot: str) -> dict:
        info = (self._apply_status.get(identity) or {}).get("slots", {}).get(slot, {})
        return {"slot": slot, "ssid": info.get("ssid", ""), "operation": info.get("op", "")}

    def _mark_wlan_sent(self, identity: str, slot: str) -> None:
        data = self._slot_data(identity, slot)
        info = (self._apply_status.get(identity) or {}).get("slots", {}).get(slot, {})
        if info.get("state") in (None, "unchanged"):
            data["operation"] = "join"
            self.record(identity, "wlan_sent", "WLAN request sent", data)
            return
        self._set_slot(identity, slot, "waiting")
        self.record(identity, "wlan_sent", "WLAN request sent", data)

    def _mark_wlan_confirmed(self, identity: str, slot: str) -> None:
        with self._lock:
            info = (self._apply_status.get(identity) or {}).get("slots", {}).get(slot)
            if info is not None and info.get("acks", 1) > 1:
                info["acks"] -= 1
                return
        data = self._slot_data(identity, slot)
        if data["operation"] in ("", "unchanged"):
            self.record(identity, "wlan_succeeded", "WLAN succeeded", data | {"operation": "join", "result_code": 0})
            return
        self._set_slot(identity, slot, "succeeded", 0)
        self.record(identity, "wlan_succeeded", "WLAN succeeded", data | {"result_code": 0})
        with self._lock:
            status = self._apply_status.get(identity)
            if status is not None:
                status["pending_slots"].discard(slot)

    def _mark_wlan_failed(self, identity: str, slot: str, code: int) -> None:
        data = self._slot_data(identity, slot)
        if data["operation"] in ("", "unchanged"):
            data["operation"] = "join"
        else:
            self._set_slot(identity, slot, "failed", code)
        self.record(identity, "wlan_failed", "WLAN failed", data | {
            "result_code": code, "reason": cc.RESULT_CODES.get(code, f"error code {code}"),
        })

    def _mark_country_confirmed(self, identity: str) -> None:
        with self._lock:
            status = self._apply_status.get(identity)
            changed = status is not None and status["country_pending"]
            if status is not None:
                status["country_pending"] = False
        if changed:
            self.record(identity, "country_applied", "Country applied")

    def _identity_for_ip(self, ip: str) -> str | None:
        for identity in self._adopted:
            info = self._device_info.get(identity)
            if info and info.get("source_ip") == ip:
                return identity
        return None

    def _register_pending(self, ip: str) -> str:
        identity = self._ip_identity.get(ip) or self._identity_for_ip(ip) or ip
        self._ip_identity[ip] = identity
        now = time.monotonic()
        existing = self._devices.get(identity)
        if existing is not None:
            existing["last_seen"] = now
            existing["source_ip"] = ip
            return identity
        self._devices[identity] = {
            "protocol": "capwap",
            "source_ip": ip,
            "source_port": 0,
            "wtp_name": "",
            "model": "",
            "serial_number": "",
            "base_mac": "",
            "firmware": "",
            "hardware": "",
            "first_seen": now,
            "last_seen": now,
            "identity": identity,
            "status": "adopted" if identity in self._adopted else "pending",
            "profiles": self._device_profile.get(identity, []),
        }
        if identity in self._adopted:
            self._snapshot_device_info(identity)
            self._save_state()
        return identity

    def _networks_for_identity(self, identity: str) -> dict[str, wp.WifiNetwork]:
        with self._lock:
            networks = self._device_networks.get(identity)
        if networks is None:
            networks = self._merge_profiles([wp.DEFAULT_PROFILE])
        return {slot: n for slot, n in networks.items() if n.enabled}


    def serve_forever(self) -> None:
        LOG.info("CAPWAP controller listening on UDP 5246 as %s", self.ac_name)
        self._socket.settimeout(1.0)
        try:
            while not self._stopping.is_set():
                try:
                    packet, remote = self._socket.recvfrom(65535)
                except socket.timeout:
                    self._maintenance()
                    continue
                except OSError as error:
                    LOG.warning("receiving failed: %s", error)
                    self._maintenance()
                    continue
                try:
                    self.handle(packet, remote)
                except Exception:
                    LOG.exception("handling %s failed", remote)
                self._maintenance()
        finally:
            self._socket.close()

    def stop(self) -> None:
        self._stopping.set()

    def _maintenance(self) -> None:
        if time.monotonic() - self._last_saved >= STATE_SAVE_INTERVAL:
            self._last_saved = time.monotonic()
            self._save_state()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                session.tick()
            except Exception:
                LOG.exception("session tick failed for %s", session.remote)
        self._confirm_departures()
        self._update_presence()
        self.expire()

    def _update_presence(self) -> None:
        with self._lock:
            owners = dict(self._client_ap)
        listed = self._stations.listed(owners)
        for mac, (identity, entry) in listed.items():
            if mac in self._presence:
                continue
            self._presence[mac] = identity
            networks = self._networks_for_identity(identity)
            network = networks.get(f"radio{entry['radio']}_wlan{entry['wlan']}") if entry.get("radio") is not None else None
            self.record(mac, "client_connect", "Connected", {"ap": identity, "ssid": network.ssid if network else None})
        now = time.time()
        for mac in [m for m in self._presence if m not in listed]:
            identity = self._presence.pop(mac)
            with self._lock:
                self._left_at[mac] = now
            self.record(mac, "sta_disassoc", "Disassociated", {"ap": identity, "ssid": None, "left_at": now}, at=now)

    def grace_for(self, mac: str) -> float:
        return self.client_grace.get(mac) or self.disassoc_grace

    def forget_client(self, mac: str) -> None:
        with self._lock:
            self._client_ap.pop(mac, None)
            self._left_at.pop(mac, None)
            self._roam_until.pop(mac, None)
            self._stations.forget_client(mac)

    def save_state(self) -> None:
        self._save_state()

    def is_connected(self, mac: str) -> bool:
        with self._lock:
            return mac in self._presence

    def _confirm_departures(self) -> None:
        for departure in self._stations.departures(self.grace_for):
            identity, mac = departure["identity"], departure["mac"]
            with self._lock:
                if self._client_ap.get(mac) == identity:
                    self._client_ap.pop(mac, None)
                self._left_at[mac] = departure["left_at"]
                self._presence.pop(mac, None)
            self._save_state()
            network = self._networks_for_identity(identity).get(f"radio{departure['radio']}_wlan{departure['wlan']}")
            self.record(
                mac, "sta_disassoc", "Disassociated",
                {"ap": identity, "ssid": network.ssid if network else None, "left_at": departure["left_at"]},
                at=departure["left_at"],
            )

    @property
    def retention_seconds(self) -> float:
        return self.retention_days * 24 * 3600.0

    def left_at(self) -> dict[str, float]:
        with self._lock:
            return dict(self._left_at)

    def handle(self, packet: bytes, remote: tuple[str, int]) -> None:
        if capwap.is_discovery_request(packet):
            self.answer_discovery(packet, remote)
        elif packet.startswith(capwap.DTLS_PREAMBLE):
            self.route_dtls(packet, remote)
        else:
            LOG.info(
                "unrouted %d byte datagram from %s: %s",
                len(packet), remote, packet[:24].hex(),
            )


    def answer_discovery(self, packet: bytes, remote: tuple[str, int]) -> None:
        device = capwap.parse_discovery_request(packet, remote)
        if device is None:
            return
        now = time.monotonic()
        with self._lock:
            identity = device["base_mac"] or device["serial_number"] or remote[0]
            known = self._devices.get(identity)
            device["first_seen"] = known["first_seen"] if known else now
            device["last_seen"] = now
            adopted = identity in self._adopted
            device["identity"] = identity
            device["status"] = "adopted" if adopted else "pending"
            device["profiles"] = self._device_profile.get(identity, [])
            self._devices[identity] = device
            self._ip_identity[remote[0]] = identity
            if adopted:
                self._snapshot_device_info(identity)
                self._save_state()
            request_key = (remote[0], remote[1], packet[:56])
            previous = self._requests.get(request_key)
            if previous is not None and now - previous < REQUEST_TTL:
                self.counters["duplicate_requests"] += 1
                return
            self._requests[request_key] = now
            self.counters["discovery_requests"] += 1
            self.last_request_at = datetime.now(timezone.utc).isoformat()
        if not adopted:
            return
        response = capwap.build_discovery_response(
            packet,
            ac_name=self.ac_identity.name,
            ac_firmware=self.ac_firmware,
            ac_mac=b"",
            ac_address=self._local_ip,
            ac_model=self.ac_identity.model_string,
        )
        if not capwap.validate_discovery_response(response):
            LOG.error("refusing to send a malformed Discovery Response")
            return
        self._socket.sendto(response, remote)
        with self._lock:
            self.counters["discovery_responses"] += 1
        LOG.info(
            "Discovery Response to %s model=%s serial=%s mac=%s",
            remote,
            device["model"] or "?",
            device["serial_number"] or "?",
            device["base_mac"] or "?",
        )


    def route_dtls(self, packet: bytes, remote: tuple[str, int]) -> None:
        records = capwap.dtls_records(packet)
        if not records:
            LOG.warning(
                "dropped %d byte datagram from %s: preamble=%s type=%s head=%s",
                len(packet),
                remote,
                packet[:4].hex(),
                f"0x{packet[4]:02x}" if len(packet) > 4 else "n/a",
                packet[:16].hex(),
            )
            return
        is_client_hello = any(_is_client_hello(r) for r in records)
        if is_client_hello:
            with self._lock:
                existing = self._sessions.get(remote)
                if existing is not None and existing.established:
                    LOG.info("DTLS re-handshake from %s, replacing the session", remote)
                    existing.close()
                    self._sessions.pop(remote, None)
        with self._lock:
            self.counters["dtls_records_in"] += 1
            session = self._sessions.get(remote)
            if session is None:
                identity = self._ip_identity.get(remote[0])
                if identity is None:
                    if not is_client_hello:
                        LOG.info(
                            "ignoring non-ClientHello DTLS-shaped datagram from unknown %s",
                            remote,
                        )
                        return
                    identity = self._register_pending(remote[0])
                if identity not in self._adopted:
                    LOG.warning(
                        "refusing DTLS from unadopted %s (identity=%s)", remote, identity,
                    )
                    return
                session = DtlsSession(
                    remote,
                    self._certificate,
                    self._key,
                    lambda data: self._send_to_ap(remote, data),
                    self._local_ip,
                    networks_for=lambda identity=identity: self._networks_for_identity(identity),
                    country=self._device_country.get(identity),
                    on_wlan_confirmed=lambda slot, identity=identity: self._mark_wlan_confirmed(identity, slot),
                    on_country_confirmed=lambda identity=identity: self._mark_country_confirmed(identity),
                    on_event=lambda event, title, data=None, identity=identity: self._session_event(identity, event, title, data),
                    on_wlan_sent=lambda slot, identity=identity: self._mark_wlan_sent(identity, slot),
                    on_stations=lambda events, identity=identity: self._client_events(identity, events),
                    on_led=lambda state, identity=identity: self._set_led(identity, state),
                    on_report=lambda identity=identity: self._touch_report(identity),
                    on_wlan_failed=lambda slot, code, identity=identity: self._mark_wlan_failed(identity, slot, code),
                    ap_mac=self._ap_mac(identity),
                    ac_identity=self.ac_identity,
                )
                self._sessions[remote] = session
                self._close_stale_sessions(remote)
        for record in records:
            LOG.debug(
                "DTLS in from %s: content=%d hs=%s len=%d head=%s",
                remote,
                record[0],
                record[13] if record[0] == 22 and len(record) > 13 else "-",
                len(record),
                record[:12].hex(),
            )
            session.feed(record)

    def _send_to_ap(self, remote: tuple[str, int], record: bytes) -> None:
        datagram = capwap.dtls_record_header(len(record)) + record
        self._socket.sendto(datagram, remote)
        with self._lock:
            self.counters["dtls_records_out"] += 1


    def expire(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._requests = {
                key: seen for key, seen in self._requests.items() if now - seen <= REQUEST_TTL
            }
            self._devices = {
                key: device
                for key, device in self._devices.items()
                if key in self._adopted or now - device["last_seen"] <= DEVICE_TTL
            }
            stale = [
                remote
                for remote, session in self._sessions.items()
                if now - session.last_seen > self._control_ttl
            ]
            timed_out = [self._ip_identity.get(remote[0]) for remote in stale]
            for remote in stale:
                self._sessions.pop(remote).close()
        for identity in timed_out:
            if identity:
                self.record(identity, "channel_timeout", "Control channel timed out")

    def _apply_slots_locked(self, identity: str) -> dict:
        status = self._apply_status.get(identity)
        if status is None:
            return {}
        timed_out = self._apply_status_locked(identity) == "timed_out"
        out = {}
        for slot, info in status["slots"].items():
            state, code = info["state"], info["code"]
            if timed_out and state in ("configuring", "waiting"):
                state = "timeout"
            message = None
            if state == "failed":
                message = cc.RESULT_CODES.get(code, f"error code {code}")
            out[slot] = {"ssid": info["ssid"], "op": info.get("op", "add"), "state": state, "code": code, "message": message}
        return out

    def _apply_status_locked(self, identity: str) -> str | None:
        status = self._apply_status.get(identity)
        if status is None:
            return None
        if any(s["state"] == "failed" for s in status["slots"].values()):
            return "failed"
        if not status["pending_slots"] and not status["country_pending"]:
            return "confirmed"
        if time.monotonic() - status["started_at"] > status.get("timeout", APPLY_TIMEOUT):
            return "timed_out"
        return "pending"

    def _touch_report(self, identity: str) -> None:
        with self._lock:
            device = self._devices.get(identity)
            if device is not None:
                device["last_report"] = time.time()

    def _client_events(self, identity: str, events: list[dict]) -> None:
        now = time.monotonic()
        networks = self._networks_for_identity(identity)

        def ssid_of(event: dict) -> str | None:
            network = networks.get(f"radio{event['radio']}_wlan{event['wlan']}")
            return network.ssid if network else None

        with self._lock:
            for event in events:
                if event["op"] != "assoc":
                    continue
                mac = event["mac"].lower()
                previous = self._client_ap.get(mac)
                if previous is not None and previous != identity:
                    self._roam_until[mac] = now + ROAM_ADDRESS_WINDOW
        changes = self._stations.apply(identity, events)
        for change in changes:
            mac = change["mac"].lower()
            with self._lock:
                roaming = self._roam_until.get(mac, 0) > now
            if not roaming:
                self.record(mac, "client_ip", "Client address", change)
        if changes:
            self._save_state()
        for event in events:
            mac = event["mac"].lower()
            if event["op"] == "disassoc":
                continue
            if event["op"] != "assoc":
                continue
            with self._lock:
                self._left_at.pop(mac, None)
                previous = self._client_ap.get(mac)
                if previous == identity:
                    continue
                self._client_ap[mac] = identity
                roaming = previous is not None and mac in self._presence
            if roaming:
                self.record(
                    mac, "client_roam", "Roamed",
                    {"ap": identity, "previous_ap": previous, "ssid": ssid_of(event)},
                )

    def _set_led(self, identity: str, state: str) -> None:
        with self._lock:
            previous = self._led.get(identity)
            self._led[identity] = state
            desired = self._led_desired.get(identity)
        if previous is not None and previous != state:
            self.record(identity, "led_changed", f"LED turned {state}", {"from": previous, "to": state})
        with self._lock:
            written = self._led_written.get(identity)
        settling = written is not None and time.monotonic() - written < LED_SETTLE_SECONDS
        if desired is not None and desired != (state == "on") and not settling:
            LOG.info("LED of %s reports %s, restoring %s", identity, state, "on" if desired else "off")
            self._send_led(identity, desired)

    def _stations_locked(self, identity: str) -> list[dict]:
        found = self._stations.snapshot(identity)
        networks = self._networks_for_identity(identity) if found else {}
        out = []
        for station in found:
            if self._client_ap.get(station["mac"], identity) != identity:
                continue
            network = networks.get(station["slot"]) if station["slot"] else None
            out.append({**station, "ssid": network.ssid if network else None})
        return out

    def devices(self) -> list[dict]:
        with self._lock:
            connected_ips = {
                remote[0] for remote, session in self._sessions.items() if session.established
            }
            return [
                {
                    **{key: value for key, value in device.items() if key != "last_seen"},
                    "connected": device.get("source_ip") in connected_ips,
                    "apply_status": self._apply_status_locked(device.get("identity", "")),
                    "apply_slots": self._apply_slots_locked(device.get("identity", "")),
                    "stations": self._stations_locked(device.get("identity", "")),
                    "led": self._led.get(device.get("identity", "")),
                    "country": self._device_country.get(device.get("identity", "")),
                }
                for device in sorted(self._devices.values(), key=lambda item: item["base_mac"])
            ]

    def sessions(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "remote": f"{remote[0]}:{remote[1]}",
                    "records_in": session.records_in,
                    "records_out": session.records_out,
                    "idle_seconds": round(now - session.last_seen, 1),
                }
                for remote, session in sorted(self._sessions.items())
            ]
