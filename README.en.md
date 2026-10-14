<p align="center"><img src="custom_components/tpac/brand/icon.png" alt="TPAC icon" width="128" height="128"></p>

# TPAC

[中文](README.md) | **English**

TPAC is a software access controller (AC) for TP-LINK access points, packaged as a Home Assistant custom integration. It takes over the APs on your LAN, so you can manage them from Home Assistant without a TP-LINK hardware controller.

TPAC is an independent project. It is not affiliated with or endorsed by the TP-LINK company.

## Why TPAC

A TP-LINK hardware controller is one more box to buy, power, and log in to. TPAC does the same job inside the Home Assistant you already run.

- **No extra hardware, and no second router.** The TP-LINK controller is a full router with an AC built in. It is made to be your gateway, so it gets in the way of the network you already have:
  - Wi-Fi clients are expected to sit behind its NAT. Next to your main router, that means double NAT, harder port forwarding, and devices that cannot see each other across the two networks.
  - It runs its own DHCP server, which you must remember to turn off, or it competes with your main router.
  - It is one more device in the path: one more power supply, one more firmware to keep patched, one more admin password, and one more thing that can fail. When it reboots for an update, management of every AP stops.
  - Its limits are fixed by the model you bought, such as how many APs and how many networks it accepts.

  With TPAC you remove that box. Your main router stays the only router, and the APs keep working under Home Assistant.
- **Wi-Fi becomes part of your smart home.** Every AP, SSID, and Wi-Fi client is a Home Assistant device. Use them in automations: turn the AP LEDs off at night, enable the guest network only during a party, or run a script when a phone joins or leaves the Wi-Fi.
- **Presence detection that you already pay for.** TPAC knows which client is on which AP. With one AP per floor or room, you get floor- or room-level presence and roaming events without extra sensors or router polling.
- **History in one place.** Connections, roams, and AP events appear in the Home Assistant logbook and recorder, next to the rest of your home.
- **Profiles instead of per-AP setup.** Define a set of networks once, apply it to any AP, and change it later. Connected APs get the change in seconds, without a restart, and you see the result for each network.
- **Local and private.** No cloud account, no vendor app, no remote access to your network. All state stays in your Home Assistant configuration folder, and you can export the profiles as a file for backup.
- **No single-session web UI.** Manage the APs from any Home Assistant dashboard, on any device, by several people at the same time, or through the Home Assistant API.
- **Open source.** You can read the code, fix it, and keep it working after the vendor stops updating its controller.

A hardware controller is still the better choice if you need AP models that TPAC does not support yet, open or WPA3 networks, or vendor support.

## Features

- Finds access points that look for a controller on the LAN. An AP is controlled only after you confirm its adoption.
- Wi-Fi profiles: named sets of SSIDs (WPA/WPA2-PSK, VLAN, hidden flag) that you apply to one or more APs. Changes go to connected APs at once, with a result for each network.
- One Home Assistant device for each AP, each SSID, and each Wi-Fi client, with connection state, signal strength, address, and roaming events in the activity log.
- AP LED switch, reboot, factory reset, and country code.
- Checks the vendor site for newer AP firmware.
- A sidebar panel for adoption, profiles, and clients.

Tested with TL-7AP5100HC and TL-XAP1502GI-PoE, which the TL-R470GP-AC controller manages. Other models can work but are not verified.

## Before you install

1. **Home Assistant OS or Supervised** on the same LAN (layer 2 segment) as the access points. Container and Core installs work only if Home Assistant uses the host network.
2. **UDP port 5246** must be free on the Home Assistant host. No other app or add-on may use it.
3. **No other controller.** An AP that is connected to a hardware AC stays with that AC. Turn the hardware AC off (or disconnect it), then restart or reset the AP.
4. **APs in another subnet, or a Home Assistant that does not receive the AP broadcasts**, need DHCP option 138 on your DHCP server. See "Set DHCP option 138" below.
5. Find the name of the network interface that faces the APs: **Settings → System → Network**, or run `ha network info` in a terminal. Typical names are `enp0s1`, `eth0`, and `end0`.

### Set DHCP option 138

APs look for a controller by broadcast. Set DHCP option 138 (CAPWAP access controller address) on your DHCP server, so the APs contact Home Assistant directly, when:

- the APs and Home Assistant are in different subnets;
- Home Assistant runs in a virtual machine whose network does not pass LAN broadcasts.

The value is the IPv4 address of Home Assistant (4 bytes). The examples use `192.168.1.10`; use your own address. If you can, set the option only for the AP subnet or the AP leases.

**MikroTik RouterOS**

```
/ip dhcp-server option add name=capwap-ac code=138 value="'192.168.1.10'"
```

Then open the AP network in **IP → DHCP Server → Networks** and add `capwap-ac` to **DHCP Options** (keep the options that are already there).

**OpenWrt**

```bash
uci add_list dhcp.lan.dhcp_option='138,192.168.1.10'
uci commit dhcp
/etc/init.d/dnsmasq restart
```

**dnsmasq (also Pi-hole)**

```
dhcp-option=138,192.168.1.10
```

**ISC Kea (DHCPv4)**

```json
"option-data": [ { "code": 138, "name": "capwap-ac-v4", "data": "192.168.1.10" } ]
```

An AP gets the option at its next lease: restart the AP, or wait for the lease renewal. If the APs are in another subnet, your router must also pass their UDP 5246 traffic to Home Assistant.

## Install

### HACS (recommended)

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/janlay/tpac`, type **Integration**.
3. Search for **TPAC**, open it, and select **Download**.
4. Restart Home Assistant: **Settings → System → ⏻ → Restart Home Assistant**.

### Manual

From a terminal on the Home Assistant host (for example the **Terminal & SSH** app):

```bash
cd /tmp
wget -O tpac.zip https://github.com/janlay/tpac/archive/refs/heads/master.zip
unzip -o tpac.zip
mkdir -p /config/custom_components
rm -rf /config/custom_components/tpac
cp -r tpac-master/custom_components/tpac /config/custom_components/
rm -rf tpac.zip tpac-master
ha core check && ha core restart
```

To update, run the same commands again.

## Set up

1. **Settings → Devices & services → Add integration → TPAC.**
2. Enter the interface name from step 5 above. Only one TPAC entry is allowed.
3. Each AP that looks for a controller appears under **Discovered**. Select **Add** and confirm the adoption. The AP joins within about a minute, and its status changes to `online`.
4. Open **TPAC** in the sidebar. The `default` profile has three networks: 2.4 GHz, 5 GHz, and a 2.4 GHz guest network on VLAN 4084. They are disabled and have no password. Set the SSID and password, enable the networks you want, and select **Apply**.
5. Add more profiles (for example IoT or guest networks) and apply them to the APs that should broadcast them.

## Install with an AI agent

Give your agent (for example Claude Code) SSH access to the Home Assistant host, then paste this prompt:

> Install the TPAC Home Assistant integration from https://github.com/janlay/tpac on my Home Assistant host `<user>@<host>`. Follow the "Before you install" and "Manual" sections of its README:
> 1. Check that UDP 5246 is free on the host (`netstat -ulnp | grep 5246` must print nothing) and tell me the interface names from `ha network info`.
> 2. Copy `custom_components/tpac` from the `master` branch to `/config/custom_components/tpac`, replacing an older copy.
> 3. Run `ha core check`. Only if it passes, run `ha core restart`.
> 4. After the restart, run `ha core logs | grep -i tpac` and report errors.
> Do not change any other file. Do not add the integration entry; I will do that in the UI.

## Actions

| Action | Use |
|---|---|
| `tpac.adopt`, `tpac.forget` | Adopt an AP, or release it |
| `tpac.assign_profiles` | Apply one or more profiles to an AP |
| `tpac.import_profiles`, `tpac.export_profiles` | Copy all profiles to or from a JSON file |
| `tpac.reboot`, `tpac.factory_reset` | Restart an AP, or erase its configuration |
| `tpac.set_country` | Set the country code of an AP |
| `tpac.check_ap_update`, `tpac.upgrade_ap` | Look for newer AP firmware; upload it (experimental) |

See **Developer tools → Actions** for the fields of each action.

## Client names from a MikroTik router

`scripts/sync-routeros-client-names.sh` names the Wi-Fi clients after the comments of your RouterOS DHCP leases. Run it on a computer that can reach both the router and Home Assistant over SSH:

```bash
ROUTER=admin@192.168.88.1 HA=root@homeassistant.local ./scripts/sync-routeros-client-names.sh
```

It writes `/config/tpac_client_names.json`. TPAC picks up the change at once. Run it again after you edit the lease comments. Any other tool can write the same file: a JSON object that maps a lowercase MAC address to a name.

## Data and security

- TPAC keeps its state in `/config/tpac/`. `tpac-profiles.json` holds the Wi-Fi passwords in clear text (file mode 0600). The file from `tpac.export_profiles` holds them too.
- TPAC creates its own controller certificate and key in `/config/tpac/` at first start.
- Entity states and attributes never show Wi-Fi passwords.
- The `dtls_established` event holds the secret of an AP session. Anyone who can read the Home Assistant recorder can decrypt that session.

## Remove

1. Remove the TPAC entry in **Settings → Devices & services**. The APs lose their controller and look for another one.
2. Delete `/config/custom_components/tpac`, and `/config/tpac` if you do not need the profiles. Restart Home Assistant.

## Limits

- Open networks and WPA3-SAE are not supported.
- Only APs with two radios (2.4 GHz and 5 GHz) are verified.
- Firmware upload (`tpac.upgrade_ap`) is experimental.
