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

  // YouTube Music: if moving its slider stops changing the sound (YTM changed
  // the element), read and set the player volume directly for the rest of
  // this page instead (a different loudness curve, but the knob keeps working).
  // "Not answering" = the slider moved at least YTM_STUCK_POINTS away from where
  // the sound last changed, while the sound stayed the same. (Single steps can
  // map to the same player volume on YTM's curve, mostly near the bottom, so
  // one unchanged step proves nothing; below YTM_MIN_SLIDER it is never judged.)
  let ytmDirect = false;
  let ytmAnchor = null;   // { slider, loud } where the sound last changed
  let ytmChecking = false;
  const YTM_CHECK_MS = 250;
  const YTM_STUCK_POINTS = 6;
  const YTM_MIN_SLIDER = 15;

  function ytmControl() {
    return ytmDirect ? null : ytmSlider();
  }

  function playerLoudness(p) {
    const v = playerVideo(p);
    return [Number(p.getVolume()), v ? Number(v.volume) : NaN];
  }

  function sameLoudness(a, b) {
    const same = (x, y) => x === y || (Number.isNaN(x) && Number.isNaN(y));
    return same(a[0], b[0]) && same(a[1], b[1]);
  }

  // Called before each slider move (with the slider and sound before it).
  function noteYtmBefore(sliderBefore, loudBefore) {
    if (!ytmAnchor || !sameLoudness(ytmAnchor.loud, loudBefore)) {
      ytmAnchor = { slider: sliderBefore, loud: loudBefore };
    }
  }

  function checkYtmSlider(p, s) {
    if (ytmChecking) return;
    ytmChecking = true;
    setTimeout(() => {
      ytmChecking = false;
      try {
        const now = playerLoudness(p);
        const slider = Number(s.value);
        if (!ytmAnchor || !sameLoudness(now, ytmAnchor.loud)) {
          ytmAnchor = { slider, loud: now }; // the sound follows the slider
        } else if (Math.abs(slider - ytmAnchor.slider) >= YTM_STUCK_POINTS && slider >= YTM_MIN_SLIDER) {
          ytmDirect = true;
          console.info("Volume knob: YouTube Music slider not answering, using the player volume");
          schedule();
        }
      } catch (e) { /* player gone: check again next time */ }
    }, YTM_CHECK_MS);
  }

  // The volume as the page's own slider shows it, 0-100. On YouTube that is the
  // player volume. On YouTube Music the player-bar slider maps to the player
  // volume on a curve (slider 96 -> player 89 in the live test), so the slider
  // is the value that matches what the user sees.
  function sliderValue(p) {
    const s = ytmControl();
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
      if (ctx.state === "running") {
        audioBack(); // came back (e.g. after sleep)
      } else if (ctx.state !== "running" && ctx.state !== "closed" && anyRouted()) {
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

  // A routed tab plays only through the context: if it stopped (sleep, output
  // device change), try again on play, on showing the tab and on knob clicks.
  // Resolves true if the sound path works (or was never routed).
  // The context runs again with a routed video: the boost works again
  // ("blocked" here came from a boost step tried while it was stuck).
  function audioBack() {
    if (anyRouted() && (boost.status === "error" || boost.status === "blocked")) {
      boost.status = boost.gain > 1 ? "on" : "off";
      schedule();
    }
  }

  function wakeAudio() {
    const ctx = boost.ctx;
    if (!ctx || !anyRouted() || ctx.state === "running") return Promise.resolve(true);
    if (ctx.state === "closed") return Promise.resolve(false);
    return Promise.race([
      ctx.resume().catch(() => {}),
      new Promise((r) => setTimeout(r, 300)),
    ]).then(() => {
      if (ctx.state === "running") audioBack();
      return ctx.state === "running";
    });
  }

  function setBoostGain(g) {
    boost.gain = Math.min(HARD_MAX / 100, Math.max(1, g));
    boost.status = boost.gain > 1 ? "on" : (boost.status === "on" ? "off" : boost.status);
    applyGain();
  }

  function effectivePercent(p) {
    const s = sliderValue(p);
    if (!Number.isFinite(s)) return NaN;
    // (rounded to 0.001 first, so 100 x 1.015 counts as 101.5, not 101.4999...)
    return s >= 100 ? Math.round(Math.round(s * boost.gain * 1000) / 1000) : s;
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
    const s = ytmControl();
    if (s) {
      // The player uses a different scale on YTM, so p.setVolume is only the
      // fallback once the slider is known not to work (checkYtmSlider).
      if (Number(s.value) !== percent) {
        let checked = false;
        try { noteYtmBefore(Number(s.value), playerLoudness(p)); checked = true; } catch (e) { /* no reading */ }
        try { setYtmSlider(s, percent); } catch (e) { /* slider changed shape */ }
        if (checked) checkYtmSlider(p, s);
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

  // Set the effective volume (percent, 0-150). The slider takes whole percent;
  // the boost gain keeps the fraction.
  async function setEffective(p, target) {
    target = Math.min(HARD_MAX, Math.max(0, target));
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

  // The page shows whole percent, but knob steps can be fractional (1.5 %).
  // Remember the exact target of the last knob step and count on from it while
  // the page still shows that target rounded; if the user moved the volume
  // somewhere else, start again from what the page shows.
  let exact = null;

  // ---- volume box ---------------------------------------------------------------
  // A small box in YouTube's style ("76%", "Muted", "135%" + boost bar) shown on
  // each knob click, inside the player so it also shows in fullscreen. Built with
  // createElement/textContent only (YouTube enforces Trusted Types). Not shown for
  // the program's failsafe or for the page's own controls. If no place to attach
  // is found, there is simply no box.
  const BOX_CLASS = "vk8bitdo-box";
  const BOX_MS = 800;
  const BOX_CSS = [
    "." + BOX_CLASS + "{position:absolute;left:50%;top:10%;transform:translateX(-50%);z-index:70;",
    "pointer-events:none;min-width:64px;padding:10px 18px;box-sizing:content-box;",
    "background:rgba(28,28,28,.82);color:#fff;border-radius:10px;text-align:center;",
    "font:500 22px/1.15 \"YouTube Sans\",Roboto,Arial,sans-serif;letter-spacing:.3px;",
    "box-shadow:0 2px 10px rgba(0,0,0,.35);opacity:0;transition:opacity .3s ease-out}",
    "." + BOX_CLASS + ".vk8bitdo-on{opacity:1;transition:opacity .1s ease-in}",
    "." + BOX_CLASS + " .vk8bitdo-bar{display:block;margin:7px auto 0;width:56px;height:3px;",
    "border-radius:2px;background:rgba(255,255,255,.25);overflow:hidden}",
    "." + BOX_CLASS + " .vk8bitdo-bar i{display:block;height:100%;background:#ff0033}",
    "." + BOX_CLASS + " .vk8bitdo-tag{display:block;margin-top:4px;font-size:11px;letter-spacing:.8px;",
    "color:#ff4e6a;text-transform:uppercase}",
    "." + BOX_CLASS + ".vk8bitdo-above{position:absolute;top:auto;bottom:calc(100% + 12px)}",
  ].join("");
  let boxEl = null;
  let boxTimer = 0;

  function shownSize(el) {
    return !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
  }

  // Where the box goes: YouTube -> the active player; YouTube Music -> the
  // player page (video / album art) when open, else above the player bar.
  function boxHost(p) {
    if (site === "ytmusic") {
      const page = document.querySelector("ytmusic-player#player") || document.querySelector("ytmusic-player");
      if (shownSize(page) && page.offsetHeight >= 120) return { el: page, above: false };
      const bar = document.querySelector("ytmusic-player-bar");
      if (shownSize(bar)) return { el: bar, above: true };
      return null;
    }
    return shownSize(p) ? { el: p, above: false } : null;
  }

  function ensureStyle() {
    if (document.getElementById("vk8bitdo-box-style")) return;
    const st = document.createElement("style");
    st.id = "vk8bitdo-box-style";
    st.textContent = BOX_CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  // percent: effective volume 0-150, or null for "Muted"; label: text instead.
  function showBox(p, percent, label) {
    try {
      const host = boxHost(p);
      if (!host) return;
      ensureStyle();
      if (!boxEl) {
        boxEl = document.createElement("div");
        boxEl.className = BOX_CLASS;
        boxEl.setAttribute("aria-hidden", "true");
      }
      if (boxEl.parentNode !== host.el) host.el.appendChild(boxEl);
      boxEl.classList.toggle("vk8bitdo-above", host.above);
      while (boxEl.firstChild) boxEl.removeChild(boxEl.firstChild);
      const num = document.createElement("span");
      num.textContent = label ? label : (percent === null ? "Muted" : Math.round(percent) + "%");
      boxEl.appendChild(num);
      if (!label && percent !== null && percent > 100) {
        const bar = document.createElement("span");
        bar.className = "vk8bitdo-bar";
        const fill = document.createElement("i");
        fill.style.width = Math.min(100, Math.max(0, (percent - 100) / (HARD_MAX - 100) * 100)) + "%";
        bar.appendChild(fill);
        boxEl.appendChild(bar);
        const tag = document.createElement("span");
        tag.className = "vk8bitdo-tag";
        tag.textContent = "Boost";
        boxEl.appendChild(tag);
      }
      boxEl.classList.add("vk8bitdo-on");
      if (boxTimer) clearTimeout(boxTimer);
      boxTimer = setTimeout(() => {
        boxTimer = 0;
        if (boxEl) boxEl.classList.remove("vk8bitdo-on");
      }, BOX_MS);
    } catch (e) { /* the box is only a hint: never break the volume */ }
  }

  function hideBox() {
    if (boxTimer) { clearTimeout(boxTimer); boxTimer = 0; }
    if (boxEl) boxEl.classList.remove("vk8bitdo-on");
  }

  // ---- YouTube's own volume bubble (0-100 % and Muted) ----------------------------
  // YouTube shows "95%" for its arrow keys with its "bezel" layer: a hidden
  // <div data-layer> holding .ytp-bezel-text-wrapper > .ytp-bezel-text and
  // .ytp-bezel > .ytp-bezel-icon; showing that layer (display none -> "") starts
  // its CSS fade-out animations, and YouTube hides the layer again afterwards.
  // We do the same with our value (no key presses: those would move 5 %). These
  // are YouTube internals: everything is checked, and if anything is missing or
  // doesn't start, our own box is shown instead.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICON_VOLUME = "M8,21 L12,21 L17,26 L17,10 L12,15 L8,15 L8,21 Z M19,14 L19,22 C20.48,21.32 21.5,19.77 21.5,18 C21.5,16.26 20.48,14.74 19,14 Z";
  const ICON_MUTED = "m 21.48,17.98 c 0,-1.77 -1.02,-3.29 -2.5,-4.03 v 2.21 l 2.45,2.45 c .03,-0.2 .05,-0.41 .05,-0.63 z " +
    "m 2.5,0 c 0,.94 -0.2,1.82 -0.54,2.64 l 1.51,1.51 c .66,-1.24 1.03,-2.65 1.03,-4.15 0,-4.28 -2.99,-7.86 -7,-8.76 v 2.05 " +
    "c 2.89,.86 5,3.54 5,6.71 z M 9.25,8.98 l -1.27,1.26 4.72,4.73 H 7.98 v 6 H 11.98 l 5,5 v -6.73 l 4.25,4.25 " +
    "c -0.67,.52 -1.42,.93 -2.25,1.18 v 2.06 c 1.38,-0.31 2.63,-0.95 3.69,-1.81 l 2.04,2.05 1.27,-1.27 -9,-9 -7.72,-7.72 z " +
    "m 7.72,.99 -2.09,2.08 2.09,2.09 V 9.98 z";
  let bubble = null;      // { layer, text } of the bubble we showed last
  let bubbleTimer = 0;

  function ytBezel(p) {
    if (site !== "youtube" || !p || typeof p.querySelector !== "function") return null;
    const wrap = p.querySelector(".ytp-bezel-text-wrapper");
    const text = wrap && wrap.querySelector(".ytp-bezel-text");
    const layer = wrap && wrap.parentElement;
    if (!text || !layer || layer === p || !layer.style) return null;
    const bezel = layer.querySelector(".ytp-bezel");
    const icon = bezel && bezel.querySelector(".ytp-bezel-icon");
    return { layer, wrap, text, bezel, icon };
  }

  function setIcon(icon, muted) {
    if (!icon || !document.createElementNS) return;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("height", "100%");
    svg.setAttribute("width", "100%");
    svg.setAttribute("viewBox", "0 0 36 36");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", muted ? ICON_MUTED : ICON_VOLUME);
    path.setAttribute("fill", "#fff");
    svg.appendChild(path);
    while (icon.firstChild) icon.removeChild(icon.firstChild);
    icon.appendChild(svg);
  }

  function seconds(list) {
    return String(list || "").split(",").map((s) => parseFloat(s) * (/ms\s*$/.test(s) ? 0.001 : 1))
      .filter((n) => Number.isFinite(n));
  }

  function hideBubble() {
    if (bubbleTimer) { clearTimeout(bubbleTimer); bubbleTimer = 0; }
    const b = bubble;
    bubble = null;
    // Only hide it if it still shows what we put there (YouTube may have reused it).
    if (b && b.layer.style.display !== "none" && b.textEl.textContent === b.text) b.layer.style.display = "none";
  }

  // percent: 0-100, or null for "Muted". Returns true if YouTube's bubble shows it.
  // What we change in YouTube's bubble, so it can be put back exactly if the
  // bubble doesn't work (YouTube's next play/pause bubble must look normal).
  function saveBezel(z) {
    return {
      text: z.text.textContent,
      hide: z.wrap.classList.contains("ytp-bezel-text-hide"),
      icon: z.icon ? Array.from(z.icon.childNodes || z.icon.children || []) : null,
      aria: z.bezel ? z.bezel.getAttribute("aria-label") : null,
      display: z.layer.style.display,
    };
  }

  function restoreBezel(z, s) {
    try {
      z.text.textContent = s.text;
      if (s.hide) z.wrap.classList.add("ytp-bezel-text-hide");
      if (z.icon && s.icon) {
        while (z.icon.firstChild) z.icon.removeChild(z.icon.firstChild);
        s.icon.forEach((n) => z.icon.appendChild(n));
      }
      if (z.bezel) {
        if (s.aria === null) z.bezel.removeAttribute("aria-label");
        else z.bezel.setAttribute("aria-label", s.aria);
      }
      z.layer.style.display = s.display === "" ? "none" : s.display;
    } catch (e) { /* nothing more we can do */ }
  }

  function showYtBubble(p, percent) {
    let z = null;
    let saved = null;
    try {
      z = ytBezel(p);
      if (!z) return false;
      const text = percent === null ? "Muted" : Math.round(percent) + "%";
      if (bubbleTimer) { clearTimeout(bubbleTimer); bubbleTimer = 0; }
      saved = saveBezel(z);
      z.text.textContent = text;
      z.wrap.classList.remove("ytp-bezel-text-hide");
      setIcon(z.icon, percent === null);
      if (z.bezel) z.bezel.setAttribute("aria-label", text);
      // Restart YouTube's fade-out animations: hide, force a layout, show.
      z.layer.style.display = "none";
      void z.layer.offsetWidth;
      z.layer.style.display = "";
      const cs = getComputedStyle(z.wrap);
      if (getComputedStyle(z.layer).display === "none" || !cs.animationName || cs.animationName === "none") {
        restoreBezel(z, saved);
        bubble = null;
        return false;
      }
      const dur = seconds(cs.animationDuration);
      const delay = seconds(cs.animationDelay);
      let ms = 0;
      dur.forEach((d, i) => { ms = Math.max(ms, (d + (delay[i] || 0)) * 1000); });
      if (!(ms > 0 && ms < 5000)) ms = 1000;
      bubble = { layer: z.layer, textEl: z.text, text };
      bubbleTimer = setTimeout(() => { bubbleTimer = 0; hideBubble(); }, ms + 100);
      return true;
    } catch (e) {
      if (z && saved) restoreBezel(z, saved);
      bubble = null;
      return false;
    }
  }

  // After a knob click: YouTube's own bubble for 0-100 % and Muted, our box for
  // the boost (over 100 %), on YouTube Music, and whenever the bubble fails.
  function showIndicator(p, percent) {
    try {
      if ((percent === null || percent <= 100) && showYtBubble(p, percent)) {
        hideBox();
        return;
      }
      hideBubble();
    } catch (e) { /* fall through to our box */ }
    showBox(p, percent);
  }

  // Test probe: which indicator shows now ("yt:76%" / "own:135%Boost" / null).
  function boxText() {
    if (bubble && bubble.layer.style.display !== "none") return "yt:" + bubble.textEl.textContent;
    if (!boxEl || !boxEl.parentNode || !boxEl.classList.contains("vk8bitdo-on")) return null;
    return "own:" + boxEl.textContent;
  }

  // Commands are queued so a fast knob spin is applied in order.
  let chain = Promise.resolve();
  function apply(cmd) {
    chain = chain.then(() => applyNow(cmd)).catch(() => {}).then(() => schedule());
  }

  const RELOAD_HINT = "Reload tab";
  const RELOAD_HINT_MS = 10000;
  let reloadHintAt = -Infinity;

  async function applyNow(cmd) {
    const p = findPlayer();
    if (!p) return;
    if (cmd.type !== "probe" && !(await wakeAudio())) {
      // The boosted sound path is stuck: say so on a knob click (not too often).
      if (cmd.type !== "set_volume" && Date.now() - reloadHintAt >= RELOAD_HINT_MS) {
        reloadHintAt = Date.now();
        hideBubble();
        showBox(p, null, RELOAD_HINT);
        await applyCommand(p, cmd, false);
        return;
      }
    }
    await applyCommand(p, cmd, true);
  }

  async function applyCommand(p, cmd, indicate) {
    if (cmd.type === "volume") {
      const shown = effectivePercent(p);
      if (!Number.isFinite(shown)) return;
      const old = exact !== null && Math.round(exact) === shown ? exact : shown;
      if (typeof p.isMuted === "function" && p.isMuted()) setMuted(p, false); // a volume change unmutes
      const max = Math.min(HARD_MAX, Math.max(0, Math.round((typeof cmd.max === "number" ? cmd.max : 1) * 100)));
      // Always move at least one step for a non-zero delta.
      let step = cmd.delta * 100;
      if (step > 0 && step < 1) step = 1;
      if (step < 0 && step > -1) step = -1;
      let target = Math.round((old + step) * 1000) / 1000; // no float drift
      // Crossing 100 % stops exactly on 100 % once (PROTOCOL.md).
      if ((old < 100 && target > 100) || (old > 100 && target < 100)) target = 100;
      target = Math.min(max, Math.max(0, target));
      await setEffective(p, target);
      exact = target;
      rememberVolume(p);
      if (indicate) showIndicator(p, effectivePercent(p));
    } else if (cmd.type === "set_volume") {
      exact = null;
      // Exact, and mute is left alone (used by the program's failsafe).
      await setEffective(p, cmd.volume * 100);
      rememberVolume(p);
    } else if (cmd.type === "mute") {
      const isMuted = typeof p.isMuted === "function" && p.isMuted();
      const target = typeof cmd.muted === "boolean" ? cmd.muted : !isMuted;
      setMuted(p, target);
      rememberVolume(p);
      if (indicate) showIndicator(p, target ? null : effectivePercent(p));
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
        box: boxText(),
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
    if (e.type === "play" || e.type === "playing") wakeAudio();
    schedule();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") wakeAudio();
  });
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
