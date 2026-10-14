"""Wi-Fi clients reported by access points."""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import capwap_control as cc

STA_ASSOC, STA_DISASSOC, STA_IP, STA_STATS = 2, 3, 0x4D, 0x52
STATS_ENTRY = 38
STATION_TTL = 3600.0
DISASSOC_GRACE = 180.0
SILENT_AFTER = 120.0


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def parse(message: bytes) -> list[dict]:
    events: list[dict] = []
    if cc.message_type(message) != cc.WTP_EVENT_REQUEST:
        return events
    for ptype, data in cc.vendor_payloads(message):
        if ptype in (STA_ASSOC, STA_DISASSOC) and len(data) >= 8:
            event = {"op": "assoc" if ptype == STA_ASSOC else "disassoc",
                     "radio": data[0], "wlan": data[1], "mac": _mac(data[2:8])}
            if ptype == STA_ASSOC:
                event["rssi"] = None
                if len(data) >= 16 and 16 + int.from_bytes(data[12:16], "big") == len(data):
                    event["rssi"] = int.from_bytes(data[8:12], "big", signed=True)
            events.append(event)
        elif ptype == STA_IP and len(data) >= 15 and data[10] == 1:
            events.append({"op": "ip", "mac": _mac(data[4:10]), "ip": ".".join(str(b) for b in data[11:15])})
        elif ptype == STA_STATS and len(data) >= 4:
            count = int.from_bytes(data[:4], "big")
            if len(data) == 4 + STATS_ENTRY * count:
                for i in range(count):
                    start = 4 + STATS_ENTRY * i
                    events.append({"op": "seen", "mac": _mac(data[start : start + 6]),
                                   "radio": data[start + 6], "wlan": data[start + 7],
                                   "rssi": int.from_bytes(data[start + 10 : start + 11], "big", signed=True)})
    return events


class StationTable:

    def __init__(self, ttl: float = STATION_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._wall = time.time
        self._by_ap: dict[str, dict[str, dict]] = {}
        self._last_ip: dict[str, str] = {}
        self._lock = threading.Lock()

    def apply(self, identity: str, events: list[dict]) -> list[dict]:
        with self._lock:
            return self._apply(identity, events)

    def _apply(self, identity: str, events: list[dict]) -> list[dict]:
        table = self._by_ap.setdefault(identity, {})
        now = self._clock()
        changes: list[dict] = []
        for event in events:
            mac, op = event["mac"], event["op"]
            if op == "disassoc":
                entry = table.get(mac)
                if entry is not None and entry.get("left") is None:
                    entry["left"] = {"at": self._wall(), "radio": event["radio"], "wlan": event["wlan"]}
                continue
            entry = table.get(mac)
            if entry is None:
                entry = table[mac] = {"mac": mac, "radio": None, "wlan": None, "rssi": None,
                                      "ip": None, "since": None, "first_seen": int(self._wall())}
            if op == "assoc":
                others = [other.pop(mac) for other_identity, other in self._by_ap.items()
                          if other_identity != identity and mac in other]
                sessions = [e[key] for e in [entry, *others] for key in ("since", "first_seen") if e.get(key)]
                entry.update(radio=event["radio"], wlan=event["wlan"], rssi=event["rssi"], left=None)
                entry["since"] = min(sessions, default=int(self._wall()))
            elif op == "seen" and event.get("radio"):
                entry.update(radio=event["radio"], wlan=event["wlan"], rssi=event["rssi"], left=None)
            elif op == "ip":
                entry["ip"] = event["ip"]
                entry["left"] = None
                if self._last_ip.get(mac) != event["ip"]:
                    self._last_ip[mac] = event["ip"]
                    changes.append({"mac": mac, "ip": event["ip"]})
            entry["heard"] = now
        return changes

    def snapshot(self, identity: str) -> list[dict]:
        with self._lock:
            return self._snapshot(identity)

    def departures(self, grace_of: Callable[[str], float] = lambda mac: DISASSOC_GRACE) -> list[dict]:
        with self._lock:
            now = self._wall()
            out = []
            for identity, table in self._by_ap.items():
                for mac, entry in list(table.items()):
                    left = entry.get("left")
                    if left and now - left["at"] >= grace_of(mac):
                        del table[mac]
                        out.append({"identity": identity, "mac": mac, "radio": left["radio"],
                                    "wlan": left["wlan"], "left_at": left["at"]})
            return out

    def _snapshot(self, identity: str) -> list[dict]:
        table = self._by_ap.get(identity, {})
        now = self._clock()
        for mac in [m for m, e in table.items() if now - e["heard"] > self._ttl]:
            del table[mac]
        for entry in table.values():
            silent = now - entry["heard"]
            if entry.get("left") is None and silent > SILENT_AFTER:
                entry["left"] = {"at": self._wall() - silent, "radio": entry["radio"], "wlan": entry["wlan"]}
        return [
            {**{k: v for k, v in entry.items() if k not in ("heard", "left")},
             "slot": f"radio{entry['radio']}_wlan{entry['wlan']}" if entry["radio"] is not None else None}
            for entry in sorted(table.values(), key=lambda e: e["mac"])
        ]

    def last_ips(self) -> dict[str, str]:
        with self._lock:
            return dict(self._last_ip)

    def listed(self, owners: dict[str, str]) -> dict[str, tuple[str, dict]]:
        with self._lock:
            out: dict[str, tuple[str, dict]] = {}
            for identity, table in self._by_ap.items():
                for mac, entry in table.items():
                    if owners.get(mac, identity) == identity:
                        out.setdefault(mac, (identity, entry))
            return out

    def export(self) -> dict[str, list[dict]]:
        with self._lock:
            return {
                identity: [{k: e.get(k) for k in ("mac", "radio", "wlan", "rssi", "ip", "since", "first_seen")}
                           for e in table.values() if e.get("left") is None]
                for identity, table in self._by_ap.items() if table
            }

    def load(self, data: dict[str, list[dict]]) -> None:
        with self._lock:
            now = self._clock()
            for identity, entries in data.items():
                table = self._by_ap.setdefault(identity, {})
                for entry in entries:
                    if entry.get("mac"):
                        table[entry["mac"]] = {"first_seen": int(self._wall()), **entry, "left": None, "heard": now}

    def load_last_ips(self, last_ip: dict[str, str]) -> None:
        with self._lock:
            self._last_ip = dict(last_ip)

    def forget(self, identity: str) -> None:
        with self._lock:
            self._by_ap.pop(identity, None)

    def forget_client(self, mac: str) -> None:
        with self._lock:
            for table in self._by_ap.values():
                table.pop(mac, None)
            self._last_ip.pop(mac, None)
