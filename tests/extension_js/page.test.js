// page.js: knob maths, boost, YouTube Music slider fallback, silent-tab recovery.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadPage, run } = require("./fakes.js");

async function step(pg, delta, max = 1.5) {
  pg.cmd({ type: "volume", delta, max });
  await run(pg.clock, 400);
}

test("small steps add up exactly although the slider shows whole percent", async () => {
  const pg = loadPage();
  pg.player.vol = 50;
  await step(pg, 0.013);
  assert.equal(pg.player.vol, 51);   // 51.3
  await step(pg, 0.013);
  assert.equal(pg.player.vol, 53);   // 52.6
  await step(pg, -0.013);
  assert.equal(pg.player.vol, 51);   // 51.3
});

test("a delta under 1 % still moves one step; a turn unmutes", async () => {
  const pg = loadPage();
  pg.player.vol = 20;
  pg.player.muted = true;
  await step(pg, 0.001);
  assert.equal(pg.player.vol, 21);
  assert.equal(pg.player.muted, false);
});

test("going up stops once on 100 %, then boosts; max caps it", async () => {
  const pg = loadPage();
  pg.player.vol = 99;
  await step(pg, 0.05);
  assert.equal(pg.player.vol, 100);
  assert.equal(pg.state().volume, 1);
  await step(pg, 0.05);
  assert.equal(pg.state().volume, 1.05);
  assert.equal(pg.state().boost, "on");
  assert.equal(pg.state().max_volume, 1.5);
  for (let i = 0; i < 20; i++) await step(pg, 0.2, 1.2);
  assert.equal(pg.state().volume, 1.2);
  // and down again stops on 100 % once
  await step(pg, -0.2);
  assert.equal(pg.state().volume, 1);
  assert.equal(pg.state().boost, "off");
  assert.equal(pg.player.vol, 100);
});

test("without boost permission (max 1.0) it stays at 100 %", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.05, 1.0);
  assert.equal(pg.state().volume, 1);
  assert.equal(pg.audio(), null); // no audio processing started at all
});

test("blocked audio (no click on the page yet): stays at 100 %, reports blocked", async () => {
  const pg = loadPage({ audio: "suspended", allowResume: false });
  pg.player.vol = 100;
  await step(pg, 0.05);
  assert.equal(pg.state().volume, 1);
  assert.equal(pg.state().boost, "blocked");
  assert.equal(pg.state().max_volume, 1);
});

test("set_volume brings a boosted tab down and leaves mute alone", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.2);
  assert.equal(pg.state().volume, 1.2);
  pg.player.muted = true;
  pg.cmd({ type: "set_volume", volume: 0.5 });
  await run(pg.clock, 400);
  assert.equal(pg.player.vol, 50);
  assert.equal(pg.player.muted, true);
  assert.equal(pg.state().boost, "off");
  assert.equal(pg.state().volume, 0.5);
});

test("the user's own slider below 100 turns the boost off at once", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.3);
  assert.equal(pg.state().boost, "on");
  pg.player.setVolume(80);
  await run(pg.clock, 50);
  assert.equal(pg.state().boost, "off");
  assert.equal(pg.state().volume, 0.8);
});

test("mute is explicit; a mute toggle without a value flips it", async () => {
  const pg = loadPage();
  pg.cmd({ type: "mute", muted: true });
  await run(pg.clock, 50);
  assert.equal(pg.player.muted, true);
  pg.cmd({ type: "mute", muted: true });
  await run(pg.clock, 50);
  assert.equal(pg.player.muted, true);
  pg.cmd({ type: "mute" });
  await run(pg.clock, 50);
  assert.equal(pg.player.muted, false);
});

test("YouTube Music: a working slider is used, never the player directly", async () => {
  const pg = loadPage({ ytm: true });
  pg.slider.value = 50;
  for (let i = 0; i < 5; i++) await step(pg, 0.02);
  assert.equal(Number(pg.slider.value), 60);
  assert.equal(pg.player.vol, Math.round(60 * 0.93)); // set through YTM's own handler
});

test("YouTube Music: a slider that stops answering -> the player volume", async () => {
  const pg = loadPage({ ytm: true });
  pg.slider.value = 50;
  pg.player.vol = 46;
  pg.slider.works = false; // YTM changed its slider
  await step(pg, 0.03);    // slider 53, sound unchanged
  assert.equal(pg.player.vol, 46);
  await step(pg, 0.03);    // slider 56: 6 points without any change -> direct
  assert.equal(pg.player.vol, 46);
  await step(pg, 0.03);
  assert.equal(pg.player.vol, 49); // now direct: 46 + 3
  await step(pg, -0.03);
  assert.equal(pg.player.vol, 46);
  assert.equal(pg.state().volume, 0.46);
});

test("YouTube Music: steps that land on the same player volume are not a switch", async () => {
  const pg = loadPage({ ytm: true });
  pg.slider.curve = (v) => Math.round((v * v) / 100); // flat near the bottom
  pg.slider.value = 0;
  pg.player.vol = 0;
  for (let i = 0; i < 12; i++) await step(pg, 0.02);
  assert.equal(Number(pg.slider.value), 24);
  assert.equal(pg.player.vol, 6); // still through the slider (24^2/100)
});

test("YouTube Music: a slow first step is not a switch", async () => {
  const pg = loadPage({ ytm: true });
  pg.slider.value = 50;
  pg.slider.works = false;
  await step(pg, 0.04);       // slider 54, no change yet
  pg.slider.works = true;
  await step(pg, 0.04);       // 58: the sound follows again
  await step(pg, 0.04);       // 62
  assert.equal(Number(pg.slider.value), 62);
  assert.equal(pg.player.vol, Math.round(62 * 0.93));
});

test("boosted tab whose audio stopped: error, Reload tab hint, recovers on play", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.2);
  const ac = pg.audio();
  assert.equal(pg.state().boost, "on");
  ac.allowResume = false;
  ac.setState("suspended"); // e.g. Windows sleep / output device change
  await run(pg.clock, 1200);
  assert.equal(pg.state().boost, "error");
  await step(pg, 0.013);
  assert.equal(pg.box().textContent, "Reload tab");
  await run(pg.clock, 1000);
  // a second click within 10 s does not repeat the hint (normal indicator instead)
  await step(pg, -0.2);
  await run(pg.clock, 1000);
  assert.equal(pg.box().textContent, "100%");
  // the device is back: the next play resumes it, and boosting works again
  ac.allowResume = true;
  pg.video.fire("play");
  await run(pg.clock, 400);
  assert.equal(ac.state, "running");
  assert.equal(pg.state().boost, "off");
  assert.equal(pg.state().max_volume, 1.5);
  await step(pg, 0.1);
  assert.equal(pg.state().boost, "on");
  assert.equal(pg.state().volume, 1.1);
});

test("audio that comes back by itself clears the error", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.1);
  const ac = pg.audio();
  ac.allowResume = false;
  ac.setState("suspended");
  await run(pg.clock, 1200);
  assert.equal(pg.state().boost, "error");
  ac.setState("running");
  await run(pg.clock, 50);
  assert.equal(pg.state().boost, "on");
});

test("the boost is not saved: YouTube's saved volume stays at the slider", async () => {
  const pg = loadPage();
  pg.player.vol = 100;
  await step(pg, 0.3);
  // rememberVolume writes YouTube's own record with the slider value only
  pg.cmd({ type: "probe" });
  let probe = null;
  pg.document.addEventListener("vk8bitdo:state", (e) => {
    const m = JSON.parse(e.detail);
    if (m.probe) probe = m;
  });
  await run(pg.clock, 50);
  const saved = JSON.parse(JSON.parse(probe.saved).data);
  assert.equal(saved.volume, 100);
});
