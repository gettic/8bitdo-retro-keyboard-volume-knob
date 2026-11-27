// background.js: link to the program, retries, grace, command limits, probe.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadBackground, fakePort } = require("./fakes.js");

function ytState(volume = 0.5) {
  return { type: "state", state: { site: "youtube", playing: true, volume, muted: false } };
}

// Connect one tab and bring the link up on the first port.
function upWithTab(tabId = 7) {
  const b = loadBackground();
  const port = b.connect(fakePort(tabId));
  port.send(ytState());
  const ws = b.last();
  ws.open();
  ws.serverHello();
  return { b, port, ws };
}

test("no link without a tab; first tab opens ws://127.0.0.1:38475 and says hello", () => {
  const b = loadBackground();
  assert.equal(b.sockets.length, 0);
  b.connect(fakePort(1));
  assert.equal(b.sockets.length, 1);
  assert.equal(b.last().url, "ws://127.0.0.1:38475/volume-knob/2");
  b.last().open();
  const hello = b.last().sent[0];
  assert.equal(hello.type, "hello");
  assert.equal(hello.protocol, 2);
  assert.equal(hello.host_version, "1.6.0");
  assert.deepEqual(hello.features, ["boost", "set_volume"]);
});

test("state only after the program's hello, then a 2 s heartbeat", () => {
  const b = loadBackground();
  const port = b.connect(fakePort(7));
  port.send(ytState(0.4));
  const ws = b.last();
  ws.open();
  b.clock.advance(100);
  assert.equal(ws.states().length, 0);
  ws.serverHello();
  assert.deepEqual(ws.states()[0].tabs, [{ tab: 7, site: "youtube", playing: true, volume: 0.4, muted: false }]);
  b.clock.advance(4000);
  assert.ok(ws.states().length >= 3);
});

test("wrong hello closes and tries the next port", () => {
  const b = loadBackground();
  b.connect(fakePort(1));
  const ws = b.last();
  ws.open();
  ws.serverHello(1);
  assert.ok(ws.closed);
  b.clock.advance(1100);
  assert.equal(b.last().url, "ws://127.0.0.1:38476/volume-knob/2");
});

test("no hello within 3 s closes the socket", () => {
  const b = loadBackground();
  b.connect(fakePort(1));
  b.last().open();
  b.clock.advance(3100);
  assert.ok(b.sockets[0].closed);
});

test("retries: 1 s per port, waits between rounds never above 5 s", () => {
  const b = loadBackground();
  b.connect(fakePort(1));
  // the program is not there: every try is refused at once
  const start = b.clock.now;
  while (b.sockets.length < 16 && b.clock.now - start < 600000) {
    const s = b.last();
    if (!s.closed) s.refuse();
    b.clock.advance(50);
  }
  const gaps = b.madeAt.slice(1).map((t, i) => t - b.madeAt[i]);
  assert.ok(gaps.length >= 14);
  assert.ok(Math.max(...gaps) <= 5000, "gaps " + gaps.join(","));
  const ports = b.sockets.slice(0, 3).map((s) => s.url.split(":")[2].split("/")[0]);
  assert.deepEqual(ports, ["38475", "38476", "38477"]);
});

test("after the program restarts the extension is back within about 5 s", () => {
  const { b, ws } = upWithTab();
  ws.refuse(); // program quit
  const start = b.clock.now;
  // the program is away for 40 s: every try is refused
  while (b.clock.now - start < 40000) {
    b.clock.advance(100);
    const s = b.last();
    if (s !== ws && !s.closed) s.refuse();
  }
  const comeBack = b.clock.now;
  let answered = null;
  while (b.clock.now - comeBack < 15000 && !answered) {
    b.clock.advance(100);
    const s = b.last();
    if (s !== ws && !s.closed && !s.opened) {
      if (s.url.includes(":38475/")) { s.opened = true; s.open(); s.serverHello(); answered = b.clock.now; }
      else s.refuse();
    }
  }
  assert.ok(answered, "never reconnected");
  assert.ok(answered - comeBack <= 8000, "took " + (answered - comeBack) + " ms");
});

test("reload grace: the last tab going keeps the link for a new tab", () => {
  const { b, port, ws } = upWithTab(7);
  port.drop();
  assert.deepEqual(ws.states().at(-1).tabs, []); // the program hears it at once
  b.clock.advance(1000);
  assert.equal(ws.closed, false);
  const again = b.connect(fakePort(7));
  again.send(ytState(0.6));
  b.clock.advance(5000);
  assert.equal(ws.closed, false);
  assert.equal(b.sockets.length, 1); // same link, no new hello round trip
  assert.equal(ws.states().at(-1).tabs[0].volume, 0.6);
});

test("reload grace: the link closes 4 s after the last tab went for good", () => {
  const { b, port, ws } = upWithTab(7);
  port.drop();
  b.clock.advance(3900);
  assert.equal(ws.closed, false);
  b.clock.advance(200);
  assert.equal(ws.closed, true);
  assert.equal(ws.closeCode, 1000);
  b.clock.advance(60000);
  assert.equal(b.sockets.length, 1); // no retries without tabs
});

test("commands are limited: delta to +/-0.2, set_volume to 1.0, max to 1.5", () => {
  const { port, ws } = upWithTab(7);
  ws.serverSend({ type: "volume", tabs: [7], delta: 1.5, max: 9 });
  ws.serverSend({ type: "volume", tabs: [7], delta: -1.5 });
  ws.serverSend({ type: "volume", tabs: [7], delta: 0.013, max: 1.5 });
  ws.serverSend({ type: "set_volume", tabs: [7], volume: 1.5 });
  ws.serverSend({ type: "set_volume", tabs: [7], volume: 0.5 });
  ws.serverSend({ type: "set_volume", tabs: [7], volume: -3 });
  assert.deepEqual(port.received, [
    { type: "volume", delta: 0.2, max: 1.5 },
    { type: "volume", delta: -0.2, max: 1.0 },
    { type: "volume", delta: 0.013, max: 1.5 },
    { type: "set_volume", volume: 1.0 },
    { type: "set_volume", volume: 0.5 },
    { type: "set_volume", volume: 0 },
  ]);
});

test("targets: tabs list first, else site", () => {
  const b = loadBackground();
  const a = b.connect(fakePort(1));
  a.send(ytState());
  const m = b.connect(fakePort(2));
  m.send({ type: "state", state: { site: "ytmusic", playing: false, volume: 0.3, muted: false } });
  const ws = b.last();
  ws.open();
  ws.serverHello();
  ws.serverSend({ type: "mute", tabs: [2], muted: true });
  ws.serverSend({ type: "mute", site: "youtube", muted: false });
  ws.serverSend({ type: "mute", site: "all", muted: true });
  ws.serverSend({ type: "mute", site: "elsewhere", muted: true });
  assert.deepEqual(a.received, [{ type: "mute", muted: false }, { type: "mute", muted: true }]);
  assert.deepEqual(m.received, [{ type: "mute", muted: true }, { type: "mute", muted: true }]);
});

test("a second port for the same tab replaces the first", () => {
  const b = loadBackground();
  const first = b.connect(fakePort(3));
  const second = b.connect(fakePort(3));
  assert.ok(first.disconnected);
  first.drop(); // late disconnect of the old one must not drop the tab
  second.send(ytState(0.7));
  const ws = b.last();
  ws.open();
  ws.serverHello();
  assert.equal(ws.states()[0].tabs.length, 1);
  assert.equal(ws.states()[0].tabs[0].volume, 0.7);
});

test("ports from subframes, prerendered frames or other extensions are refused", () => {
  const b = loadBackground();
  const sub = b.connect(fakePort(4, 5));
  const other = b.connect(fakePort(4, 0, "someone-else"));
  assert.ok(sub.disconnected);
  assert.ok(other.disconnected);
  assert.equal(b.sockets.length, 0);
});

test("probe answers go out only right after the program's probe", () => {
  const { b, port, ws } = upWithTab(7);
  const answer = { type: "probe", ytSlider: "50", ytmSlider: null, saved: null, box: null };
  port.send(answer);
  assert.equal(ws.sent.filter((m) => m.type === "probe").length, 0);
  ws.serverSend({ type: "probe", tabs: [7] });
  assert.deepEqual(port.received.at(-1), { type: "probe" });
  port.send(answer);
  assert.equal(ws.sent.filter((m) => m.type === "probe").length, 1);
  b.clock.advance(2500);
  port.send(answer);
  assert.equal(ws.sent.filter((m) => m.type === "probe").length, 1);
});

test("bad tab states are ignored, volumes clamped", () => {
  const { b, port, ws } = upWithTab(7);
  port.send({ type: "state", state: { site: "evil", volume: 0.5 } });
  port.send({ type: "state", state: { site: "youtube", volume: "x" } });
  port.send({ type: "state", state: { site: "youtube", volume: 9, max_volume: 9, boost: "weird" } });
  b.clock.advance(50);
  const tab = ws.states().at(-1).tabs[0];
  assert.equal(tab.volume, 1.5);
  assert.equal(tab.max_volume, 1.5);
  assert.equal(tab.boost, undefined);
});
