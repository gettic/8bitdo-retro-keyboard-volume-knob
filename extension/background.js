// Service worker: keeps a list of YouTube / YouTube Music tabs and connects to
// the native-messaging host only while at least one such tab is open.
// The host forwards everything to and from the volume knob program (see PROTOCOL.md).
"use strict";

const HOST_NAME = "com.gettic.volume_knob";
const SITES = new Set(["youtube", "ytmusic"]);
const BOOST = new Set(["off", "on", "blocked", "unavailable", "error"]);
const FEATURES = ["boost", "set_volume"];

const tabs = new Map(); // tabId -> { port, state: {site, playing, volume, muted} | null }
let native = null;
let nativeRetryTimer = 0;
let lastFailLogged = 0;
let sendTimer = 0;

function tabList() {
  const list = [];
  for (const [tab, t] of tabs) {
    if (t.state) list.push({ tab, ...t.state });
  }
  return list;
}

function sendState() {
  sendTimer = 0;
  if (!native) return;
  try {
    native.postMessage({ type: "state", tabs: tabList() });
  } catch (e) {
    native = null;
  }
}

function scheduleState() {
  if (!sendTimer) sendTimer = setTimeout(sendState, 30);
}

function connectNative() {
  nativeRetryTimer = 0;
  if (native || tabs.size === 0) return;
  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    retryNative();
    return;
  }
  native = port;
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (native === port) native = null;
    if (err && Date.now() - lastFailLogged > 600000) {
      lastFailLogged = Date.now();
      console.info("Volume knob host not available:", err.message);
    }
    retryNative();
  });
  try {
    port.postMessage({ type: "hello", ext: chrome.runtime.getManifest().version, features: FEATURES });
  } catch (e) { /* onDisconnect handles it */ }
  sendState();
}

function retryNative() {
  if (nativeRetryTimer || tabs.size === 0) return;
  nativeRetryTimer = setTimeout(connectNative, 60000);
}

function disconnectNative() {
  if (nativeRetryTimer) { clearTimeout(nativeRetryTimer); nativeRetryTimer = 0; }
  if (native) {
    try { native.disconnect(); } catch (e) { /* ignore */ }
    native = null;
  }
}

function onHostMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  const type = msg.type;
  if (type !== "volume" && type !== "set_volume" && type !== "mute" && type !== "probe") return;
  // Targets (PROTOCOL.md): "tabs" if present, otherwise "site".
  let only = null;
  let site = "all";
  if (Array.isArray(msg.tabs)) {
    only = new Set(msg.tabs.filter((n) => Number.isInteger(n)));
  } else {
    site = msg.site;
    if (site !== "youtube" && site !== "ytmusic" && site !== "all") return;
  }
  let cmd;
  if (type === "volume") {
    const d = msg.delta;
    if (typeof d !== "number" || !Number.isFinite(d) || d < -1.5 || d > 1.5) return;
    cmd = { type: "volume", delta: d };
    // "max" missing = 1.0 (no boost), PROTOCOL.md v2.
    const m = msg.max;
    cmd.max = typeof m === "number" && Number.isFinite(m) ? Math.min(1.5, Math.max(0, m)) : 1.0;
  } else if (type === "set_volume") {
    const v = msg.volume;
    if (typeof v !== "number" || !Number.isFinite(v)) return;
    cmd = { type: "set_volume", volume: Math.min(1.5, Math.max(0, v)) };
  } else if (type === "mute") {
    cmd = { type: "mute" };
    if (typeof msg.muted === "boolean") cmd.muted = msg.muted;
  } else {
    cmd = { type: "probe" };
  }
  for (const [tabId, t] of tabs) {
    if (!t.state) continue;
    if (site !== "all" && t.state.site !== site) continue;
    if (only && !only.has(tabId)) continue;
    try { t.port.postMessage(cmd); } catch (e) { /* tab is going away */ }
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "tab" || !port.sender || !port.sender.tab || port.sender.frameId !== 0) {
    port.disconnect();
    return;
  }
  if (port.sender.id !== chrome.runtime.id) { port.disconnect(); return; }
  const tabId = port.sender.tab.id;
  const old = tabs.get(tabId);
  if (old && old.port !== port) {
    try { old.port.disconnect(); } catch (e) { /* ignore */ }
  }
  const entry = { port, state: null };
  tabs.set(tabId, entry);

  port.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "state") {
      const s = msg.state;
      if (s === null) {
        entry.state = null;
      } else if (s && SITES.has(s.site) &&
                 (s.volume === null || (typeof s.volume === "number" && Number.isFinite(s.volume)))) {
        // volume null = player not loaded yet (the tab is still reported).
        entry.state = {
          site: s.site,
          playing: s.playing === true,
          volume: s.volume === null ? null : Math.min(1.5, Math.max(0, s.volume)),
          muted: s.muted === true,
        };
        if (typeof s.max_volume === "number" && Number.isFinite(s.max_volume)) {
          entry.state.max_volume = Math.min(1.5, Math.max(0, s.max_volume));
        }
        if (BOOST.has(s.boost)) entry.state.boost = s.boost;
      } else {
        return;
      }
      scheduleState();
    } else if (msg.type === "probe" && native) {
      // Test diagnostics only: what the page's own controls show.
      try {
        native.postMessage({ type: "probe", tab: tabId, ytSlider: msg.ytSlider,
                             ytmSlider: msg.ytmSlider, saved: msg.saved });
      } catch (e) { /* ignore */ }
    }
  });

  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    if (tabs.get(tabId) === entry) tabs.delete(tabId);
    if (tabs.size === 0) {
      sendState();
      disconnectNative();
    } else {
      scheduleState();
    }
  });

  connectNative();
});
