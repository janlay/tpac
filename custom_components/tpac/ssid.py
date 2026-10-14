"""Wi-Fi networks (SSIDs) as Home Assistant devices, one per name."""

from __future__ import annotations

from collections.abc import Callable


def wlans_by_ssid(coordinator) -> dict[str, dict]:
    """The WLAN definitions of every profile, keyed by SSID.  The first profile that names an SSID wins."""
    found: dict[str, dict] = {}
    for profile in coordinator.data.get("profiles", []):
        for wlan in (profile.get("wlans") or {}).values():
            if wlan.get("ssid"):
                found.setdefault(wlan["ssid"], wlan)
    return found


def access_points(coordinator, ssid: str) -> int:
    """How many adopted access points provide the SSID: one of their profiles has it enabled.

    The default profile always applies to an access point.
    """
    profiles = {profile["name"]: profile.get("wlans") or {} for profile in coordinator.data.get("profiles", [])}
    count = 0
    for device in coordinator.data.get("devices", []):
        if device.get("status") != "adopted":
            continue
        for name in {"default", *(device.get("profiles") or [])}:
            if any(w.get("ssid") == ssid and w.get("enabled", True) for w in profiles.get(name, {}).values()):
                count += 1
                break
    return count


def clients(coordinator, ssid: str) -> int:
    """The Wi-Fi clients connected to the SSID, on any access point, each counted once."""
    return len(
        {
            (station.get("mac") or "").lower()
            for device in coordinator.data.get("devices", [])
            for station in device.get("stations") or []
            if station.get("ssid") == ssid
        }
    )


def unique_id(entry, ssid: str, key: str) -> str:
    return f"{entry.entry_id}_ssid_{ssid}_{key}"


def async_track_ssids(coordinator, entry, async_add_entities, make: Callable[[str], list]) -> None:
    """Create each platform's SSID entities once per SSID name, as profiles add names."""
    known: set[str] = set()

    def add_new() -> None:
        fresh = [ssid for ssid in wlans_by_ssid(coordinator) if ssid not in known]
        known.update(fresh)
        if fresh:
            async_add_entities(entity for ssid in fresh for entity in make(ssid))

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
