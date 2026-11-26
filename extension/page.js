// Runs inside the YouTube / YouTube Music page (MAIN world), so it can use the
// player's own API. Using the player API (not <video>.volume) keeps the player's
// slider in sync and lets YouTube save the volume for the next video.
// Talks to content.js only through CustomEvents on document.
//
// Volume model (PROTOCOL.md v2): the tab's effective volume is 0.0-1.5.
//   0-100 %   = the page's own slider (gain 1.0)
//   100-150 % = slider at 100 + a Web Audio gain of 1.0-1.5 ("boost").
// The boost is never saved: a reload starts at gain 1.0 again.
(() => {
  "use strict";
  if (window.__vkPageLoaded) return;
  window.__vkPageLoaded = true;

  const TO_CONTENT = "vk8bitdo:state";
  const FROM_CONTENT = "vk8bitdo:cmd";
  const site = location.hostname === "music.youtube.com" ? "ytmusic" : "youtube";
  const HARD_MAX = 150; // percent; the program's "max" can only lower this

  function playerVideo(p) {
    return p && p.querySelector ? p.querySelector("video") : null;
  }

  function usable(p) {
    return p && typeof p.getVolume === "function" && typeof p.setVolume === "function";
  }

  // The active player: on Shorts there are two; the one whose video plays wins.
  function findPlayer() {
    const list = [];
    for (const id of ["shorts-player", "movie_player"]) {
      const p = document.getElementById(id);
      if (usable(p)) list.push(p);
    }
    for (const p of list) {
      const v = playerVideo(p);
      if (v && !v.paused && !v.ended) return p;
    }
    if (location.pathname.startsWith("/shorts/") && list.length && list[0].id === "shorts-player") {
      return list[0];
    }
    return list.find((p) => p.id === "movie_player") || list[0] || null;
  }

  function ytmSlider() {
    return site === "ytmusic" ? document.querySelector("ytmusic-player-bar #volume-slider") : null;
  }

  // The volume as the page's own slider shows it, 0-100. On YouTube that is the
  // player volume. On YouTube Music the player-bar slider maps to the player
  // volume on a curve (slider 96 -> player 89 in the live test), so the slider
  // is the value that matches what the user sees.
  function sliderValue(p) {
    const s = ytmSlider();
    if (s) {
      const n = Number(s.value);
      if (Number.isFinite(n)) return n;
    }
    const vol = Number(p.getVolume());
    return Number.isFinite(vol) ? vol : NaN;
  }

  // ---- boost (Web Audio) ---------------------------------------------------------
  // The graph is only built on the first step over 100 %. A video element is only
  // routed into it while the AudioContext is running: a routed element plays only
  // through the context, so routing into a suspended one would silence the tab.
  const boost = {
    gain: 1.0,          // the gain we want (1.0-1.5)
    status: "off",      // off | on | blocked | unavailable | error
    ctx: null,
    gainNode: null,
    dry: null,
    wet: null,
    trim: 1.0,          // cancels the compressor's automatic make-up gain
    routed: new WeakMap(), // video -> true (routed) | false (could not be routed)
  };

  function buildGraph() {
    if (boost.ctx) return boost.ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    const ctx = new AC({ latencyHint: "interactive" });
    const g = ctx.createGain();
    const dry = ctx.createGain();
    const wet = ctx.createGain();
    const comp = ctx.createDynamicsCompressor();
    // Limiter: keeps boosted peaks under full scale instead of clipping.
    comp.threshold.value = -2;
    comp.knee.value = 0;
    comp.ratio.value = 20;
    comp.attack.value = 0.002;
    comp.release.value = 0.15;
    dry.gain.value = 1;
    wet.gain.value = 0;
    g.connect(dry).connect(ctx.destination);
    g.connect(comp).connect(wet).connect(ctx.destination);
    ctx.onstatechange = () => {
      if (ctx.state !== "running" && ctx.state !== "closed" && anyRouted()) {
        ctx.resume().catch(() => {});
        setTimeout(() => {
          if (ctx.state !== "running") { boost.status = "error"; schedule(); }
        }, 1000);
      }
    };
    boost.ctx = ctx;
    boost.gainNode = g;
    boost.dry = dry;
    boost.wet = wet;
    measureMakeup(comp).then((m) => { if (m > 0.5 && m < 2) boost.trim = 1 / m; applyGain(); });
    return ctx;
  }

  // The compressor adds a fixed make-up gain; measure it once (offline, a quiet
  // tone below the threshold) and cancel it, so 1.2 really means 1.2.
  async function measureMakeup(liveComp) {
    try {
      const rate = 48000;
      const off = new OfflineAudioContext(1, rate / 2, rate);
      const osc = off.createOscillator();
      osc.frequency.value = 441;
      const quiet = off.createGain();
      quiet.gain.value = 0.1; // -20 dBFS
      const c = off.createDynamicsCompressor();
      for (const k of ["threshold", "knee", "ratio", "attack", "release"]) c[k].value = liveComp[k].value;
      osc.connect(quiet).connect(c).connect(off.destination);
      osc.start();
      const buf = await off.startRendering();
      const d = buf.getChannelData(0);
      let sum = 0;
      let n = 0;
      for (let i = rate / 4; i < d.length; i++) { sum += d[i] * d[i]; n++; }
      const rms = Math.sqrt(sum / n);
      return rms / (0.1 / Math.SQRT2);
    } catch (e) {
      return 1;
    }
  }

  let routedCount = 0;
  function anyRouted() { return routedCount > 0; }

  // Route one video element into the graph. Returns true if it is routed.
  function routeVideo(v) {
    if (!v || !boost.ctx || boost.ctx.state !== "running") return false;
    const done = boost.routed.get(v);
    if (done !== undefined) return done;
    try {
      const src = boost.ctx.createMediaElementSource(v);
      src.connect(boost.gainNode);
      boost.routed.set(v, true);
      routedCount++;
      return true;
    } catch (e) {
      // Already routed by someone else (e.g. another audio extension).
      boost.routed.set(v, false);
      return false;
    }
  }

  // Smoothly move to boost.gain; the limiter branch is only used above 1.0, so
  // at 100 % and below the sound is untouched.
  function applyGain() {
    if (!boost.ctx) return;
    const t = boost.ctx.currentTime;
    const g = boost.gain;
    boost.gainNode.gain.setTargetAtTime(g, t, 0.02);
    boost.dry.gain.setTargetAtTime(g > 1 ? 0 : 1, t, 0.01);
    boost.wet.gain.setTargetAtTime(g > 1 ? boost.trim : 0, t, 0.01);
  }

  // Try to get a running context and a routed video. Resolves true/false.
  let starting = null;
  function startBoost(v) {
    if (starting) return starting;
    starting = (async () => {
      const ctx = buildGraph();
      if (!ctx) { boost.status = "unavailable"; return false; }
      if (ctx.state !== "running") {
        // resume() stays pending until the page has had a user click: wait briefly.
        await Promise.race([ctx.resume().catch(() => {}), new Promise((r) => setTimeout(r, 300))]);
      }
      if (ctx.state !== "running") { boost.status = "blocked"; return false; }
      if (!routeVideo(v)) { boost.status = "unavailable"; return false; }
      return true;
    })().finally(() => { starting = null; });
    return starting;
  }

  function setBoostGain(g) {
    boost.gain = Math.min(HARD_MAX / 100, Math.max(1, g));
    boost.status = boost.gain > 1 ? "on" : (boost.status === "on" ? "off" : boost.status);
    applyGain();
  }

  function effectivePercent(p) {
    const s = sliderValue(p);
    if (!Number.isFinite(s)) return NaN;
    return s >= 100 ? Math.round(s * boost.gain) : s;
  }

  // ---- state ----------------------------------------------------------------------
  function readState() {
    const p = findPlayer();
    if (!p) return null;
    const v = playerVideo(p);
    const s = sliderValue(p);
    if (!Number.isFinite(s)) return null;
    // The user moved the page's own slider below 100 while boosted: boost off.
    if (s < 100 && boost.gain > 1) setBoostGain(1);
    const state = typeof p.getPlayerState === "function" ? p.getPlayerState() : -1;
    const playing = state === 1 || (!!v && !v.paused && !v.ended && v.readyState > 2);
    const muted = typeof p.isMuted === "function" ? !!p.isMuted() : !!(v && v.muted);
    const eff = Math.min(HARD_MAX, Math.max(0, effectivePercent(p)));
    // PROTOCOL.md: 1.5 only once a boost is known to work in this tab.
    const canBoost = !!boost.ctx && boost.ctx.state === "running" && routedCount > 0 &&
      (boost.status === "on" || boost.status === "off");
    return {
      site, playing, volume: eff / 100, muted,
      max_volume: canBoost ? HARD_MAX / 100 : 1.0, boost: boost.status,
    };
  }

  // YouTube Music: move its player-bar slider the way a user would, so YTM's
  // own handler sets the player volume, saves it and the slider shows it.
  function setYtmSlider(s, percent) {
    s.value = percent;
    s.dispatchEvent(new CustomEvent("immediate-value-change", { bubbles: true }));
    s.dispatchEvent(new CustomEvent("value-change", { bubbles: true }));
    s.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setSlider(p, percent) {
    percent = Math.round(Math.min(100, Math.max(0, percent)));
    const s = ytmSlider();
    if (s) {
      // No fallback to p.setVolume here: the player uses a different scale on
      // YTM, so a direct call would jump the sound to a different loudness.
      if (Number(s.value) !== percent) {
        try { setYtmSlider(s, percent); } catch (e) { /* slider changed shape: no change */ }
      }
      return;
    }
    if (Number(p.getVolume()) !== percent) p.setVolume(percent);
  }

  function setMuted(p, muted) {
    if (muted) {
      if (typeof p.mute === "function") p.mute();
    } else if (typeof p.unMute === "function") {
      p.unMute();
    }
  }

  // YouTube only remembers the volume for the next page load when its own
  // slider is used (it writes "yt-player-volume"); the player API doesn't. So
  // write the same record YouTube writes, so a reload keeps the slider volume.
  // (Only the slider, 0-100: the boost is never saved.)
  function rememberVolume(p) {
    if (site !== "youtube") return;
    try {
      const volume = Math.round(Number(p.getVolume()));
      const muted = typeof p.isMuted === "function" && !!p.isMuted();
      if (!Number.isFinite(volume)) return;
      const now = Date.now();
      const data = JSON.stringify({ volume, muted });
      localStorage.setItem("yt-player-volume",
        JSON.stringify({ data, expiration: now + 30 * 24 * 3600 * 1000, creation: now }));
      sessionStorage.setItem("yt-player-volume", JSON.stringify({ data, creation: now }));
    } catch (e) { /* storage blocked: only this page keeps the volume */ }
  }

  // Set the effective volume (percent, 0-150).
  async function setEffective(p, target) {
    target = Math.round(Math.min(HARD_MAX, Math.max(0, target)));
    if (target <= 100) {
      // Down from a boost: the gain goes first, so it is never loud for a moment.
      if (boost.gain > 1) setBoostGain(1);
      setSlider(p, target);
      return;
    }
    setSlider(p, 100);
    const v = playerVideo(p);
    const ok = await startBoost(v);
    if (ok) setBoostGain(target / 100);
  }

  // Commands are queued so a fast knob spin is applied in order.
  let chain = Promise.resolve();
  function apply(cmd) {
    chain = chain.then(() => applyNow(cmd)).catch(() => {}).then(() => schedule());
  }

  async function applyNow(cmd) {
    const p = findPlayer();
    if (!p) return;
    if (cmd.type === "volume") {
      const old = effectivePercent(p);
      if (!Number.isFinite(old)) return;
      if (typeof p.isMuted === "function" && p.isMuted()) setMuted(p, false); // a volume change unmutes
      const max = Math.min(HARD_MAX, Math.max(0, Math.round((typeof cmd.max === "number" ? cmd.max : 1) * 100)));
      // Always move at least one step for a non-zero delta.
      let step = cmd.delta * 100;
      if (step > 0 && step < 1) step = 1;
      if (step < 0 && step > -1) step = -1;
      let target = Math.round(old + step);
      // Crossing 100 % stops exactly on 100 % once (PROTOCOL.md).
      if ((old < 100 && target > 100) || (old > 100 && target < 100)) target = 100;
      target = Math.min(max, Math.max(0, target));
      await setEffective(p, target);
      rememberVolume(p);
    } else if (cmd.type === "set_volume") {
      // Exact, and mute is left alone (used by the program's failsafe).
      await setEffective(p, cmd.volume * 100);
      rememberVolume(p);
    } else if (cmd.type === "mute") {
      const isMuted = typeof p.isMuted === "function" && p.isMuted();
      const target = typeof cmd.muted === "boolean" ? cmd.muted : !isMuted;
      setMuted(p, target);
      rememberVolume(p);
    } else if (cmd.type === "probe") {
      // Used by the tests only: report what the page's own controls show.
      const s = ytmSlider();
      const panel = document.querySelector(".ytp-volume-panel");
      let saved = null;
      try {
        saved = localStorage.getItem("yt-player-volume") || sessionStorage.getItem("yt-player-volume");
      } catch (e) { /* ignore */ }
      send({
        probe: true,
        ytSlider: panel ? panel.getAttribute("aria-valuenow") : null,
        ytmSlider: s ? s.value : null,
        saved,
      });
    }
  }

  function send(detail) {
    document.dispatchEvent(new CustomEvent(TO_CONTENT, { detail: JSON.stringify(detail) }));
  }

  // Reports are coalesced with a microtask, not a timer: Brave throttles timers
  // in background tabs (a paused YouTube tab behind others), which delayed
  // state updates by many seconds in the live test.
  let last = "";
  let queued = false;
  function report(force) {
    queued = false;
    const s = readState();
    const text = JSON.stringify(s);
    if (force === true || text !== last) {
      last = text;
      send({ state: s });
    }
  }
  function schedule() {
    if (!queued) {
      queued = true;
      Promise.resolve().then(report);
    }
  }

  document.addEventListener(FROM_CONTENT, (e) => {
    let cmd;
    try { cmd = JSON.parse(String(e.detail)); } catch (err) { return; }
    if (!cmd || typeof cmd !== "object") return;
    if (cmd.type === "resend") { report(true); return; }
    if (cmd.type === "volume" && !(typeof cmd.delta === "number" && Number.isFinite(cmd.delta))) return;
    if (cmd.type === "set_volume" && !(typeof cmd.volume === "number" && Number.isFinite(cmd.volume))) return;
    apply(cmd);
  });

  // Watch the <video> element (YouTube swaps it on navigation; Shorts use a pool).
  let watched = null;
  const EVENTS = ["play", "playing", "pause", "ended", "volumechange", "emptied", "loadeddata"];
  function onVideoEvent(e) {
    // A boosted tab: a video that starts playing gets routed too.
    if (boost.gain > 1 && (e.type === "play" || e.type === "playing")) routeVideo(e.target);
    schedule();
  }
  function watch() {
    const p = findPlayer();
    const v = playerVideo(p) || document.querySelector("video");
    if (v !== watched) {
      if (watched) EVENTS.forEach((n) => watched.removeEventListener(n, onVideoEvent));
      watched = v;
      if (v) {
        EVENTS.forEach((n) => v.addEventListener(n, onVideoEvent));
        if (boost.gain > 1 && !routeVideo(v)) {
          // The new element can't be boosted: fall back to 100 % rather than lie.
          setBoostGain(1);
          if (boost.routed.get(v) === false) boost.status = "unavailable";
        }
      }
      schedule();
    }
  }
  ["yt-navigate-finish", "yt-page-data-updated", "yt-player-updated"].forEach((n) =>
    document.addEventListener(n, () => { watch(); schedule(); }));
  setInterval(() => { watch(); schedule(); }, 1000);
  watch();
})();
