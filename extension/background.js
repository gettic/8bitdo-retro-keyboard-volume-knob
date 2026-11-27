// Service worker: keeps a list of YouTube / YouTube Music tabs and, only while
// at least one such tab is open, keeps a WebSocket to the volume knob program
// on 127.0.0.1 (see PROTOCOL.md). No other network use.
"use strict";

// The program listens on the first free one of these ports, on 127.0.0.1 only.
const PORTS = [38475, 38476, 38477];
const WS_PATH = "/volume-knob/2";
const PROTOCOL = 2;
const HEARTBEAT_MS = 2000;
const RETRY_MS = [1000, 2000, 5000]; // after all ports failed (local connects are cheap)
const GRACE_MS = 4000;     // keep the link this long after the last tab went (page reloads)
const MAX_DELTA = 0.2;     // the program's largest knob step (config "step" 0.01-0.20)
const MAX_SET = 1.0;       // set_volume never boosts (tray quick row / failsafe are <= 1.0)
const PROBE_MS = 2000;     // a tab's probe answer is forwarded only this long after a probe
const SITES = new Set(["youtube", "ytmusic"]);
const BOOST = new Set(["off", "on", "blocked", "unavailable", "error"]);
const FEATURES = ["boost", "set_volume"];

const tabs = new Map(); // tabId -> { port, state: {site, playing, volume, muted} | null }
let native = null;      // open WebSocket whose hello answer was right
let socket = null;      // WebSocket being opened or open
let portIndex = 0;
let failRound = 0;
let nativeRetryTimer = 0;
let heartbeatTimer = 0;
let lastFailLogged = 0;
let sendTimer = 0;
let graceTimer = 0;
let probeUntil = 0;

function tabList() {
  const list = [];
  for (const [tab, t] of tabs) {
    if (t.state) list.push({ tab, ...t.state });
  }
  return list;
}

function post(msg) {
  if (!native) return;
  try {
    native.send(JSON.stringify(msg));
  } catch (e) { /* onclose handles it */ }
}

function sendState() {
  sendTimer = 0;
  post({ type: "state", tabs: tabList() });
}

function scheduleState() {
  if (!sendTimer) sendTimer = setTimeout(sendState, 30);
}

function connectNative() {
  nativeRetryTimer = 0;
  if (socket || tabs.size === 0) return;
  let ws;
  try {
    ws = new WebSocket("ws://127.0.0.1:" + PORTS[portIndex] + WS_PATH);
  } catch (e) {
    nextPort();
    return;
  }
  socket = ws;
  ws.onopen = () => {
    if (socket !== ws) return;
    setTimeout(() => {
      if (socket === ws && native !== ws) { try { ws.close(); } catch (e) { /* ignore */ } }
    }, 3000);
    // The program answers our hello with its own; only then it gets state.
    try {
      ws.send(JSON.stringify({ type: "hello", protocol: PROTOCOL,
                               host_version: chrome.runtime.getManifest().version,
                               features: FEATURES }));
    } catch (e) { /* onclose handles it */ }
  };
  ws.onmessage = (ev) => {
    if (socket !== ws || typeof ev.data !== "string" || ev.data.length > 65536) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!msg || typeof msg !== "object") return;
    if (native !== ws) {
      if (msg.type === "hello" && msg.protocol === PROTOCOL) {
        native = ws;
        failRound = 0;
        sendState();
        heartbeatTimer = setInterval(sendState, HEARTBEAT_MS);
      } else {
        try { ws.close(); } catch (e) { /* ignore */ }
      }
      return;
    }
    onHostMessage(msg);
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    const wasUp = native === ws;
    socket = null;
    native = null;
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = 0; }
    if (wasUp) {
      retryNative(RETRY_MS[0]); // same port first
    } else {
      nextPort();
    }
  };
}

function nextPort() {
  portIndex = (portIndex + 1) % PORTS.length;
  if (portIndex !== 0) {
    retryNative(1000);
    return;
  }
  if (Date.now() - lastFailLogged > 600000) {
    lastFailLogged = Date.now();
    console.info("Volume knob program not reachable on 127.0.0.1");
  }
  retryNative(RETRY_MS[Math.min(failRound, RETRY_MS.length - 1)]);
  failRound += 1;
}

function retryNative(wait) {
  if (nativeRetryTimer || tabs.size === 0) return;
  nativeRetryTimer = setTimeout(connectNative, wait);
}

function disconnectNative() {
  if (graceTimer) { clearTimeout(graceTimer); graceTimer = 0; }
  if (nativeRetryTimer) { clearTimeout(nativeRetryTimer); nativeRetryTimer = 0; }
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = 0; }
  const ws = socket;
  socket = null;
  native = null;
  failRound = 0;
  if (ws) {
    try { ws.close(1000); } catch (e) { /* ignore */ }
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
    if (typeof d !== "number" || !Number.isFinite(d)) return;
    // One knob click at most: whoever answers on the port can't jump a tab up.
    cmd = { type: "volume", delta: Math.min(MAX_DELTA, Math.max(-MAX_DELTA, d)) };
    // "max" missing = 1.0 (no boost), PROTOCOL.md v2.
    const m = msg.max;
    cmd.max = typeof m === "number" && Number.isFinite(m) ? Math.min(1.5, Math.max(0, m)) : 1.0;
  } else if (type === "set_volume") {
    const v = msg.volume;
    if (typeof v !== "number" || !Number.isFinite(v)) return;
    cmd = { type: "set_volume", volume: Math.min(MAX_SET, Math.max(0, v)) };
  } else if (type === "mute") {
    cmd = { type: "mute" };
    if (typeof msg.muted === "boolean") cmd.muted = msg.muted;
  } else {
    cmd = { type: "probe" };
    probeUntil = Date.now() + PROBE_MS;
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
  if (graceTimer) { clearTimeout(graceTimer); graceTimer = 0; }
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
    } else if (msg.type === "probe") {
      // Test diagnostics only: what the page's own controls show. Only as an
      // answer to the program's probe, never on the page's own initiative.
      if (Date.now() > probeUntil) return;
      post({ type: "probe", tab: tabId, ytSlider: msg.ytSlider,
             ytmSlider: msg.ytmSlider, saved: msg.saved, box: msg.box });
    }
  });

  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    if (tabs.get(tabId) === entry) tabs.delete(tabId);
    if (tabs.size === 0) {
      // Tell the program at once, but keep the link a moment: a reload or a
      // YouTube link brings a new tab port right away.
      sendState();
      if (!graceTimer) {
        graceTimer = setTimeout(() => {
          graceTimer = 0;
          if (tabs.size === 0) disconnectNative();
        }, GRACE_MS);
      }
    } else {
      scheduleState();
    }
  });

  connectNative();
});
