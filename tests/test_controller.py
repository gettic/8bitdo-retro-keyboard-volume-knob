"""Controller + browser bridge with fake Core Audio, fake clock and fake browser link."""
import logging
import threading
import unittest

from knob import coreaudio, hotkeys
from knob.controller import Controller

from tests.fakes import (FakeAudio, FakeClock, FakeSession, ProcessList, connect_host,
                         make_bridge, settings)

STEP = settings()["step"]  # one knob click (default setting)


def yt(tid, playing=False, muted=False, site="youtube"):
    return {"tab": tid, "site": site, "playing": playing, "volume": 0.5, "muted": muted}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)
        self.spotify = FakeSession("spotify.exe", volume=0.5)
        self.brave = FakeSession("brave.exe", volume=0.8, pid=200)
        self.audio = FakeAudio([self.spotify, self.brave])
        self.ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                              process_lister=ProcessList())
        self.ctl.audio = self.audio

    def tick(self):
        self.ctl.poll()

    def test_spotify_playing_only_spotify_changes(self):
        connect_host(self.bridge, 1, [yt(7, playing=False)])
        self.spotify._peak = 0.3
        self.tick()
        self.ctl.handle("up")
        self.assertAlmostEqual(self.spotify._volume, 0.5 + STEP)
        self.assertEqual([m["type"] for _, m in self.rec.sent], ["hello"])
        self.assertEqual(self.brave._volume, 0.8)

    def test_youtube_playing_only_tab_changes(self):
        connect_host(self.bridge, 1, [yt(7, playing=True), yt(8)])
        self.tick()
        self.ctl.handle("down")
        self.assertEqual(self.spotify._volume, 0.5)
        self.assertEqual(self.rec.sent[-1],
                         (1, {"type": "volume", "delta": -STEP, "max": 1.5, "tabs": [7]}))
        self.assertEqual(self.brave._volume, 0.8)  # extension connected: Brave untouched

    def test_nothing_playing_changes_both(self):
        connect_host(self.bridge, 1, [yt(7), yt(9, site="ytmusic")])
        self.tick()
        self.ctl.handle("up")
        self.assertAlmostEqual(self.spotify._volume, 0.5 + STEP)
        self.assertEqual(self.rec.sent[-1], (1, {"type": "volume", "delta": STEP, "max": 1.5, "tabs": [7, 9]}))
        self.assertEqual(self.brave._volume, 0.8)

    def test_playing_lasts_hold_time(self):
        self.spotify._peak = 0.3
        self.tick()
        self.spotify._peak = 0.0
        connect_host(self.bridge, 1, [yt(7)])
        self.clock.advance(1.0)
        self.tick()
        self.ctl.handle("up")
        self.assertEqual(len(self.rec.sent), 1)  # only hello: spotify still counts as playing
        self.clock.advance(1.0)
        connect_host(self.bridge, 1, [yt(7)])
        self.tick()
        self.ctl.handle("up")
        self.assertEqual(self.rec.sent[-1][1]["type"], "volume")  # now nothing plays: both

    def test_mute_toggles_all_together(self):
        connect_host(self.bridge, 1, [yt(7)])
        self.tick()
        self.ctl.handle("mute")
        self.assertTrue(self.spotify._muted)
        self.assertEqual(self.rec.sent[-1], (1, {"type": "mute", "muted": True, "tabs": [7]}))
        connect_host(self.bridge, 1, [yt(7, muted=True)])
        self.ctl.handle("mute")
        self.assertFalse(self.spotify._muted)
        self.assertEqual(self.rec.sent[-1], (1, {"type": "mute", "muted": False, "tabs": [7]}))

    def test_volume_unmutes_and_clamps(self):
        self.spotify._muted = True
        self.spotify._volume = 1.0 - STEP / 2
        self.tick()
        self.ctl.handle("up")
        self.assertFalse(self.spotify._muted)
        self.assertEqual(self.spotify._volume, 1.0)
        self.spotify._volume = STEP / 2
        self.ctl.handle("down")
        self.assertEqual(self.spotify._volume, 0.0)

    def test_full_range_reaches_exact_ends(self):
        # about 2 knob turns from 0 to 100 %; float steps still end exactly on 0 and 1
        self.assertEqual(STEP, 0.013)
        self.spotify._volume = 0.0
        self.spotify._peak = 0.3
        self.tick()
        clicks = 0
        while self.spotify._volume < 1.0 and clicks < 200:
            self.ctl.handle("up")
            clicks += 1
        self.assertEqual((self.spotify._volume, clicks), (1.0, 77))
        clicks = 0
        while self.spotify._volume > 0.0 and clicks < 200:
            self.ctl.handle("down")
            clicks += 1
        self.assertEqual((self.spotify._volume, clicks), (0.0, 77))

    def test_brave_fallback_without_extension(self):
        self.brave._peak = 0.5
        self.tick()
        self.ctl.handle("down")
        self.assertAlmostEqual(self.brave._volume, 0.8 - STEP)
        self.assertEqual(self.spotify._volume, 0.5)  # Brave plays, Spotify does not

    def test_brave_fallback_off(self):
        self.ctl.settings = settings(brave_fallback=False)
        self.tick()
        self.ctl.handle("up")
        self.assertEqual(self.brave._volume, 0.8)
        self.assertAlmostEqual(self.spotify._volume, 0.5 + STEP)

    def test_stale_host_falls_back(self):
        self.brave._peak = 0.5  # the fallback only changes a Brave that plays
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.clock.advance(6)
        self.tick()
        self.ctl.handle("up")
        self.assertEqual(len(self.rec.sent), 1)  # stale tab gets no command
        self.assertAlmostEqual(self.brave._volume, 0.8 + STEP)

    def test_other_apps_never_touched(self):
        other = FakeSession("game.exe", volume=0.3, peak=0.9)
        self.audio.all.append(other)
        self.ctl.refresh(force=True)
        self.tick()
        self.ctl.handle("up")
        self.ctl.handle("mute")
        self.assertEqual((other._volume, other._muted), (0.3, False))

    def test_session_error_is_survived(self):
        self.tick()
        self.spotify.fail = True
        self.ctl.handle("up")  # must not raise
        self.spotify.fail = False
        self.assertIsNone(self.ctl._refreshed)

    def test_run_loop_with_fake_audio(self):
        self.spotify._peak = 0.3
        t = threading.Thread(target=self.ctl.run)
        t.start()
        self.ctl.press("up")
        self.ctl.press("up")
        self.ctl.stop()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertAlmostEqual(self.spotify._volume, 0.5 + 2 * STEP)
        self.assertTrue(self.audio.closed)
        self.assertTrue(self.spotify.released)

    def test_sessions_kept_until_an_error(self):
        self.tick()
        for _ in range(3):
            self.clock.advance(2.0)
            self.tick()                              # normal 2 s refreshes
        self.assertEqual(self.audio.resets, 0)
        self.assertFalse(self.spotify.released)      # the audio object owns them
        self.spotify.fail = True
        self.ctl.handle("up")                        # a session call fails ...
        self.spotify.fail = False
        self.tick()                                  # ... so the next refresh starts clean
        self.assertEqual(self.audio.resets, 1)
        self.clock.advance(2.0)
        self.tick()
        self.assertEqual(self.audio.resets, 1)

    def test_tab_delta_never_over_0_2(self):
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.tick()
        for _ in range(300):                        # a very fast spin: one command per click
            self.ctl.handle("up")
        vols = [m for _, m in self.rec.sent if m["type"] == "volume"]
        self.assertEqual(len(vols), 300)
        self.assertTrue(all(abs(m["delta"]) <= 0.2 for m in vols))
        self.ctl.settings = settings(step=0.5)      # even a hand-broken step is capped
        self.ctl.handle("down")
        self.assertEqual(self.rec.sent[-1][1]["delta"], -0.2)

    # --- the Brave fallback leaves nothing behind (v1.5.2) ---
    def test_idle_brave_not_changed_when_nothing_plays(self):
        self.tick()
        self.ctl.handle("down")
        self.ctl.handle("mute")
        self.assertEqual((self.brave._volume, self.brave._muted), (0.8, False))
        self.assertTrue(self.spotify._muted)

    def test_fallback_change_put_back_when_extension_connects(self):
        self.brave._peak = 0.5
        self.tick()
        self.ctl.handle("down")
        self.ctl.handle("mute")
        self.assertTrue(self.brave._muted)
        self.assertAlmostEqual(self.brave._volume, 0.8 - STEP)
        connect_host(self.bridge, 1, [yt(7)])
        self.tick()
        self.assertEqual((self.brave._volume, self.brave._muted), (0.8, False))
        self.brave._volume = 0.3                    # changed by hand later: left alone
        self.tick()
        self.assertEqual(self.brave._volume, 0.3)

    def test_brave_never_touched_by_knob_nothing_to_put_back(self):
        connect_host(self.bridge, 1, [yt(7)])
        self.brave._volume = 0.4
        self.tick()
        self.assertEqual(self.brave._volume, 0.4)
        self.assertEqual(self.ctl._brave_orig, {})

    def test_closed_brave_forgotten(self):
        self.brave._peak = 0.5
        self.tick()
        self.ctl.handle("down")
        self.audio.all.remove(self.brave)
        self.ctl.refresh(force=True)
        self.assertEqual(self.ctl._brave_orig, {})

    # --- Mute / 0 % keep their target (v1.5.2) ---
    def silence(self, session):
        """A muted / 0 % app makes no sound: its meter reads 0."""
        session._peak = 0.0
        self.clock.advance(2.0)  # past hold_seconds
        connect_host(self.bridge, 1, [yt(7)])
        self.tick()

    def test_second_mute_unmutes_the_muted_app_only(self):
        connect_host(self.bridge, 1, [yt(7)])
        self.spotify._peak = 0.3
        self.tick()
        self.ctl.handle("mute")
        self.assertTrue(self.spotify._muted)
        self.silence(self.spotify)
        self.ctl.handle("mute")
        self.assertFalse(self.spotify._muted)
        self.assertEqual({m["type"] for _, m in self.rec.sent}, {"hello"})  # tab untouched

    def test_turned_to_zero_stays_the_target(self):
        connect_host(self.bridge, 1, [yt(7)])
        self.spotify._peak = 0.3
        self.spotify._volume = STEP / 2
        self.tick()
        self.ctl.handle("down")
        self.assertEqual(self.spotify._volume, 0.0)
        self.silence(self.spotify)
        self.ctl.handle("up")
        self.assertAlmostEqual(self.spotify._volume, STEP)
        self.assertEqual({m["type"] for _, m in self.rec.sent}, {"hello"})

    def test_quiet_app_dropped_when_something_else_plays(self):
        self.spotify._peak = 0.3
        self.tick()
        self.ctl.handle("mute")
        self.silence(self.spotify)
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.ctl.handle("up")
        self.assertTrue(self.spotify._muted)        # a turn for the video doesn't unmute it
        self.assertEqual(self.rec.sent[-1][1]["type"], "volume")

    def test_mute_all_then_unmute_all(self):
        connect_host(self.bridge, 1, [yt(7)])
        self.tick()
        self.ctl.handle("mute")                     # nothing played: everything muted
        connect_host(self.bridge, 1, [yt(7, muted=True)])
        self.clock.advance(2.0)
        self.tick()
        self.ctl.handle("mute")                     # and everything unmuted again
        self.assertFalse(self.spotify._muted)
        self.assertEqual(self.rec.sent[-1][1], {"type": "mute", "muted": False, "tabs": [7]})

    # --- Windows audio not answering (v1.5.2) ---
    def test_audio_failure_keys_still_reach_tabs(self):
        logs = []
        handler = logging.Handler()
        handler.emit = lambda r: logs.append(r)
        lg = logging.getLogger("knob.controller")
        lg.addHandler(handler)
        self.addCleanup(lg.removeHandler, handler)
        self.addCleanup(lg.setLevel, lg.level)
        lg.setLevel(logging.DEBUG)

        def broken(apps=None):
            self.audio.calls += 1
            raise OSError("audio service restarting")
        good = self.audio.sessions
        self.audio.sessions = broken
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.ctl._refreshed = None
        for _ in range(10):
            self.tick()
            self.clock.advance(0.2)
        self.assertEqual(self.audio.calls, 1)  # retried on the 2 s refresh only
        self.ctl.handle("up")
        self.assertEqual(self.rec.sent[-1][1]["type"], "volume")
        self.clock.advance(0.2)
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.tick()
        self.assertEqual(self.audio.calls, 2)
        self.assertEqual(sum(r.levelname == "WARNING" for r in logs), 1)
        self.audio.sessions = good
        self.clock.advance(2.0)
        self.tick()
        self.assertTrue(any("available again" in r.getMessage() for r in logs))
        self.assertTrue(self.ctl._sessions)

    def test_run_handles_key_when_poll_fails(self):
        connect_host(self.bridge, 1, [yt(7, playing=True)])
        self.ctl.poll = lambda: (_ for _ in ()).throw(RuntimeError("poll broke"))
        lg = logging.getLogger("knob.controller")
        lg.disabled = True
        self.addCleanup(setattr, lg, "disabled", False)
        t = threading.Thread(target=self.ctl.run)
        t.start()
        self.ctl.press("up")
        self.ctl.stop()
        t.join(5)
        self.assertEqual(self.rec.sent[-1][1]["type"], "volume")


def boosted(tid, volume=1.3, site="youtube", boost="on", max_volume=1.5):
    return {"tab": tid, "site": site, "playing": True, "volume": volume, "muted": False,
            "max_volume": max_volume, "boost": boost}


class BoostAndFailsafeTests(unittest.TestCase):
    """Controller + failsafe through the bridge, as the real program wires them."""

    def setUp(self):
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)
        self.spotify = FakeSession("spotify.exe", volume=0.9)
        self.audio = FakeAudio([self.spotify])
        self.procs = ProcessList()
        self.ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                              process_lister=self.procs)
        self.ctl.audio = self.audio

    def drain(self):
        """Process queued bridge events the way the audio thread does."""
        while not self.ctl.keys.empty():
            ev = self.ctl.keys.get_nowait()
            self.ctl.poll()
            if isinstance(ev, tuple):
                self.ctl.on_event(ev)

    def sent(self, kind):
        return [(c, m) for c, m in self.rec.sent if m["type"] == kind]

    def test_start_resets_boosted_tab_not_spotify(self):
        connect_host(self.bridge, 1, [boosted(7), boosted(8, volume=0.8, boost="off")])
        self.drain()
        self.assertEqual(self.sent("set_volume"),
                         [(1, {"type": "set_volume", "volume": 0.5, "tabs": [7]})])
        self.assertEqual(self.spotify._volume, 0.9)  # never touched by the failsafe

    def test_new_tab_with_spotify_open_not_reset(self):
        self.clock.advance(60)
        self.ctl.refresh(force=True)
        connect_host(self.bridge, 1, [boosted(7)])
        self.drain()
        self.assertEqual(self.sent("set_volume"), [])

    def test_new_tab_with_spotify_process_only_not_reset(self):
        self.audio.all = []
        self.procs.running = {"spotify.exe"}
        self.clock.advance(60)
        connect_host(self.bridge, 1, [boosted(7)])
        self.drain()
        self.assertEqual(self.sent("set_volume"), [])

    def test_new_tab_alone_reset(self):
        self.audio.all = []
        self.clock.advance(60)
        connect_host(self.bridge, 1, [boosted(7)])
        self.drain()
        self.assertEqual(len(self.sent("set_volume")), 1)

    def test_host_without_set_volume_gets_none(self):
        connect_host(self.bridge, 1, [boosted(7)], features=["boost"])
        self.drain()
        self.assertEqual(self.sent("set_volume"), [])

    def test_failsafe_off(self):
        self.ctl.failsafe.enabled = False
        connect_host(self.bridge, 1, [boosted(7)])
        self.drain()
        self.assertEqual(self.sent("set_volume"), [])

    def test_max_per_tab(self):
        self.audio.all = []
        self.clock.advance(60)
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="off"),
                                      boosted(8, 0.9, boost="unavailable", max_volume=1.0),
                                      boosted(9, 0.9, boost="blocked", max_volume=1.0)])
        self.drain()
        self.ctl.handle("up")
        vols = [m for _, m in self.sent("volume")]
        self.assertEqual(vols, [{"type": "volume", "delta": STEP, "max": 1.0, "tabs": [8]},
                                {"type": "volume", "delta": STEP, "max": 1.5, "tabs": [7, 9]}])

    def test_host_without_boost_gets_max_1(self):
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="off")], features=[])
        self.ctl.handle("up")
        self.assertEqual(self.sent("volume")[0][1]["max"], 1.0)

    def test_boost_max_setting(self):
        self.ctl.settings = settings(boost_max=1.2)
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="off")])
        self.ctl.handle("up")
        self.assertEqual(self.sent("volume")[0][1]["max"], 1.2)

    def test_spotify_capped_at_100(self):
        self.spotify._volume = 0.99
        self.spotify._peak = 0.5
        self.ctl.poll()
        self.ctl.handle("up")
        self.ctl.handle("up")
        self.assertEqual(self.spotify._volume, 1.0)

    def test_error_boost_logged_once(self):
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="error")])
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="error")])
        with self.assertLogs("knob.controller", "WARNING") as cm:
            self.drain()
        self.assertEqual(len(cm.output), 1)

    def test_host_gone_event(self):
        self.audio.all = []
        self.clock.advance(60)
        connect_host(self.bridge, 1, [boosted(7, 0.9, boost="off")])
        self.drain()
        self.bridge.on_disconnect(1)
        self.clock.advance(1)
        connect_host(self.bridge, 2, [boosted(20)])   # other host: tab 7 no longer open
        self.drain()
        self.assertEqual(self.sent("set_volume"),
                         [(2, {"type": "set_volume", "volume": 0.5, "tabs": [20]})])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)

    def test_state_before_hello_ignored(self):
        self.bridge.on_message(1, {"type": "state", "tabs": [yt(1)]})
        self.assertEqual(self.bridge.state.tabs(self.clock()), [])

    def test_wrong_protocol_ignored(self):
        self.bridge.on_message(1, {"type": "hello", "protocol": 99})
        self.bridge.on_message(1, {"type": "state", "tabs": [yt(1)]})
        self.assertEqual(self.bridge.state.tabs(self.clock()), [])
        self.bridge.command(1, {"type": "volume"})
        self.assertEqual(self.rec.sent, [])

    def test_unknown_type_ignored_and_disconnect(self):
        connect_host(self.bridge, 1, [yt(1)])
        self.bridge.on_message(1, {"type": "future-thing", "x": 1})
        self.assertEqual(len(self.bridge.state.tabs(self.clock())), 1)
        self.assertTrue(self.bridge.state.connected(self.clock()))
        self.bridge.on_disconnect(1)
        self.assertEqual(self.bridge.state.tabs(self.clock()), [])
        self.assertFalse(self.bridge.state.connected(self.clock()))

    def test_two_hosts_same_tab_id(self):
        connect_host(self.bridge, 1, [yt(5)])
        connect_host(self.bridge, 2, [yt(5)])
        keys = sorted((t.client, t.tab) for t in self.bridge.state.tabs(self.clock()))
        self.assertEqual(keys, [(1, 5), (2, 5)])


class GuardTests(unittest.TestCase):
    def test_real_audio_and_keys_blocked(self):
        with self.assertRaises(AssertionError):
            coreaudio.CoreAudio()
        with self.assertRaises(AssertionError):
            hotkeys.register()


if __name__ == "__main__":
    unittest.main()
