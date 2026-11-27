// Isolated-world bridge, one per YouTube / YouTube Music tab:
// page.js (player API) <-> this script <-> background service worker.
(() => {
  "use strict";
  const TO_PAGE = "vk8bitdo:cmd";
  const FROM_PAGE = "vk8bitdo:state";
  const SITE = location.hostname === "music.youtube.com" ? "ytmusic" : "youtube";

  let port = null;
  let lastState = null;
  let retry = 0;
  let retryTimer = 0;
  const BACKOFF = [1000, 2000, 5000, 10000, 30000];
  const PROBE_MS = 2000; // a probe answer from the page counts only this long after a probe
  let probeUntil = 0;

  function toPage(cmd) {
    document.dispatchEvent(new CustomEvent(TO_PAGE, { detail: JSON.stringify(cmd) }));
  }

  const BOOST = new Set(["off", "on", "blocked", "unavailable", "error"]);

  // Player not loaded yet: the tab is still reported, with volume null.
  function unknownState() {
    return { site: SITE, playing: false, volume: null, muted: false };
  }

  function cleanState(s) {
    if (s === null) return unknownState();
    if (!s || typeof s !== "object") return undefined;
    const volume = Number(s.volume);
    if (!Number.isFinite(volume)) return undefined;
    const out = {
      site: SITE,
      playing: s.playing === true,
      volume: Math.min(1.5, Math.max(0, volume)),
      muted: s.muted === true,
    };
    if (typeof s.max_volume === "number" && Number.isFinite(s.max_volume)) {
      out.max_volume = Math.min(1.5, Math.max(0, s.max_volume));
    }
    if (BOOST.has(s.boost)) out.boost = s.boost;
    return out;
  }

  function post(msg) {
    if (!port) return;
    try { port.postMessage(msg); } catch (e) { port = null; reconnectSoon(); }
  }

  function connect() {
    retryTimer = 0;
    if (port) return;
    try {
      port = chrome.runtime.connect({ name: "tab" });
    } catch (e) {
      // Extension was reloaded or removed: this old script is orphaned, stop.
      port = null;
      return;
    }
    port.onMessage.addListener((cmd) => {
      if (!cmd || typeof cmd !== "object") return;
      if (cmd.type === "volume" && typeof cmd.delta === "number" && Number.isFinite(cmd.delta)) {
        const c = { type: "volume", delta: Math.max(-1.5, Math.min(1.5, cmd.delta)) };
        if (typeof cmd.max === "number" && Number.isFinite(cmd.max)) c.max = Math.max(0, Math.min(1.5, cmd.max));
        toPage(c);
      } else if (cmd.type === "set_volume" && typeof cmd.volume === "number" && Number.isFinite(cmd.volume)) {
        toPage({ type: "set_volume", volume: Math.max(0, Math.min(1.5, cmd.volume)) });
      } else if (cmd.type === "mute") {
        const c = { type: "mute" };
        if (typeof cmd.muted === "boolean") c.muted = cmd.muted;
        toPage(c);
      } else if (cmd.type === "probe") {
        probeUntil = Date.now() + PROBE_MS;
        toPage({ type: "probe" });
      }
    });
    port.onDisconnect.addListener(() => {
      void chrome.runtime.lastError;
      port = null;
      reconnectSoon();
    });
    retry = 0;
    // Report the tab at once (volume null until the player answers).
    post({ type: "state", state: lastState !== undefined ? lastState : unknownState() });
    toPage({ type: "resend" });
  }

  function reconnectSoon() {
    if (retryTimer || document.prerendering) return; // prerenderingchange connects
    const wait = BACKOFF[Math.min(retry, BACKOFF.length - 1)];
    retry += 1;
    retryTimer = setTimeout(connect, wait);
  }

  document.addEventListener(FROM_PAGE, (e) => {
    let msg;
    try { msg = JSON.parse(String(e.detail)); } catch (err) { return; }
    if (!msg || typeof msg !== "object") return;
    if (msg.probe === true) {
      if (Date.now() > probeUntil) return; // not asked for: page scripts can't use this pipe
      probeUntil = 0;
      post({
        type: "probe",
        ytSlider: msg.ytSlider == null ? null : String(msg.ytSlider).slice(0, 16),
        ytmSlider: msg.ytmSlider == null ? null : String(msg.ytmSlider).slice(0, 16),
        saved: msg.saved == null ? null : String(msg.saved).slice(0, 512),
        box: msg.box == null ? null : String(msg.box).slice(0, 32),
      });
      return;
    }
    if (!("state" in msg)) return;
    const s = cleanState(msg.state);
    if (s === undefined) return;
    lastState = s;
    post({ type: "state", state: s });
  });

  // Back/forward cache: the port dies on pagehide, come back on pageshow.
  window.addEventListener("pageshow", (e) => { if (e.persisted) connect(); });
  window.addEventListener("pagehide", () => {
    if (port) { try { port.disconnect(); } catch (e) { /* ignore */ } port = null; }
  });

  lastState = undefined;
  // A prerendered page (address-bar prerender) is not a tab's main frame yet:
  // the worker would refuse it, so connect once it is shown.
  document.addEventListener("prerenderingchange", () => connect(), { once: true });
  if (!document.prerendering) connect();
})();
