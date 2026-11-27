// content.js: the page <-> worker bridge.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadContent } = require("./fakes.js");

test("connects at once and reports the tab with volume null", () => {
  const c = loadContent();
  assert.equal(c.ports.length, 1);
  assert.deepEqual(c.ports[0].sent[0],
    { type: "state", state: { site: "youtube", playing: false, volume: null, muted: false } });
  assert.deepEqual(c.toPage.at(-1), { type: "resend" });
});

test("YouTube Music is reported as ytmusic", () => {
  const c = loadContent({ hostname: "music.youtube.com" });
  assert.equal(c.ports[0].sent[0].state.site, "ytmusic");
});

test("a prerendered page connects only once it is shown", () => {
  const c = loadContent({ prerendering: true });
  assert.equal(c.ports.length, 0);
  c.clock.advance(60000);
  assert.equal(c.ports.length, 0);
  c.activate();
  assert.equal(c.ports.length, 1);
});

test("page states are cleaned and clamped", () => {
  const c = loadContent();
  c.fromPage({ state: { volume: 3, playing: true, muted: 1, max_volume: 7, boost: "on", title: "x" } });
  c.fromPage({ state: { volume: "nope" } });
  const sent = c.ports[0].sent.filter((m) => m.type === "state");
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1].state,
    { site: "youtube", playing: true, volume: 1.5, muted: false, max_volume: 1.5, boost: "on" });
});

test("worker commands go to the page, limited", () => {
  const c = loadContent();
  c.ports[0].fromWorker({ type: "volume", delta: 4, max: 9 });
  c.ports[0].fromWorker({ type: "set_volume", volume: 2 });
  c.ports[0].fromWorker({ type: "mute", muted: true });
  c.ports[0].fromWorker({ type: "evil" });
  assert.deepEqual(c.toPage.slice(-3), [
    { type: "volume", delta: 1.5, max: 1.5 },
    { type: "set_volume", volume: 1.5 },
    { type: "mute", muted: true },
  ]);
});

test("probe answers from the page pass only after a probe was asked", () => {
  const c = loadContent();
  const probes = () => c.ports[0].sent.filter((m) => m.type === "probe");
  c.fromPage({ probe: true, ytSlider: 5, saved: "s" });
  assert.equal(probes().length, 0);
  c.ports[0].fromWorker({ type: "probe" });
  c.fromPage({ probe: true, ytSlider: 5, saved: "s" });
  c.fromPage({ probe: true, ytSlider: 6, saved: "s" }); // only one answer per probe
  assert.equal(probes().length, 1);
  assert.equal(probes()[0].ytSlider, "5");
  c.ports[0].fromWorker({ type: "probe" });
  c.clock.advance(2500);
  c.fromPage({ probe: true });
  assert.equal(probes().length, 1);
});

test("reconnects after the worker went away, keeping the last state", () => {
  const c = loadContent();
  c.fromPage({ state: { volume: 0.42, playing: true } });
  c.ports[0].drop();
  c.clock.advance(900);
  assert.equal(c.ports.length, 1);
  c.clock.advance(200);
  assert.equal(c.ports.length, 2);
  assert.equal(c.ports[1].sent[0].state.volume, 0.42);
});
