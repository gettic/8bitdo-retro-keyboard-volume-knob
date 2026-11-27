// Fake browser parts for the extension tests (node --test, no packages).
// The real extension files are loaded unchanged into a vm context.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const EXT = path.join(__dirname, "..", "..", "extension");

// ---- fake clock: setTimeout / setInterval / Date.now ---------------------------
class Clock {
  constructor() {
    this.now = 1000000;
    this.timers = new Map();
    this.nextId = 1;
  }
  setTimeout(fn, ms) {
    const id = this.nextId++;
    this.timers.set(id, { at: this.now + Math.max(0, Number(ms) || 0), fn, every: 0 });
    return id;
  }
  setInterval(fn, ms) {
    const id = this.nextId++;
    this.timers.set(id, { at: this.now + ms, fn, every: Math.max(1, ms) });
    return id;
  }
  clear(id) { this.timers.delete(id); }
  // Run every timer due within `ms`, in time order.
  advance(ms) {
    const end = this.now + ms;
    for (;;) {
      let best = null;
      for (const [id, t] of this.timers) {
        if (t.at <= end && (!best || t.at < best[1].at)) best = [id, t];
      }
      if (!best) break;
      const [id, t] = best;
      this.now = t.at;
      if (t.every) t.at += t.every; else this.timers.delete(id);
      t.fn();
    }
    this.now = end;
  }
  globals() {
    const clock = this;
    return {
      setTimeout: (fn, ms) => clock.setTimeout(fn, ms),
      setInterval: (fn, ms) => clock.setInterval(fn, ms),
      clearTimeout: (id) => clock.clear(id),
      clearInterval: (id) => clock.clear(id),
      Date: class extends Date {
        constructor(...a) { if (a.length) super(...a); else super(clock.now); }
        static now() { return clock.now; }
      },
    };
  }
}

// Let pending promise callbacks run (real event loop turns, not the fake clock).
async function flush(turns = 8) {
  for (let i = 0; i < turns; i++) await new Promise((r) => setImmediate(r));
}

// Advance the fake clock in small slices, letting promises run in between.
async function run(clock, ms, slice = 50) {
  await flush();
  for (let t = 0; t < ms; t += slice) {
    clock.advance(Math.min(slice, ms - t));
    await flush();
  }
}

function listeners() {
  const list = [];
  return {
    list,
    addListener(fn) { list.push(fn); },
    fire(...a) { list.slice().forEach((fn) => fn(...a)); },
  };
}

// ---- background.js ---------------------------------------------------------------
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closed = false;
    this.closeCode = null;
    FakeWebSocket.made.push(this);
    FakeWebSocket.madeAt.push(FakeWebSocket.clock.now);
  }
  send(data) {
    if (this.closed) throw new Error("closed");
    this.sent.push(JSON.parse(data));
  }
  close(code) {
    if (this.closed) return;
    this.closed = true;
    this.closeCode = code === undefined ? null : code;
    FakeWebSocket.clock.setTimeout(() => this.onclose && this.onclose({}), 0);
  }
  // test side ("the program")
  open() { this.onopen && this.onopen({}); }
  serverSend(obj) { this.onmessage && this.onmessage({ data: JSON.stringify(obj) }); }
  serverHello(protocol = 2) {
    this.serverSend({ type: "hello", protocol, version: "test", features: ["boost", "set_volume"] });
  }
  refuse() { this.closed = true; this.onclose && this.onclose({}); }
  states() { return this.sent.filter((m) => m.type === "state"); }
}
FakeWebSocket.made = [];
FakeWebSocket.madeAt = [];

function fakePort(tabId, frameId = 0, extId = "ext-id") {
  const onMessage = listeners();
  const onDisconnect = listeners();
  return {
    name: "tab",
    sender: { id: extId, tab: { id: tabId }, frameId },
    received: [],
    disconnected: false,
    onMessage,
    onDisconnect,
    postMessage(m) { this.received.push(JSON.parse(JSON.stringify(m))); },
    disconnect() { this.disconnected = true; },
    // test side ("the tab")
    send(m) { onMessage.fire(m); },
    drop() { onDisconnect.fire(); },
  };
}

function loadBackground() {
  const clock = new Clock();
  FakeWebSocket.made = [];
  FakeWebSocket.madeAt = [];
  FakeWebSocket.clock = clock;
  const onConnect = listeners();
  const chrome = {
    runtime: {
      id: "ext-id",
      lastError: undefined,
      onConnect,
      getManifest: () => JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8")),
    },
  };
  const ctx = { chrome, WebSocket: FakeWebSocket, console: { info() {}, log() {} },
                JSON, Math, Number, Array, Set, Map, Object, ...clock.globals() };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(EXT, "background.js"), "utf8"), ctx,
                  { filename: "background.js" });
  return {
    clock, ctx, sockets: FakeWebSocket.made, madeAt: FakeWebSocket.madeAt,
    connect(port) { onConnect.fire(port); return port; },
    last() { return FakeWebSocket.made[FakeWebSocket.made.length - 1]; },
  };
}

// ---- DOM bits for content.js / page.js ---------------------------------------------
class FakeElement extends EventTarget {
  constructor(tag) {
    super();
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.style = {};
    this.id = "";
    this._text = "";
    this.offsetWidth = 0;
    this.offsetHeight = 0;
    const el = this;
    const set = new Set();
    this.classList = {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c, on) => { if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); },
    };
    Object.defineProperty(this, "className", {
      get: () => Array.from(set).join(" "),
      set: (v) => { set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => set.add(c)); },
    });
    void el;
  }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(v) { this.children = []; this._text = String(v); }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    this.children.push(c);
    c.parentNode = this;
    if (this.doc && c.id) this.doc.ids.set(c.id, c);
    return c;
  }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    c.parentNode = null;
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  querySelector() { return null; }
}

class FakeDocument extends EventTarget {
  constructor() {
    super();
    this.ids = new Map();
    this.selectors = new Map(); // selector -> element
    this.prerendering = false;
    this.visibilityState = "visible";
    this.head = new FakeElement("head");
    this.head.doc = this;
    this.documentElement = this.head;
  }
  getElementById(id) { return this.ids.get(id) || null; }
  querySelector(sel) { return this.selectors.get(sel) || null; }
  createElement(tag) { const e = new FakeElement(tag); e.doc = this; return e; }
}

function storage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
}

// ---- content.js ---------------------------------------------------------------------
function loadContent({ hostname = "www.youtube.com", prerendering = false } = {}) {
  const clock = new Clock();
  const document = new FakeDocument();
  document.prerendering = prerendering;
  const window = new EventTarget();
  const ports = [];
  const chrome = {
    runtime: {
      lastError: undefined,
      connect() {
        const onMessage = listeners();
        const onDisconnect = listeners();
        const p = {
          sent: [], onMessage, onDisconnect, disconnected: false,
          postMessage(m) { this.sent.push(JSON.parse(JSON.stringify(m))); },
          disconnect() { this.disconnected = true; },
          fromWorker(m) { onMessage.fire(m); },
          drop() { onDisconnect.fire(); },
        };
        ports.push(p);
        return p;
      },
    },
  };
  const toPage = [];
  document.addEventListener("vk8bitdo:cmd", (e) => toPage.push(JSON.parse(e.detail)));
  const ctx = { chrome, document, window, location: { hostname }, CustomEvent,
                JSON, Math, Number, Array, Set, Map, Object, String, ...clock.globals() };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(EXT, "content.js"), "utf8"), ctx, { filename: "content.js" });
  return {
    clock, document, window, ports, toPage,
    fromPage(detail) { document.dispatchEvent(new CustomEvent("vk8bitdo:state", { detail: JSON.stringify(detail) })); },
    activate() { document.prerendering = false; document.dispatchEvent(new Event("prerenderingchange")); },
  };
}

// ---- page.js --------------------------------------------------------------------------
class FakeVideo extends EventTarget {
  constructor() {
    super();
    this.paused = false;
    this.ended = false;
    this.readyState = 4;
    this.volume = 0.5;
    this.muted = false;
  }
  fire(type) { this.dispatchEvent(new Event(type)); }
}

function fakePlayer(video, id = "movie_player") {
  const el = new FakeElement("div");
  el.id = id;
  el.offsetWidth = 640;
  el.offsetHeight = 360;
  el.vol = 50;
  el.muted = false;
  el.getVolume = () => el.vol;
  el.setVolume = (v) => { el.vol = v; video.volume = v / 100; video.fire("volumechange"); };
  el.mute = () => { el.muted = true; video.fire("volumechange"); };
  el.unMute = () => { el.muted = false; video.fire("volumechange"); };
  el.isMuted = () => el.muted;
  el.getPlayerState = () => (video.paused ? 2 : 1);
  el.querySelector = (sel) => (sel === "video" ? video : null);
  return el;
}

// YTM's player-bar slider. `works` = YTM's handler sets the player volume.
function fakeYtmSlider(player) {
  const s = new FakeElement("tp-yt-paper-slider");
  s.value = player.vol;
  s.works = true;
  s.curve = (v) => Math.round(v * 0.93); // YTM's curve, roughly (96 -> 89)
  s.addEventListener("value-change", () => {
    if (s.works) player.setVolume(s.curve(Number(s.value)));
  });
  return s;
}

class FakeAudioContext {
  constructor() {
    this.state = FakeAudioContext.startState;
    this.allowResume = FakeAudioContext.allowResume;
    this.currentTime = 0;
    this.destination = { connect() {} };
    this.sources = [];
    FakeAudioContext.made.push(this);
  }
  node(extra) {
    const param = (v) => ({ value: v, setTargetAtTime(x) { this.value = x; } });
    return Object.assign({ gain: param(1), connect(n) { return n; } }, extra ? extra(param) : {});
  }
  createGain() { return this.node(); }
  createDynamicsCompressor() {
    return this.node((param) => ({ threshold: param(0), knee: param(0), ratio: param(1),
                                   attack: param(0), release: param(0) }));
  }
  createMediaElementSource(v) {
    if (FakeAudioContext.taken.has(v)) throw new Error("already connected");
    this.sources.push(v);
    return { connect() {} };
  }
  setState(s) { this.state = s; if (this.onstatechange) this.onstatechange(); }
  resume() {
    if (this.allowResume) { this.setState("running"); return Promise.resolve(); }
    return new Promise(() => {}); // pending until the page had a user click
  }
}
FakeAudioContext.made = [];
FakeAudioContext.taken = new Set();
FakeAudioContext.startState = "running";
FakeAudioContext.allowResume = true;

function loadPage({ hostname = "www.youtube.com", ytm = false, audio = "running", allowResume = true } = {}) {
  const clock = new Clock();
  const document = new FakeDocument();
  const video = new FakeVideo();
  const player = fakePlayer(video);
  document.ids.set("movie_player", player);
  document.selectors.set("video", video);
  let slider = null;
  if (ytm) {
    hostname = "music.youtube.com";
    slider = fakeYtmSlider(player);
    document.selectors.set("ytmusic-player-bar #volume-slider", slider);
  }
  FakeAudioContext.made = [];
  FakeAudioContext.taken = new Set();
  FakeAudioContext.startState = audio;
  FakeAudioContext.allowResume = allowResume;
  const states = [];
  document.addEventListener("vk8bitdo:state", (e) => {
    const m = JSON.parse(e.detail);
    if ("state" in m) states.push(m.state);
  });
  const ctx = {
    document, location: { hostname, pathname: "/watch" }, CustomEvent, Event,
    AudioContext: FakeAudioContext, localStorage: storage(), sessionStorage: storage(),
    getComputedStyle: () => ({ display: "block", animationName: "none" }),
    console: { info() {}, log() {} },
    JSON, Math, Number, Array, Set, Map, WeakMap, Object, String, Promise, Error,
    ...clock.globals(),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(EXT, "page.js"), "utf8"), ctx, { filename: "page.js" });
  return {
    clock, document, video, player, slider, states,
    cmd(c) { document.dispatchEvent(new CustomEvent("vk8bitdo:cmd", { detail: JSON.stringify(c) })); },
    state() { return states[states.length - 1]; },
    audio() { return FakeAudioContext.made[0] || null; },
    box() { return player.children.find((c) => c.className.includes("vk8bitdo-box")) || null; },
  };
}

module.exports = { Clock, flush, run, FakeWebSocket, fakePort, loadBackground, loadContent, loadPage,
                   FakeAudioContext };
