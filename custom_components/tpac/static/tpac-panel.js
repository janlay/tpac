// A profile is edited as a list of logical networks, each with its own SSID,
// password and VLAN, plus which bands it broadcasts on. The AP has up to seven
// WLAN slots per band, so each band can carry at
// most seven networks; a network can use either band, both, or (while being
// edited) neither yet.
const SLOTS_PER_RADIO = 7;
const radioSlots = (radio) => Array.from({ length: SLOTS_PER_RADIO }, (_, i) => `radio${radio}_wlan${i + 1}`);
const BAND_SLOTS = {
  "2.4GHz": radioSlots(1),
  "5GHz": radioSlots(2),
};
const BANDS = Object.keys(BAND_SLOTS);
// The built-in "default" profile owns three slots (2.4GHz, the disabled 2.4GHz
// VLAN 4084 network, 5GHz), laid out as the AC does; its fixed VLANs and bands
// come from here and the service enforces the same.
const DEFAULT_PROFILE = "default";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const DEFAULT_NETWORKS = [
  { slot: "radio1_wlan1", label: "2.4GHz" },
  { slot: "radio2_wlan2", label: "5GHz" },
  { slot: "radio1_wlan2", label: "2.4GHz guest (VLAN 4084)", vlan: 4084 },
];
const RESERVED_SLOTS = DEFAULT_NETWORKS.map((n) => n.slot);
// Other profiles are written into the slots "default" leaves free.
const FREE_SLOTS = Object.fromEntries(
  BANDS.map((band) => [band, BAND_SLOTS[band].filter((slot) => !RESERVED_SLOTS.includes(slot))])
);
const MAX_NETWORKS = BANDS.reduce((sum, band) => sum + FREE_SLOTS[band].length, 0);

// Countries a profile may set.  This changes the AP's legal channels and
// transmit power, so it is a fixed, reviewed list, not free text.
const COUNTRIES = ["CN", "HK", "TW", "SG", "JP", "US", "DE", "FR"];

function emptyNetwork() {
  const bands = {};
  for (const band of BANDS) bands[band] = false;
  return { ssid: "", password: "", vlan: "0", hidden: false, auth: "auto", cipher: "aes", bands };
}

/** Turn a profile's per-slot WLANs back into a list of logical networks,
 * merging a 2.4GHz and a 5GHz slot into one dual-band network when they
 * share the same SSID, VLAN and hidden flag (the only signal available,
 * since the API never returns a saved password). */
function networksFromProfile(profile) {
  const wlans = profile.wlans || {};
  const pending = BANDS.flatMap((band) =>
    BAND_SLOTS[band].filter((slot) => wlans[slot]).map((slot) => ({ band, slot, net: wlans[slot] }))
  );
  const networks = [];
  while (pending.length) {
    const first = pending.shift();
    const network = emptyNetwork();
    network.ssid = first.net.ssid;
    network.vlan = String(first.net.vlan ?? 0);
    network.hidden = !!first.net.hidden;
    network.auth = first.net.auth || "auto";
    network.cipher = first.net.cipher || "aes";
    network.bands[first.band] = true;
    const matchIndex = pending.findIndex(
      (p) =>
        p.band !== first.band &&
        p.net.ssid === first.net.ssid &&
        (p.net.vlan ?? 0) === (first.net.vlan ?? 0) &&
        !!p.net.hidden === network.hidden &&
        (p.net.auth || "auto") === network.auth &&
        (p.net.cipher || "aes") === network.cipher
    );
    if (matchIndex >= 0) {
      network.bands[pending[matchIndex].band] = true;
      pending.splice(matchIndex, 1);
    }
    networks.push(network);
  }
  return networks.length ? networks : [emptyNetwork()];
}

/** Assign each network's checked bands to that band's free slots, in order,
 * and silently drop a check past that band's slot limit (the form
 * disables a checkbox before that can happen). */
function wlansFromNetworks(networks) {
  const pools = { "2.4GHz": [...FREE_SLOTS["2.4GHz"]], "5GHz": [...FREE_SLOTS["5GHz"]] };
  const wlans = {};
  for (const network of networks) {
    if (!network.ssid) continue;
    for (const band of BANDS) {
      if (network.bands[band] && pools[band].length) {
        const slot = pools[band].shift();
        wlans[slot] = {
          ssid: network.ssid,
          password: network.password || "",
          vlan: parseInt(network.vlan || "0", 10) || 0,
          security: "psk",
          auth: network.auth || "auto",
          cipher: network.cipher || "aes",
          hidden: !!network.hidden,
        };
      }
    }
  }
  return wlans;
}

const AUTH_OPTIONS = { auto: "WPA-PSK/WPA2-PSK", wpa: "WPA-PSK", wpa2: "WPA2-PSK" };
const CIPHER_OPTIONS = { aes: "AES", tkip: "TKIP", auto: "Auto" };

function slotLabel(slot) {
  for (const band of BANDS) {
    const index = BAND_SLOTS[band].indexOf(slot);
    if (index >= 0) return `${band} #${index + 1}`;
  }
  return slot;
}

// Radio labels by the number of radios the AP reports.  Anything else is "Radio n".
// A device is alive while its AP has reported within this many minutes.
const ALIVE_WITHIN_MINUTES = 1;
// The panel re-renders this often so the status dots follow the clock.

const RADIO_LABELS = {
  2: ["2.4G", "5G"],
  3: ["2.4G", "5G1", "5G2"],
  4: ["2.4G1", "2.4G2", "5G1", "5G2"],
};


// ---------------------------------------------------------------------------
// View.  One custom element renders the whole panel into its light DOM.  Events
// are delegated from the element itself, so they are bound once, not on every
// render.  Elements carry `data-action` (clicks) or `data-field` (form input).
// ---------------------------------------------------------------------------

const ICON = {
  device: "mdi:access-point",
  wifi: "mdi:wifi",
  ok: "mdi:check-circle",
  warn: "mdi:alert-circle",
  pending: "mdi:progress-clock",
  check: "mdi:check",
  edit: "mdi:pencil-outline",
  copy: "mdi:content-copy",
  delete: "mdi:delete-outline",
  add: "mdi:plus",
  download: "mdi:download",
  upload: "mdi:upload",
  open: "mdi:open-in-new",
  ledOn: "mdi:lightbulb-on",
  ledOff: "mdi:lightbulb-off-outline",
};

/** Sort key for an IPv4 address: numeric, so 10.0.0.9 comes before 10.0.0.10.
 * A device without an address sorts last. */
function ipKey(ip) {
  if (!ip) return Number.MAX_SAFE_INTEGER;
  return ip.split(".").reduce((n, octet) => n * 256 + Number(octet), 0);
}

/** Identifies one button's action across re-renders.  Volatile values (such as an LED's
 * data-on) are left out, so a button stays disabled while its action runs. */
const ADOPT_TIMEOUT_MS = 2 * 60 * 1000;
// A reboot or factory reset is finished when the AP completes its next configuration.
const RESTART_TIMEOUT_MS = 5 * 60 * 1000;
// How long the card shows that the AP is back, before the line goes away.
const RESTART_DONE_MS = 15 * 1000;
// Actions that stay available on a card while its AP restarts.
const UNLOCKED_ACTIONS = new Set(["open-device", "details-toggle"]);

function actionKey(d) {
  return [d.action, d.identity, d.name, d.entity, d.list, d.index].join("|");
}

function plural(n, word) {
  return n === 1 ? word : `${word}s`;
}

function icon(name, extra = "") {
  return `<ha-icon class="ico ${extra}" icon="${name}"></ha-icon>`;
}

class TpacPanel extends HTMLElement {
  constructor() {
    super();
    // Transient UI state.  It survives the coordinator refresh, which re-renders
    // the whole view on every update.
    this._ui = {
      networkOpen: new Set(), // SSID names whose client list is expanded
      offlineOpen: false, // the Offline client list is expanded
      newProfileOpen: false,
      draftName: "",
      draftNetworks: [emptyNetwork()],
      duplicating: null,
      duplicateValue: "",
      editing: null,
      editNetworks: [],
      editDefault: [],
      managing: new Set(),
      countryDialog: null, // { identity, value } while the country dialog is open
      pendingProfile: new Map(),
      detailsOpen: new Set(),
      busy: new Set(),
      // identity -> { since, state }: an adoption in progress ("waiting") or timed out ("timeout").
      adopting: new Map(),
      // identity -> { kind, since, phase }: a reboot or factory reset in progress.
      // kind is "reboot" or "reset"; phase is "sending", "sent", "joining", "configuring", "done", or "timeout".
      restarting: new Map(),
      inFlight: new Set(), // actions whose click is still running; their buttons stay disabled
      pushing: null,
    };
    this._profilesCache = [];
    this.addEventListener("click", (event) => this._onClick(event));
    this.addEventListener("input", (event) => this._onInput(event));
    this.addEventListener("change", (event) => this._onChange(event));
  }

  set hass(hass) {
    this._hass = hass;
    this._subscribe();
    this.render();
  }

  connectedCallback() {
    this._subscribe();
    this.render();
  }

  disconnectedCallback() {
    this._unsubscribe?.then((unsub) => unsub()).catch(() => {});
    this._unsubscribe = null;
    clearInterval(this._ticker);
    this._ticker = null;
  }

  /** Follows TPAC events, which tell when an AP that restarts is back. */
  _subscribe() {
    if (this._unsubscribe || !this._hass?.connection || !this.isConnected) return;
    this._unsubscribe = this._hass.connection.subscribeEvents((event) => this._onTpacEvent(event.data || {}), "tpac_event");
    this._unsubscribe.catch(() => { this._unsubscribe = null; });
  }

  _onTpacEvent({ event, identity }) {
    const entry = this._ui.restarting.get(identity);
    if (!entry || !["sent", "joining", "configuring"].includes(entry.phase)) return;
    const next = { dtls_established: "joining", joined: "configuring", config_complete: "done" }[event];
    if (!next) return;
    this._ui.restarting.set(identity, { ...entry, phase: next, at: Date.now() });
    this.render();
  }

  // ---- events --------------------------------------------------------------

  async _onClick(event) {
    const el = event.target.closest("[data-action]");
    if (!el || el.disabled) return;
    const d = el.dataset;
    const key = actionKey(d);
    if (this._ui.inFlight.has(key)) return;
    this._ui.inFlight.add(key);
    el.disabled = true;
    try {
      await this._runAction(d);
    } finally {
      this._ui.inFlight.delete(key);
      this.render();
    }
  }

  /** Runs one click's action; the button stays disabled until the returned promise settles. */
  async _runAction(d) {
    const actions = {
      "open-device": () => this._openDevice(d.entity),
      "open-client": () => this._openClient(d.mac),
      "open-network": () => this._openNetwork(d.ssid),
      adopt: () => this._adopt(d.identity),
      reboot: () => this._restart("reboot", d.identity,
        "Reboot this AP? Its Wi-Fi drops for about two minutes while it restarts."),
      "factory-reset": () => this._restart("reset", d.identity,
        "Factory reset this AP? Its configuration is erased. It restarts, rejoins TPAC, and gets its profiles again in about two minutes."),
      forget: () => this._confirmCall(
        "Forget this device? Its control channel session will be dropped.",
        "forget", { identity: d.identity }, `forget:${d.identity}`),
      "manage-toggle": () => this._toggle(this._ui.managing, d.identity),
      "led-toggle": () => this._hass.callService("switch", d.on === "true" ? "turn_on" : "turn_off", { entity_id: d.entity }),
      "country-open": () => this._openCountry(d.identity, d.country),
      "country-cancel": () => { this._ui.countryDialog = null; this.render(); },
      "country-apply": () => this._applyCountry(),
      "details-toggle": () => this._toggle(this._ui.detailsOpen, d.identity),
      "network-toggle": () => this._toggle(this._ui.networkOpen, d.ssid),
      "offline-toggle": () => { this._ui.offlineOpen = !this._ui.offlineOpen; this.render(); },
      "select-profile": () => this._selectProfile(d.identity, d.name, JSON.parse(d.applied)),
      "apply-now": () => this._applyProfile(d.identity),
      "duplicate-open": () => { this._ui.duplicating = d.name; this._ui.duplicateValue = `${d.name} copy`; this.render(); },
      "duplicate-confirm": () => this._duplicateConfirm(d.name),
      "duplicate-cancel": () => { this._ui.duplicating = null; this.render(); },
      "edit-open": () => this._editOpen(d.name),
      "edit-cancel": () => { this._ui.editing = null; this.render(); },
      "edit-save": () => this._editSave(d.name),
      "delete-profile": () => this._deleteProfile(d.name),
      "new-profile-toggle": () => this._newProfileToggle(),
      "profiles-export": () => this._exportProfiles(),
      "profiles-import": () => this._importProfiles(),
      "new-profile-save": () => this._newProfileSave(),
      "add-network": () => this._addNetwork(d.list),
      "remove-network": () => this._removeNetwork(d.list, parseInt(d.index, 10)),
    };
    return actions[d.action]?.();
  }

  _onInput(event) {
    const el = event.target;
    if (!el.dataset.field) return;
    const { field, list, index } = el.dataset;
    if (field === "draftName") this._ui.draftName = el.value;
    else if (field === "duplicateValue") this._ui.duplicateValue = el.value;
    else if (list) this._ui[list][parseInt(index, 10)][field] = el.value;
  }

  _onChange(event) {
    const el = event.target;
    const { field, list, index, band } = el.dataset;
    if (field === "countryPick") {
      this._ui.countryDialog.value = el.value;
    } else if (band) {
      this._ui[list][parseInt(index, 10)].bands[band] = el.checked;
      this.render();
    } else if (list && field) {
      const value = el.type === "checkbox" ? el.checked : el.value;
      this._ui[list][parseInt(index, 10)][field] = value;
      if (el.type === "checkbox" || el.tagName === "SELECT") this.render();
    }
  }

  _toggle(set, key) {
    if (set.has(key)) set.delete(key);
    else set.add(key);
    this.render();
  }

  // ---- service calls -------------------------------------------------------

  /** Asks TPAC to adopt the AP and shows the progress until the AP joins. */
  async _adopt(identity) {
    this._ui.adopting.set(identity, { since: Date.now(), state: "waiting" });
    this.render();
    try {
      await this._hass.callService("tpac", "adopt", { identity });
    } catch (err) {
      this._ui.adopting.delete(identity);
      alert(err.message || String(err));
    }
    this.render();
  }

  /** Asks TPAC to reboot or factory reset the AP, then shows the progress until the AP is configured again. */
  async _restart(kind, identity, message) {
    if (!confirm(message)) return;
    this._ui.restarting.set(identity, { kind, since: Date.now(), phase: "sending" });
    this.render();
    try {
      await this._hass.callService("tpac", kind === "reset" ? "factory_reset" : "reboot", { identity });
      const entry = this._ui.restarting.get(identity);
      if (entry?.phase === "sending") this._ui.restarting.set(identity, { ...entry, phase: "sent" });
    } catch (err) {
      this._ui.restarting.delete(identity);
      alert(err.message || String(err));
    }
    this.render();
  }

  /** The restart progress of one access point, or null.  While it is active, the card is locked. */
  _restartProgress(identity) {
    const entry = this._ui.restarting.get(identity);
    if (!entry) return null;
    const what = entry.kind === "reset" ? "Factory reset" : "Reboot";
    const now = Date.now();
    if (entry.phase === "done") {
      if (now - entry.at > RESTART_DONE_MS) {
        this._ui.restarting.delete(identity);
        return null;
      }
      return { active: false, state: "done", text: `${what} done. The AP is back and configured.` };
    }
    if (entry.phase === "timeout" || now - entry.since > RESTART_TIMEOUT_MS) {
      this._ui.restarting.set(identity, { ...entry, phase: "timeout" });
      return { active: false, state: "timeout", text: `${what}: the AP did not come back within 5 minutes. Check its power and cable.` };
    }
    const seconds = Math.floor((now - entry.since) / 1000);
    const elapsed = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    const text = {
      sending: `${what}: sending the request…`,
      sent: `${what} sent. The AP is restarting…`,
      joining: `${what}: the AP is back and joins TPAC…`,
      configuring: `${what}: sending the configuration…`,
    }[entry.phase];
    return { active: true, state: "busy", text: `${text} ${elapsed}` };
  }

  /** Re-renders every few seconds while an AP restarts, so the elapsed time and the timeout move on. */
  _tick() {
    const running = this._ui.restarting.size > 0;
    if (running && !this._ticker) this._ticker = setInterval(() => this.render(), 5000);
    if (!running && this._ticker) {
      clearInterval(this._ticker);
      this._ticker = null;
    }
  }

  async _call(service, data, busyKey) {
    if (busyKey) this._ui.busy.add(busyKey);
    this.render();
    try {
      await this._hass.callService("tpac", service, data);
    } catch (err) {
      // The service explains a refusal, e.g. an AP that is not connected cannot be rebooted.
      alert(err.message || String(err));
    } finally {
      if (busyKey) this._ui.busy.delete(busyKey);
      this.render();
    }
  }

  _confirmCall(message, service, data, busyKey) {
    if (!confirm(message)) return;
    return this._call(service, data, busyKey);
  }

  async _openDevice(entityId) {
    try {
      const entry = await this._hass.callWS({ type: "config/entity_registry/get", entity_id: entityId });
      if (entry?.device_id) {
        this._hass.navigate(`/config/devices/device/${entry.device_id}`);
        return;
      }
    } catch (err) {
      // No registry access: fall through to the more-info dialog.
    }
    this._moreInfo(entityId);
  }

  /** One entity per client MAC, named by MAC until renamed in Home Assistant. */
  async _openClient(mac) {
    const key = String(mac).toLowerCase();
    let entities = [];
    try {
      entities = await this._hass.callWS({ type: "config/entity_registry/list" });
    } catch (err) {
      return;
    }
    const entry = entities.find((e) => e.platform === "tpac" && (e.unique_id || "").endsWith(`_client_${key}`));
    if (entry) this._moreInfo(entry.entity_id);
  }

  /** The SSID's status entity holds its device's details: state, band, and client count. */
  async _openNetwork(ssid) {
    let entities = [];
    try {
      entities = await this._hass.callWS({ type: "config/entity_registry/list" });
    } catch (err) {
      return;
    }
    const entry = entities.find((e) => e.platform === "tpac" && (e.unique_id || "").endsWith(`_ssid_${ssid}_status`));
    if (entry) this._moreInfo(entry.entity_id);
  }

  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  // ---- devices -------------------------------------------------------------

  _selectProfile(identity, name, applied) {
    if (!identity || name === DEFAULT_PROFILE) return;
    const next = new Set(this._ui.pendingProfile.get(identity) || applied);
    if (!next.delete(name)) next.add(name);
    const same = next.size === applied.length && applied.every((n) => next.has(n));
    if (same) this._ui.pendingProfile.delete(identity);
    else this._ui.pendingProfile.set(identity, [...next]);
    this.render();
  }

  async _applyProfile(identity) {
    const names = this._ui.pendingProfile.get(identity);
    if (!names) return;
    this._ui.pendingProfile.delete(identity);
    try {
      // An empty list is a deliberate "clear all profiles", not a no-op.
      await this._call("assign_profiles", { identity, names }, `apply:${identity}`);
    } catch (err) {
      this._ui.pendingProfile.set(identity, names);
      this.render();
      alert(err?.message || String(err));
    }
  }

  // ---- profiles ------------------------------------------------------------

  _duplicateConfirm(name) {
    const newName = this._ui.duplicateValue.trim();
    if (!newName) return;
    this._ui.duplicating = null;
    this._call("duplicate_profile", { name, new_name: newName }, `duplicate:${name}`);
  }

  _editOpen(name) {
    const profile = this._profilesCache.find((p) => p.name === name);
    if (!profile) return;
    this._ui.editing = profile.name;
    this._ui.editDefault = DEFAULT_NETWORKS.map((n) => {
      const net = (profile.wlans || {})[n.slot] || {};
      return { slot: n.slot, ssid: net.ssid || "", password: "", enabled: net.enabled !== false, hidden: !!net.hidden };
    });
    this._ui.editNetworks = networksFromProfile(profile);
    this.render();
  }

  _defaultWlans() {
    return Object.fromEntries(
      this._ui.editDefault.map((n) => [
        n.slot,
        {
          ssid: n.ssid,
          password: n.password,
          vlan: DEFAULT_NETWORKS.find((d) => d.slot === n.slot).vlan || 0,
          security: "psk",
          hidden: n.hidden,
          enabled: n.enabled,
        },
      ])
    );
  }

  _editSave(name) {
    const data = name === DEFAULT_PROFILE
      ? { name, wlans: this._defaultWlans() }
      : { name, wlans: wlansFromNetworks(this._ui.editNetworks) };
    this._ui.editing = null;
    this._runProfileChange(`Saving "${name}"`, this._profileUsers(name), "set_profile", data);
  }

  _deleteProfile(name) {
    if (!confirm(`Delete profile "${name}"? This cannot be undone. Devices that use it get the remaining profiles at once.`)) return;
    this._runProfileChange(`Deleting "${name}"`, this._profileUsers(name), "delete_profile", { name });
  }

  /** Devices a profile change reaches: every adopted device for "default",
   * otherwise the adopted devices that select the profile. */
  _profileUsers(name) {
    return Object.values(this._hass.states)
      .filter((e) =>
        e.attributes?.protocol === "capwap" &&
        e.attributes?.status === "adopted" &&
        (name === DEFAULT_PROFILE || (e.attributes.profiles || []).includes(name))
      )
      .map((e) => e.entity_id);
  }

  /** Runs one profile change on the service and shows its progress until each
   * connected AP has answered.  Offline APs get the change at their next join. */
  async _runProfileChange(title, ids, service, data) {
    this._ui.pushing = { title, ids, results: [], phase: "saving" };
    this.render();
    try {
      await this._hass.callService("tpac", service, data);
    } catch (err) {
      this._ui.pushing = null;
      this.render();
      alert(err?.message || String(err));
      return;
    }
    this._ui.pushing.phase = "pushing";
    this.render();
    await this._watchPush();
  }

  /** Poll the APs until the connected ones answer, then keep the result visible for three seconds. */
  async _watchPush() {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      await sleep(2000);
      const devices = this._ui.pushing.ids.map((id) => this._hass.states[id]).filter(Boolean);
      this._ui.pushing.results = devices.map((e) => ({
        name: e.attributes.friendly_name || e.entity_id,
        connected: !!e.attributes.connected,
        status: e.attributes.apply_status || "unknown",
      }));
      this.render();
      if (!this._ui.pushing.results.some((r) => r.connected && r.status === "pending")) break;
    }
    this._ui.pushing.phase = "done";
    this.render();
    await sleep(3000);
    this._ui.pushing = null;
    this._ui.editing = null;
    this.render();
  }

  /** Saves every profile as a JSON file.  The file holds the Wi-Fi passwords in clear text. */
  async _exportProfiles() {
    if (!confirm("The file holds the Wi-Fi passwords in clear text. Export anyway?")) return;
    try {
      const result = await this._hass.callWS({
        type: "call_service", domain: "tpac", service: "export_profiles", service_data: {}, return_response: true,
      });
      const url = URL.createObjectURL(new Blob([JSON.stringify(result.response, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "tpac-profiles.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  /** Imports a profile file.  A profile with the same name as a saved one replaces it. */
  _importProfiles() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        await this._hass.callService("tpac", "import_profiles", { data });
      } catch (err) {
        alert(err.message || String(err));
      }
    };
    input.click();
  }

  _newProfileToggle() {
    this._ui.newProfileOpen = !this._ui.newProfileOpen;
    if (this._ui.newProfileOpen) {
      this._ui.draftNetworks = [emptyNetwork()];
    }
    this.render();
  }

  _newProfileSave() {
    const name = this._ui.draftName.trim();
    if (!name) return;
    if (this._ui.draftNetworks.some((n) => !n.ssid.trim() || !n.password)) {
      alert("Every network needs an SSID and a password.");
      return;
    }
    const wlans = wlansFromNetworks(this._ui.draftNetworks);
    if (Object.keys(wlans).length === 0) {
      alert("A profile needs at least one network.");
      return;
    }
    const data = { name, wlans };
    this._ui.newProfileOpen = false;
    this._ui.draftName = "";
    this._call("set_profile", data, "set_profile");
  }

  _addNetwork(list) {
    if (this._ui[list].length >= MAX_NETWORKS) return;
    this._ui[list].push(emptyNetwork());
    this.render();
  }

  _removeNetwork(list, index) {
    this._ui[list].splice(index, 1);
    if (this._ui[list].length === 0) this._ui[list].push(emptyNetwork());
    this.render();
  }

  // ---- rendering -----------------------------------------------------------

  render() {
    if (!this._hass) return;
    const entities = Object.values(this._hass.states);
    const clientEntities = entities.filter((e) => e.entity_id.startsWith("sensor.") && e.attributes?.mac && !e.attributes.identity);
    this._clientIcons = new Map(
      clientEntities.filter((e) => e.attributes.icon).map((e) => [String(e.attributes.mac).toLowerCase(), e.attributes.icon])
    );
    this._clientNames = new Map(
      clientEntities.map((e) => [String(e.attributes.mac).toLowerCase(), e.attributes.friendly_name || e.attributes.mac])
    );
    // An SSID's status entity carries its icon and SSID name.
    this._networkIcons = new Map(
      entities
        .filter((e) => e.entity_id.startsWith("sensor.") && e.attributes?.ssid)
        .map((e) => [e.attributes.ssid, e.attributes.icon])
    );
    const offline = clientEntities
      .filter((e) => e.state === "disconnected")
      .sort((a, b) => String(a.attributes.mac).localeCompare(String(b.attributes.mac)));
    const devices = entities
      .filter(
        (entity) =>
          entity.entity_id.startsWith("sensor.") && entity.attributes.identity
      )
      .sort((a, b) => ipKey(a.attributes.source_ip) - ipKey(b.attributes.source_ip));
    this._apNames = new Map(devices.map((d) => [d.attributes.identity, d.attributes.friendly_name || d.entity_id]));
    // Keyed by the access point name that a client row shows.
    this._apIcons = new Map(
      devices.filter((d) => d.attributes.icon).map((d) => [d.attributes.friendly_name || d.entity_id, d.attributes.icon])
    );
    const service = this._sensor(entities, "_version");
    const profiles = [...(service?.attributes.profiles || [])].sort((a, b) => (b.builtin ? 1 : 0) - (a.builtin ? 1 : 0));
    this._profilesCache = profiles;
    this.innerHTML = this._styles() + this._page({
      devices,
      profiles,
      offline,
      newClients: entities.find((entity) => entity.entity_id.endsWith("_new_wi_fi_clients"))?.state,
      packets: this._sensor(entities, "_frames_received")?.state,
      sources: this._sensor(entities, "_observed_neighbour_addresses")?.state,
      version: service?.state ?? "unknown",
    });
    for (const el of this.querySelectorAll("[data-action]")) {
      if (this._ui.inFlight.has(actionKey(el.dataset))) el.disabled = true;
      if (el.closest("[data-locked]") && !UNLOCKED_ACTIONS.has(el.dataset.action)) el.disabled = true;
    }
    this._tick();
  }

  _sensor(entities, suffix) {
    return entities.find((entity) => entity.entity_id.startsWith("sensor.tpac_") && entity.entity_id.endsWith(suffix));
  }

  _page({ devices, profiles, offline, newClients, packets, sources, version }) {
    return `<div class="page">
      <header class="page-head">
        <div>
          <h1>TPAC</h1>
          <p>Software access controller for TP-LINK access points</p>
        </div>
        <div class="build mono">v${this.escape(version)}</div>
      </header>

      <section class="overview">
        ${this._stat("New Wi-Fi clients", newClients ?? "—")}
        ${this._stat("CAPWAP packets", packets ?? "—")}
        ${this._stat("Packet sources", sources ?? "—")}
      </section>

      <section class="block">
        ${this._networksSection(devices, profiles, offline)}
      </section>

      <section class="block">
        ${this._sectionHead("Access points", devices.length,
          "Discovery does not adopt a device. Adopt it to accept its control channel.")}
        <div class="grid">${devices.length
          ? devices.map((entity) => this._deviceCard(entity, profiles)).join("")
          : this._empty("No access points discovered yet. The list updates from CAPWAP discovery.")}</div>
      </section>

      <section class="block">
        ${this._sectionHead("Wi-Fi profiles", profiles.length,
          `"default" applies to every device. Other profiles add up to ${MAX_NETWORKS} networks, on 2.4 GHz and 5 GHz.`)}
        ${this._ui.pushing ? this._pushStatus() : ""}
        <div class="grid">${profiles.length
          ? profiles.map((p) => this._profileCard(p)).join("")
          : this._empty("No Wi-Fi profiles saved yet.")}</div>
        ${this._newProfile()}
      </section>
    </div>`;
  }

  _stat(label, value) {
    return `<div class="stat"><span class="stat-value">${this.escape(value)}</span><span class="stat-label">${this.escape(label)}</span></div>`;
  }

  _sectionHead(title, count, hint) {
    return `<div class="section-head">
      <div class="section-title"><h2>${this.escape(title)}</h2><span class="count">${count}</span></div>
      <p class="hint">${this.escape(hint)}</p>
    </div>`;
  }

  _empty(text) {
    return `<div class="empty">${this.escape(text)}</div>`;
  }

  // ---- device card ---------------------------------------------------------

  _deviceCard(entity, profiles) {
    const a = entity.attributes;
    const identity = a.identity || a.source_mac || a.base_mac || a.serial_number || a.source_ip || "";
    const status = a.status || (identity ? "pending" : "");
    const alive = !!a.alive;
    const mac = a.source_mac || a.base_mac || "";
    const isCapwap = a.protocol === "capwap";
    const appliedNames = [DEFAULT_PROFILE, ...(a.profiles || []).filter((n) => n !== DEFAULT_PROFILE)];
    const ssidRows = this._ssidRows(appliedNames, profiles);
    const stations = Array.isArray(a.stations) ? a.stations : [];
    const channels = Array.isArray(a.channels) ? a.channels : [];
    const hasDetails = !!(mac || a.radios_in_use || a.mount_type || channels.length || ssidRows.length || stations.length);
    const detailsOpen = this._ui.detailsOpen.has(identity);
    const sub = [a.source_ip, a.model || a.system_description].filter(Boolean);
    const restart = identity ? this._restartProgress(identity) : null;

    return `<article class="card device" ${restart?.active ? 'data-locked aria-busy="true"' : ""}>
      <header class="device-head">
        <div class="device-title">
          ${status ? this._statusDot(status, alive, entity.attributes.icon) : ""}
          <button class="name" data-action="open-device" data-entity="${this.escape(entity.entity_id)}">${this.escape(a.friendly_name || entity.entity_id)}</button>
          ${isCapwap && identity && status === "adopted" ? this._ledToggle(identity, status, this._ledEntity(identity)) : ""}
        </div>
        ${hasDetails ? this._textButton(detailsOpen ? "Hide" : "Details", "details-toggle", { identity }, "", "", false, icon(detailsOpen ? "mdi:chevron-up" : "mdi:chevron-down", "ico-trail")) : ""}
      </header>
      ${sub.length ? `<p class="sub mono sub-line">${this.escape(sub.join(" · "))}</p>` : ""}
      ${a.hardware || a.firmware ? `<p class="sub mono sub-line">${a.hardware ? `HW ${this.escape(a.hardware)}` : ""}${a.hardware && a.firmware ? " · " : ""}${a.firmware ? `FW ${this.escape(a.firmware)}` : ""}</p>` : ""}
      ${detailsOpen ? this._deviceDetails(a, mac, ssidRows, stations, identity) : ""}
      ${restart ? `<p class="restart-progress restart-${restart.state}" role="status">${this.escape(restart.text)}</p>` : ""}
      ${isCapwap && identity ? this._deviceActions(identity, status, a, profiles) : ""}
    </article>`;
  }

  _deviceDetails(a, mac, ssidRows, stations, identity) {
    const channels = Array.isArray(a.channels) ? a.channels : [];
    return `<div class="details">
      ${mac ? `<p class="sub mono">${this.escape(mac)}</p>` : ""}
      ${a.radios_in_use ? this._radioTags(a.radios_in_use, a.max_radios) : ""}
      ${a.mount_type ? `<p class="sub">Mount ${this.escape(a.mount_type)}</p>` : ""}
      ${channels.length ? `<p class="sub">Channels ${this.escape(channels.join(", "))}</p>` : ""}
      ${this._ssidSummary(ssidRows, stations)}
    </div>`;
  }

  /** The adoption progress of one access point, or null when no adoption is running.  The Adopt button
   * stays disabled while it is waiting for the AP to join. */
  _adoptProgress(identity, a) {
    const entry = this._ui.adopting.get(identity);
    if (!entry) return null;
    if (a.connected) {
      this._ui.adopting.delete(identity);
      return null;
    }
    if (entry.state === "timeout" || Date.now() - entry.since > ADOPT_TIMEOUT_MS) {
      this._ui.adopting.set(identity, { since: entry.since, state: "timeout" });
      return { active: false, text: "No join after 2 minutes. Check the AP and its network, then adopt again." };
    }
    return { active: true, text: "Adopting: waiting for the AP to join TPAC…" };
  }

  _deviceActions(identity, status, a, profiles) {
    const progress = this._adoptProgress(identity, a);
    const progressLine = progress ? `<p class="sub adopt-progress">${this.escape(progress.text)}</p>` : "";
    const adopting = !!progress && progress.active;
    if (status !== "adopted") {
      return `<footer class="device-actions">
        ${progressLine}
        <button class="btn btn-primary" data-action="adopt" data-identity="${this.escape(identity)}" ${adopting ? "disabled" : ""}>${adopting ? "Adopting…" : "Adopt"}</button>
      </footer>`;
    }
    const managing = this._ui.managing.has(identity);
    return `<footer class="device-actions">
      ${progressLine}
      ${this._textButton(managing ? "Hide options" : "Manage", "manage-toggle", { identity }, "btn-tonal", "", false, icon(managing ? "mdi:chevron-up" : "mdi:chevron-down", "ico-trail"))}
      ${managing ? this._manage(identity, a, profiles) : ""}
    </footer>`;
  }

  _manage(identity, a, profiles) {
    const applied = [DEFAULT_PROFILE, ...(a.profiles || []).filter((n) => n !== DEFAULT_PROFILE)];
    const pending = this._ui.pendingProfile.has(identity);
    const selected = pending ? this._ui.pendingProfile.get(identity) : applied;
    const busyForget = this._ui.busy.has(`forget:${identity}`);
    const chips = profiles.length
      ? profiles.map((p) => {
          const on = selected.includes(p.name);
          const locked = p.name === DEFAULT_PROFILE;
          return `<button class="chip ${on ? "chip-on" : ""}" data-action="select-profile" data-identity="${this.escape(identity)}" data-name="${this.escape(p.name)}" data-applied="${this.escape(JSON.stringify(applied))}" ${locked ? "disabled" : ""} title="${locked ? "Always applied" : on ? "Selected" : "Select"}">${on ? icon(ICON.check) : ""}${this.escape(p.name)}</button>`;
        }).join("")
      : '<span class="sub">No profiles saved yet.</span>';
    const countryDialog = this._ui.countryDialog?.identity === identity ? this._countryDialog(identity) : "";
    return `<div class="manage">
      ${countryDialog}
      <p class="label">Wi-Fi profiles</p>
      <div class="chips">${chips}</div>
      ${pending ? `<div class="row"><span class="sub">Not applied yet</span><button class="btn btn-primary" data-action="apply-now" data-identity="${this.escape(identity)}">Apply</button></div>` : ""}
      ${this._applyStatus(a.apply_status, a.apply_slots)}
      <div class="row spread">
        <div class="row">
          <button class="btn btn-text" data-action="reboot" data-identity="${this.escape(identity)}">Reboot</button>
          <button class="btn btn-text btn-warn" data-action="factory-reset" data-identity="${this.escape(identity)}">Factory reset</button>
          <button class="btn btn-text" data-action="country-open" data-identity="${this.escape(identity)}" data-country="${this.escape(a.country || "")}">${a.country ? `Country ${this.escape(a.country)}` : "Set country"}</button>
        </div>
        <button class="btn btn-text btn-danger" data-action="forget" data-identity="${this.escape(identity)}" ${busyForget ? "disabled" : ""}>${busyForget ? "Forgetting…" : "Forget"}</button>
      </div>
    </div>`;
  }

  /** The access point's LED entity (a switch), or undefined. */
  _ledEntity(identity) {
    return Object.values(this._hass.states).find(
      (e) => e.entity_id.startsWith("switch.") && e.attributes?.identity === identity
    );
  }

  _ledToggle(identity, status, entity) {
    const led = entity?.state;
    const on = led === "on";
    const disabled = status !== "adopted" || (led !== "on" && led !== "off");
    return `<button class="led-toggle ${on ? "led-on" : ""}" data-action="led-toggle" data-entity="${this.escape(entity?.entity_id || "")}" data-on="${!on}" ${disabled ? "disabled" : ""} title="LED ${on ? "on" : "off"}, click to turn ${on ? "off" : "on"}">${icon(entity?.attributes.icon || (on ? ICON.ledOn : ICON.ledOff))}</button>`;
  }

  _openCountry(identity, current) {
    this._ui.countryDialog = { identity, value: current || COUNTRIES[0] };
    this.render();
  }

  _countryDialog(identity) {
    const value = this._ui.countryDialog.value;
    const options = COUNTRIES.map((code) => `<option value="${code}" ${code === value ? "selected" : ""}>${code}</option>`).join("");
    return `<div class="modal-backdrop">
      <div class="card modal">
        <h3>Country</h3>
        <p class="sub">The AP rejoins to apply the country.</p>
        <select class="input" data-field="countryPick">${options}</select>
        <div class="row spread">
          ${this._textButton("Cancel", "country-cancel", {})}
          <button class="btn btn-primary" data-action="country-apply" data-identity="${this.escape(identity)}">Apply</button>
        </div>
      </div>
    </div>`;
  }

  _applyCountry() {
    const { identity, value } = this._ui.countryDialog;
    this._ui.countryDialog = null;
    this._call("set_country", { identity, country: value }, `country:${identity}`);
  }

  /** The access point's entity icon, colored by status.  Without an icon, the status dot. */
  _statusDot(status, alive, iconName) {
    const tone = status !== "adopted" ? "pending" : alive ? "ok" : "off";
    const title = status !== "adopted" ? "Pending" : alive ? "Adopted, reporting" : `Adopted, no report in ${ALIVE_WITHIN_MINUTES} min`;
    if (iconName) return `<ha-icon class="status-icon status-${tone}" icon="${this.escape(iconName)}" title="${this.escape(title)}"></ha-icon>`;
    return `<span class="dot dot-${tone}" title="${this.escape(title)}"></span>`;
  }

  _radioTags(radiosInUse, maxRadios) {
    const total = Math.max(radiosInUse, maxRadios || 0, 2);
    const labels = RADIO_LABELS[total] || Array.from({ length: total }, (_, i) => `Radio ${i + 1}`);
    const tags = labels
      .map((label, i) => `<span class="tag ${i < radiosInUse ? "tag-on" : ""}">${label}</span>`)
      .join("");
    return `<div class="radios"><p class="sub">Radios in use ${radiosInUse} of ${maxRadios || radiosInUse}</p><div class="chips">${tags}</div></div>`;
  }

  _applyStatus(status, slots) {
    if (!status) return "";
    const labels = { configuring: "Configuring", waiting: "Waiting", succeeded: "Succeeded", timeout: "No response", unchanged: "Unchanged" };
    const ops = { add: "New", update: "Update", delete: "Delete" };
    const summary = {
      confirmed: [ICON.ok, "Profile applied and confirmed"],
      failed: [ICON.warn, "The device rejected part of the profile"],
      timed_out: [ICON.warn, "Not fully confirmed. The device did not answer every network."],
    }[status] || [ICON.pending, "Waiting for the device to reconnect"];
    const lines = Object.entries(slots || {}).map(([slot, s]) => {
      const text = s.state === "failed" ? `Failed: ${s.message} (code ${s.code})` : labels[s.state] || s.state;
      const mark = s.state === "succeeded" || s.state === "unchanged" ? ICON.ok : s.state === "failed" || s.state === "timeout" ? ICON.warn : ICON.pending;
      return `<li>${icon(mark, "ico-small")}<span>${ops[s.op] ? `${ops[s.op]} ` : ""}${this.escape(slotLabel(slot))} ${this.escape(s.ssid)} · ${this.escape(text)}</span></li>`;
    }).join("");
    return `<div class="status">
      <p class="sub">${icon(summary[0], "ico-small")}${this.escape(summary[1])}</p>
      ${lines ? `<ul class="slots">${lines}</ul>` : ""}
    </div>`;
  }

  /** Device-level summary; the SSID view itself is the Wi-Fi networks section. */
  _ssidSummary(rows, stations) {
    if (!rows.length && !stations.length) return "";
    const chips = rows.map((r) => `<span class="chip">${this.escape(r.ssid)} <span class="sub">${this.escape(slotLabel(r.slot).split(" ").pop())}</span></span>`).join("");
    return `<div class="ssids">
      <p class="label">${rows.length} ${plural(rows.length, "SSID")} · ${stations.length} ${plural(stations.length, "client")}</p>
      <div class="chips">${chips}</div>
    </div>`;
  }

  /** Every SSID across all APs, with its bands, the APs carrying it and its clients. */
  _networks(devices, profiles) {
    const byName = new Map();
    for (const entity of devices) {
      const a = entity.attributes;
      if (a.protocol !== "capwap" || a.status !== "adopted") continue;
      const apName = a.friendly_name || entity.entity_id;
      const names = [DEFAULT_PROFILE, ...(a.profiles || []).filter((n) => n !== DEFAULT_PROFILE)];
      for (const r of this._ssidRows(names, profiles)) {
        const net = byName.get(r.ssid) || { ssid: r.ssid, bands: new Map(), clients: [] };
        const band = slotLabel(r.slot).split(" ")[0];
        const aps = net.bands.get(band) || new Set();
        aps.add(apName);
        net.bands.set(band, aps);
        byName.set(r.ssid, net);
      }
      for (const st of Array.isArray(a.stations) ? a.stations : []) {
        const net = byName.get(st.ssid);
        if (net) net.clients.push({ ...st, ap: apName });
      }
    }
    return [...byName.values()]
      .map((n) => ({ ...n, clients: n.clients.sort((x, y) => (y.rssi ?? -999) - (x.rssi ?? -999)) }))
      .sort((x, y) => y.clients.length - x.clients.length || x.ssid.localeCompare(y.ssid));
  }

  _networksSection(devices, profiles, offline) {
    const nets = this._networks(devices, profiles);
    const total = nets.reduce((sum, n) => sum + n.clients.length, 0);
    return `${this._sectionHead("Wi-Fi networks", nets.length, `${total} ${plural(total, "client")} connected across ${nets.length} ${plural(nets.length, "SSID")}`)}
      <div class="grid">${nets.length
        ? nets.map((n) => this._networkCard(n)).join("")
        : this._empty("No SSID is running on an adopted access point.")}${this._offlineCard(offline)}</div>`;
  }

  /** Known clients that no adopted access point reports now.  The list folds away like an SSID's. */
  _offlineCard(offline) {
    const open = this._ui.offlineOpen;
    // A client still named by its MAC is unidentified: no name from the router or Home Assistant.
    const unknown = offline.filter((e) => {
      const mac = String(e.attributes.mac);
      return (this._clientNames?.get(mac.toLowerCase()) || mac) === mac;
    }).length;
    const body = open
      ? (offline.length
          ? `<div class="clients">${offline.map((e) => {
              const mac = String(e.attributes.mac);
              const name = this._clientNames?.get(mac.toLowerCase()) || mac;
              const lastAp = e.attributes.last_ap;
              const detail = lastAp ? `last connected to ${this._apNames?.get(lastAp) || lastAp}` : "";
              return `<div class="client"><div class="client-id">
                <div class="client-name">${this._clientIcon(mac)}<button class="link ${name === mac ? "mono unnamed" : ""}" data-action="open-client" data-mac="${this.escape(mac)}">${this.escape(name)}</button></div>
                ${detail ? `<span class="sub">${this.escape(detail)}</span>` : ""}
              </div></div>`;
            }).join("")}</div>`
          : '<p class="sub">No clients</p>')
      : "";
    return `<article class="card network">
      <header class="device-head">
        <div class="device-title">${icon("mdi:cellphone-off", "ico-small")}<h3>Offline</h3></div>
        <div class="row">
          <span class="count-big">${offline.length}<span class="sub"> ${plural(offline.length, "client")}</span></span>
          <button class="btn btn-text btn-icon" data-action="offline-toggle" title="${open ? "Hide clients" : "Show clients"}">${icon(open ? "mdi:chevron-up" : "mdi:chevron-down")}</button>
        </div>
      </header>
      <div class="bands"><div class="band"><span class="sub">Unknown clients: ${unknown}</span></div></div>
      ${body}
    </article>`;
  }

  /** Always shows the SSID with its client count, and the bands it runs on.
   * The client list folds away under the chevron. */
  _networkCard(net) {
    const open = this._ui.networkOpen.has(net.ssid);
    const bands = [...net.bands.entries()].map(([band, aps]) => {
      const n = aps.size;
      return `<div class="band"><span class="tag tag-on">${this.escape(band)}</span><span class="sub">Available on ${n} ${plural(n, "AP")}</span></div>`;
    }).join("");
    const body = open
      ? (net.clients.length
          ? `<div class="clients">${net.clients.map((c) => this._clientRow(c)).join("")}</div>`
          : '<p class="sub">No clients</p>')
      : "";
    return `<article class="card network">
      <header class="device-head">
        <div class="device-title">${this._networkIcons?.get(net.ssid) ? icon(this._networkIcons.get(net.ssid), "ico-small") : ""}<button class="name name-ssid" data-action="open-network" data-ssid="${this.escape(net.ssid)}">${this.escape(net.ssid)}</button></div>
        <div class="row">
          <span class="count-big">${net.clients.length}<span class="sub"> ${plural(net.clients.length, "client")}</span></span>
          <button class="btn btn-text btn-icon" data-action="network-toggle" data-ssid="${this.escape(net.ssid)}" title="${open ? "Hide clients" : "Show clients"}">${icon(open ? "mdi:chevron-up" : "mdi:chevron-down")}</button>
        </div>
      </header>
      <div class="bands">${bands}</div>
      ${body}
    </article>`;
  }

  /** The client entity's icon, when one is set. */
  _clientIcon(mac) {
    const name = this._clientIcons?.get(String(mac).toLowerCase());
    return name ? icon(name) : "";
  }

  /** A duration such as "2 h 5 min" or "3 d 4 h". */
  _duration(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d} d ${h} h`;
    if (h) return `${h} h ${m} min`;
    return `${Math.max(m, 1)} min`;
  }

  _clientRow(c) {
    const mac = String(c.mac || "");
    const name = this._clientNames?.get(mac.toLowerCase()) || mac;
    const named = name !== mac;
    const apIcon = this._apIcons?.get(c.ap);
    const ap = c.ap ? `<span class="detail-item">${apIcon ? icon(apIcon, "ico-detail") : ""}${this.escape(c.ap)}</span>` : "";
    // Online time counts from the last association.  The address is shown only for unidentified clients.
    // Since the last association, or since TPAC first saw the client when no association is known.
    const since = c.since || c.first_seen;
    const online = since ? `<span class="detail-item">${icon("mdi:web-clock", "ico-detail")}${this._duration(Date.now() / 1000 - since)}</span>` : "";
    const ip = !named && c.ip ? `<span class="detail-item">${icon("mdi:ip-network", "ico-detail")}${this.escape(c.ip)}</span>` : "";
    const detail = [ap, online, ip].filter(Boolean).join('<span class="detail-sep">·</span>');
    return `<div class="client">
      <div class="client-id">
        <div class="client-name">${this._clientIcon(mac)}<button class="link ${named ? "" : "mono unnamed"}" data-action="open-client" data-mac="${this.escape(mac)}">${this.escape(name)}</button></div>
        <span class="sub client-detail">${detail}</span>
      </div>
      <div class="signal" title="${c.rssi != null ? `${c.rssi} dBm` : "Signal unknown"}">${this._bars(c.rssi)}</div>
    </div>`;
  }

  /** Four signal bars from RSSI: at least -55 dBm is full strength. */
  _bars(rssi) {
    const level = rssi == null ? 0 : rssi >= -55 ? 4 : rssi >= -65 ? 3 : rssi >= -75 ? 2 : 1;
    return [1, 2, 3, 4].map((i) => `<span class="bar ${i <= level ? "bar-on" : ""}" style="height:${4 + i * 3}px"></span>`).join("");
  }

  _ssidRows(appliedNames, profiles) {
    const seen = new Set();
    const rows = [];
    for (const name of appliedNames) {
      const profile = profiles.find((p) => p.name === name);
      for (const [slot, net] of Object.entries(profile?.wlans || {})) {
        if (net.enabled === false || !net.ssid || seen.has(net.ssid)) continue;
        seen.add(net.ssid);
        rows.push({ ssid: net.ssid, slot });
      }
    }
    return rows;
  }

  // ---- profile card and forms ---------------------------------------------

  _profileCard(profile) {
    const chips = Object.entries(profile.wlans || {}).map(([slot, net]) => {
      const details = [net.vlan ? `VLAN ${net.vlan}` : null, net.hidden ? "hidden" : null, net.enabled === false ? "disabled" : null].filter(Boolean);
      return `<span class="chip">${this.escape(slotLabel(slot))} · ${this.escape(net.ssid)}${details.length ? ` <span class="sub">${this.escape(details.join(", "))}</span>` : ""}</span>`;
    }).join("");
    const editing = this._ui.editing === profile.name;
    const duplicating = this._ui.duplicating === profile.name;
    return `<article class="card profile${editing ? " wide" : ""}">
      <header class="device-head">
        <div class="device-title"><h3>${this.escape(profile.name)}</h3></div>
        <div class="row">
          ${this._textButton("Edit", "edit-open", { name: profile.name }, "", icon(ICON.edit))}
          ${profile.builtin ? "" : `${this._textButton("Duplicate", "duplicate-open", { name: profile.name }, "", icon(ICON.copy))}${this._textButton("Delete", "delete-profile", { name: profile.name }, "btn-danger", icon(ICON.delete))}`}
        </div>
      </header>
      <div class="chips">${chips || '<span class="sub">No WLANs</span>'}</div>
      ${duplicating ? `<div class="row">
        <input class="input" placeholder="New profile name" value="${this.escape(this._ui.duplicateValue)}" data-field="duplicateValue">
        <button class="btn btn-primary" data-action="duplicate-confirm" data-name="${this.escape(profile.name)}">Duplicate</button>
        <button class="btn btn-text" data-action="duplicate-cancel">Cancel</button>
      </div>` : ""}
      ${editing && profile.builtin ? this._defaultForm(profile.name) : ""}
      ${editing && !profile.builtin ? this._networkForm(this._ui.editNetworks, "editNetworks", "edit", `
        <button class="btn btn-primary" data-action="edit-save" data-name="${this.escape(profile.name)}">Save</button>
        <button class="btn btn-text" data-action="edit-cancel">Cancel</button>`) : ""}
    </article>`;
  }

  _pushStatus() {
    const push = this._ui.pushing;
    const labels = { confirmed: "Applied", failed: "Failed", timed_out: "No response", pending: "Pushing", unknown: "—" };
    const rows = push.results.length
      ? push.results.map((r) => `<li><span>${this.escape(r.name)}</span><span class="sub">${r.connected ? this.escape(labels[r.status] || r.status) : "Offline, applied at next join"}</span></li>`).join("")
      : `<li class="sub">${push.ids.length ? `Sending to ${push.ids.length} ${plural(push.ids.length, "AP")}` : "No AP uses this profile"}</li>`;
    const head = push.phase === "done" ? `${push.title}: done` : `${push.title}…`;
    return `<div class="card push">
      <p class="push-title">${this.escape(head)}</p>
      <ul class="slots">${rows}</ul>
    </div>`;
  }

  _defaultForm(name) {
    const rows = this._ui.editDefault.map((n, index) => {
      const fixed = DEFAULT_NETWORKS[index];
      return `<div class="net">
        <div class="net-head">
          <span class="label">${this.escape(fixed.label)}</span>
          <span class="toggles">
            <label class="check"><input type="checkbox" data-list="editDefault" data-index="${index}" data-field="enabled" ${n.enabled ? "checked" : ""}> Enabled</label>
            <label class="check"><input type="checkbox" data-list="editDefault" data-index="${index}" data-field="hidden" ${n.hidden ? "checked" : ""}> Hide SSID</label>
          </span>
        </div>
        <div class="fields">
          <input class="input" placeholder="SSID" value="${this.escape(n.ssid)}" data-list="editDefault" data-index="${index}" data-field="ssid">
          <input class="input" type="password" placeholder="Leave blank to keep" value="${this.escape(n.password)}" data-list="editDefault" data-index="${index}" data-field="password">
        </div>
      </div>`;
    }).join("");
    return `<div class="form">
      ${rows}
      <p class="sub">Security is WPA-PSK/WPA2-PSK with AES.</p>
      <div class="row">
        <button class="btn btn-primary" data-action="edit-save" data-name="${this.escape(name)}">Save</button>
        <button class="btn btn-text" data-action="edit-cancel">Cancel</button>
      </div>
    </div>`;
  }

  /** A list of logical networks.  A band checkbox is disabled once that band's
   * slots are used up by the other rows. */
  _networkForm(networks, list, scope, footer) {
    const used = {};
    for (const band of BANDS) used[band] = networks.filter((n) => n.bands[band]).length;
    const rows = networks.map((network, index) => {
      const bands = BANDS.map((band) => {
        const checked = network.bands[band];
        const disabled = !checked && used[band] >= FREE_SLOTS[band].length;
        return `<label class="check ${disabled ? "check-off" : ""}"><input type="checkbox" data-list="${list}" data-index="${index}" data-band="${band}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}> ${band}</label>`;
      }).join("");
      return `<div class="net">
        <div class="net-head">
          <span class="label">Network ${index + 1}</span>
          <span class="toggles">
            ${bands}
            <label class="check"><input type="checkbox" data-list="${list}" data-index="${index}" data-field="hidden" ${network.hidden ? "checked" : ""}> Hide SSID</label>
          </span>
          ${networks.length > 1 ? `<button class="btn btn-text btn-danger" data-action="remove-network" data-list="${list}" data-index="${index}">Remove</button>` : ""}
        </div>
        <div class="fields">
          <input class="input" placeholder="SSID" value="${this.escape(network.ssid)}" data-list="${list}" data-index="${index}" data-field="ssid">
          <input class="input" type="password" placeholder="${scope === "edit" ? "Leave blank to keep" : "Password"}" value="${this.escape(network.password)}" data-list="${list}" data-index="${index}" data-field="password">
          <select class="input" title="Security" data-list="${list}" data-index="${index}" data-field="auth">
            ${Object.entries(AUTH_OPTIONS).map(([v, l]) => `<option value="${v}" ${v === network.auth ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <select class="input" title="Encryption" data-list="${list}" data-index="${index}" data-field="cipher">
            ${Object.entries(CIPHER_OPTIONS).map(([v, l]) => `<option value="${v}" ${v === network.cipher ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <input class="input narrow" placeholder="VLAN" value="${network.vlan === "0" || network.vlan === 0 ? "" : this.escape(network.vlan)}" data-list="${list}" data-index="${index}" data-field="vlan">
        </div>
      </div>`;
    }).join("");
    return `<div class="form">
      ${rows}
      <div class="row spread">
        ${this._textButton("Add network", "add-network", { list }, "", icon(ICON.add), networks.length >= MAX_NETWORKS)}
      </div>
      ${footer}
    </div>`;
  }

  _newProfile() {
    if (!this._ui.newProfileOpen) {
      return `<div class="new">
        ${this._textButton("Export", "profiles-export", {}, "", icon(ICON.download))}
        ${this._textButton("Import", "profiles-import", {}, "", icon(ICON.upload))}
        ${this._textButton("New profile", "new-profile-toggle", {}, "btn-tonal", icon(ICON.add))}
      </div>`;
    }
    return `<article class="card profile new-card wide">
      <input class="input" placeholder="Profile name" value="${this.escape(this._ui.draftName)}" data-field="draftName">
      ${this._networkForm(this._ui.draftNetworks, "draftNetworks", "slot", `
        <button class="btn btn-primary" data-action="new-profile-save">Save profile</button>
        <button class="btn btn-text" data-action="new-profile-toggle">Cancel</button>`)}
    </article>`;
  }

  // ---- helpers -------------------------------------------------------------

  _textButton(label, action, data, extra = "", iconHtml = "", disabled = false, trailingHtml = "") {
    const attrs = Object.entries(data).map(([k, v]) => `data-${k.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())}="${this.escape(v)}"`).join(" ");
    return `<button class="btn btn-text ${extra}" data-action="${action}" ${attrs} ${disabled ? "disabled" : ""}>${iconHtml}${this.escape(label)}${trailingHtml}</button>`;
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
  }

  _styles() {
    return `<style>
      :host {
        display: block;
        min-height: 100%;
        box-sizing: border-box;
        color: var(--primary-text-color);
        background: var(--primary-background-color);
        font-family: var(--paper-font-body1_-_font-family, Roboto, "Noto Sans", Arial, sans-serif);
        font-size: 14px;
        line-height: 1.45;
      }
      .page { max-width: 1120px; margin: 0 auto; padding: 24px 24px 56px; box-sizing: border-box; color: var(--primary-text-color); }
      .mono { font-family: var(--code-font-family, ui-monospace, Menlo, monospace); }
      h1 { font-size: 24px; font-weight: 400; margin: 0; letter-spacing: 0.1px; color: var(--primary-text-color); }
      h2 { font-size: 16px; font-weight: 500; margin: 0; color: var(--primary-text-color); }
      h3 { font-size: 15px; font-weight: 500; margin: 0; color: var(--primary-text-color); }
      p { margin: 0; }
      .sub { color: var(--secondary-text-color); font-size: 12.5px; }
      .sub-line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .label { color: var(--secondary-text-color); font-size: 12px; font-weight: 500; letter-spacing: 0.02em; text-transform: uppercase; }
      .count { color: var(--secondary-text-color); font-size: 12px; margin-left: 6px; }
      .ico { --mdc-icon-size: 18px; color: var(--secondary-text-color); flex: none; }
      .ico-small { --mdc-icon-size: 15px; vertical-align: -3px; margin-right: 4px; }
      .ico-trail { --mdc-icon-size: 18px; margin: 0 0 0 4px; }
      .client-name { display: flex; align-items: center; gap: 6px; min-width: 0; }
      .client-name .ico { --mdc-icon-size: 16px; }
      .client-detail { display: flex; align-items: center; gap: 4px; min-width: 0; overflow: hidden; white-space: nowrap; }
      .detail-item { display: inline-flex; align-items: center; gap: 4px; }
      .detail-sep { margin: 0 2px; }
      .ico-detail { --mdc-icon-size: 14px; flex: none; }

      .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
      .page-head p { color: var(--secondary-text-color); margin-top: 2px; }
      .build { color: var(--secondary-text-color); font-size: 12px; }

      .overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 28px; }
      .stat { background: var(--ha-card-background, var(--card-background-color)); border: 1px solid var(--ha-card-border-color, var(--divider-color)); border-radius: var(--ha-card-border-radius, 12px); padding: 14px 16px; display: flex; flex-direction: column; gap: 2px; }
      .stat-value { font-size: 22px; font-weight: 400; }
      .stat-label { color: var(--secondary-text-color); font-size: 12px; }

      .block { margin-bottom: 32px; }
      .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
      .section-title { display: flex; align-items: baseline; }
      .hint { color: var(--secondary-text-color); font-size: 12.5px; max-width: 560px; }

      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; align-items: start; }
      .card { background: var(--ha-card-background, var(--card-background-color)); border: 1px solid var(--ha-card-border-color, var(--divider-color)); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, none); padding: 16px; display: flex; flex-direction: column; gap: 10px; box-sizing: border-box; min-width: 0; }
      .device-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 28px; }
      .device-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
      .name { border: none; background: none; padding: 0; font: inherit; font-size: 15px; font-weight: 500; color: var(--primary-color); cursor: pointer; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .name-ssid { font-size: 14px; }
      .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--disabled-text-color); }
      .dot-ok { background: var(--success-color, #43a047); }
      .dot-off { background: var(--error-color, #db4437); }
      .dot-pending { background: var(--warning-color, #ffa600); }
      .status-icon { --mdc-icon-size: 20px; flex: none; color: var(--disabled-text-color); }
      .status-ok { color: var(--success-color, #43a047); }
      .status-off { color: var(--error-color, #db4437); }
      .status-pending { color: var(--warning-color, #ffa600); }
      .details { display: flex; flex-direction: column; gap: 8px; padding-top: 10px; border-top: 1px solid var(--divider-color); }
      .device-actions { display: flex; flex-direction: column; gap: 10px; padding-top: 10px; border-top: 1px solid var(--divider-color); }
      .device-actions > button { align-self: flex-start; }
      .manage { display: flex; flex-direction: column; gap: 10px; }
      .radios { display: flex; flex-direction: column; gap: 6px; }

      .chips { display: flex; flex-wrap: wrap; gap: 6px; }
      .chip, .tag { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; border-radius: 999px; padding: 3px 10px; background: var(--secondary-background-color); color: var(--primary-text-color); white-space: nowrap; }
      .tag { color: var(--secondary-text-color); padding: 1px 8px; }
      .led-toggle { border: none; background: none; padding: 0; cursor: pointer; color: var(--disabled-text-color); display: inline-flex; flex: none; }
      .led-toggle .ico { color: inherit; }
      .led-toggle.led-on { color: var(--warning-color, #fbc02d); }
      .led-toggle:disabled { opacity: 0.5; cursor: default; }
      .modal-backdrop { position: fixed; inset: 0; z-index: 10; display: flex; align-items: center; justify-content: center; background: rgba(0, 0, 0, 0.4); padding: 16px; }
      .modal { width: min(360px, 100%); }
      .tag-on { background: color-mix(in srgb, var(--success-color, #43a047) 16%, transparent); color: var(--success-color, #43a047); }
      .chip { border: 1px solid transparent; cursor: pointer; font: inherit; font-size: 12px; }
      .chip:disabled { cursor: default; opacity: 0.7; }
      .chip-on { border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 12%, transparent); color: var(--primary-color); }
      .chip .ico { --mdc-icon-size: 14px; color: currentColor; }

      .tab { border: 1px solid var(--divider-color); background: none; border-radius: 8px; padding: 4px 10px; font: inherit; font-size: 12.5px; color: var(--primary-text-color); cursor: pointer; }
      .tab-on { border-color: var(--primary-color); color: var(--primary-color); }
      .ssids, .status { display: flex; flex-direction: column; gap: 6px; }
      .network { gap: 12px; }
      .btn-icon { padding: 4px; }
      .btn-icon .ico { --mdc-icon-size: 20px; }
      .count-big { font-size: 22px; font-weight: 400; color: var(--primary-text-color); white-space: nowrap; }
      .bands { display: flex; flex-direction: column; gap: 4px; }
      .band { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
      .clients { display: flex; flex-direction: column; border-top: 1px solid var(--divider-color); }
      .client { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
      .client:last-child { border-bottom: none; }
      .client-id { display: flex; flex-direction: column; min-width: 0; }
      .client-id .sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .signal { display: flex; align-items: flex-end; gap: 2px; height: 16px; flex: none; }
      .bar { width: 3px; border-radius: 1px; background: var(--divider-color); }
      .bar-on { background: var(--primary-color); }
      .clients { display: flex; flex-direction: column; gap: 4px; }
      .client { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
      .link { border: none; background: none; padding: 0; font: inherit; color: var(--primary-color); cursor: pointer; text-align: left; }
      .link.unnamed { color: var(--warning-color, #ffa600); }
      .slots { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
      .slots li { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }

      .btn { border: none; border-radius: 999px; padding: 7px 16px; font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
      .btn:disabled { opacity: 0.5; cursor: default; }
      .card.device[data-locked] .device-actions { opacity: 0.6; }
      .restart-progress { margin: 0; padding: 8px 10px; border-radius: 8px; font-size: 13px; background: var(--secondary-background-color); color: var(--primary-text-color); }
      .restart-busy { border-left: 3px solid var(--warning-color, #ffa600); }
      .restart-done { border-left: 3px solid var(--success-color, #43a047); }
      .restart-timeout { border-left: 3px solid var(--error-color, #db4437); }
      .btn .ico { --mdc-icon-size: 16px; color: currentColor; }
      .btn-primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .btn-tonal { background: var(--secondary-background-color); color: var(--primary-text-color); }
      .btn-text { background: none; color: var(--primary-color); padding: 6px 10px; border-radius: 8px; }
      .btn-text .ico { color: currentColor; }
      .btn-warn { color: var(--warning-color, #c77700); }
      .btn-danger { color: var(--error-color, #db4437); }
      .btn-text:hover { background: color-mix(in srgb, currentColor 8%, transparent); }

      .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .row.spread { justify-content: space-between; }
      .input, select.input { font: inherit; font-size: 13.5px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--divider-color); background: var(--primary-background-color); color: var(--primary-text-color); box-sizing: border-box; min-width: 0; width: 100%; }
      .input:focus, select.input:focus { outline: none; border-color: var(--primary-color); }
      .input.narrow { width: 96px; }
      .field-inline { display: inline-flex; align-items: center; gap: 8px; color: var(--secondary-text-color); font-size: 13px; }
      .field-inline .input { width: auto; }

      .form { display: flex; flex-direction: column; gap: 12px; padding-top: 12px; border-top: 1px solid var(--divider-color); }
      .net { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-radius: 10px; background: var(--secondary-background-color); }
      .net-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
      .toggles { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
      .check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--secondary-text-color); cursor: pointer; }
      .check input { accent-color: var(--primary-color); margin: 0; }
      .check-off { opacity: 0.5; cursor: default; }
      .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
      .fields .input.narrow { width: 100%; }

      .status { padding-top: 10px; border-top: 1px solid var(--divider-color); }
      .wide { grid-column: 1 / -1; }
      .new { display: flex; }
      .push { margin-bottom: 16px; gap: 8px; }
      .push-title { font-weight: 500; }
      .new-card { border-style: dashed; }
      .empty { border: 1px dashed var(--divider-color); border-radius: 12px; padding: 18px; color: var(--secondary-text-color); font-size: 13px; }

      @media (max-width: 600px) {
        .page { padding: 16px 12px 40px; }
        .fields { grid-template-columns: 1fr; }
        .grid { grid-template-columns: 1fr; }
        .overview { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
        .stat { padding: 10px 12px; }
        .stat-value { font-size: 18px; }
        .stat-label { font-size: 11px; }
      }
    </style>`;
  }
}

customElements.define("tpac-panel", TpacPanel);
