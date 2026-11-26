"""Settings, playing detection, the routing rule and the protocol helpers."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from knob import config, protocol, router
from knob.activity import ActivityTracker
from knob.browser import Tab


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="knob_core_test_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(os.path.join(self.dir, config.SETTINGS_NAME), "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_writes_defaults(self):
        s, w = config.load(self.dir)
        self.assertEqual(s, config.DEFAULTS)
        self.assertEqual(w, [])
        with open(os.path.join(self.dir, config.SETTINGS_NAME), encoding="utf-8") as f:
            self.assertEqual(json.load(f), config.DEFAULTS)

    def test_bad_json_gives_defaults(self):
        self.write("{not json")
        s, w = config.load(self.dir)
        self.assertEqual(s, config.DEFAULTS)
        self.assertTrue(w)

    def test_bom_is_accepted(self):
        with open(os.path.join(self.dir, config.SETTINGS_NAME), "w", encoding="utf-8-sig") as f:
            f.write('{"step": 0.05}')
        s, w = config.load(self.dir)
        self.assertEqual(s["step"], 0.05)
        self.assertEqual(w, [])

    def test_values_checked(self):
        s, w = config.validate({"step": 5, "apps": ["Spotify.EXE ", "vlc.exe"],
                                "hold_seconds": "x", "brave_fallback": 1,
                                "log_level": "debug", "peak_threshold": float("nan"),
                                "unknown": 1})
        self.assertEqual(s["step"], 0.20)
        self.assertEqual(s["apps"], ["spotify.exe", "vlc.exe"])
        self.assertEqual(s["hold_seconds"], 1.5)
        self.assertTrue(s["brave_fallback"])
        self.assertEqual(s["log_level"], "DEBUG")
        self.assertEqual(s["peak_threshold"], 0.001)
        self.assertEqual(len(w), 4)

    def test_not_an_object(self):
        s, w = config.validate([1, 2])
        self.assertEqual(s, config.DEFAULTS)
        self.assertTrue(w)

    def test_bad_apps(self):
        s, w = config.validate({"apps": "spotify.exe"})
        self.assertEqual(s["apps"], ["spotify.exe"])
        self.assertTrue(w)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"VOLUME_KNOB_DATA": self.dir}):
            self.assertEqual(config.data_dir(), self.dir)
            self.assertEqual(config.log_dir(), self.dir)


class ActivityTests(unittest.TestCase):
    def test_hold(self):
        a = ActivityTracker(threshold=0.001, hold=1.5)
        self.assertFalse(a.is_playing("spotify.exe", 0))
        a.update("spotify.exe", 0.0005, 0)          # below threshold
        self.assertFalse(a.is_playing("spotify.exe", 0))
        a.update("spotify.exe", 0.2, 10)
        self.assertTrue(a.is_playing("spotify.exe", 10))
        a.update("spotify.exe", 0.0, 10.2)           # quiet moment
        self.assertTrue(a.is_playing("spotify.exe", 11.5))
        self.assertFalse(a.is_playing("spotify.exe", 11.6))

    def test_forget(self):
        a = ActivityTracker()
        a.update("x.exe", 1, 0)
        a.forget_except(set())
        self.assertFalse(a.is_playing("x.exe", 0))


def tab(client, tid, site="youtube", playing=False, muted=False):
    return Tab(client, tid, site, playing, 0.5, muted)


class RouterTests(unittest.TestCase):
    def test_only_playing_app(self):
        d = router.decide("up", [router.AppSource("spotify.exe", True, False)],
                          [tab(1, 5, playing=False)], 0.04)
        self.assertEqual(d.apps, ["spotify.exe"])
        self.assertEqual(d.tabs, {})
        self.assertAlmostEqual(d.delta, 0.04)

    def test_only_playing_tab(self):
        d = router.decide("down", [router.AppSource("spotify.exe", False, False)],
                          [tab(1, 5, playing=True), tab(1, 6), tab(2, 5, "ytmusic", True)], 0.04)
        self.assertEqual(d.apps, [])
        self.assertEqual(d.tabs, {1: [5], 2: [5]})
        self.assertAlmostEqual(d.delta, -0.04)

    def test_nothing_playing_changes_all(self):
        d = router.decide("up", [router.AppSource("spotify.exe", False, False)],
                          [tab(1, 5), tab(1, 6, "ytmusic")], 0.04)
        self.assertEqual(d.apps, ["spotify.exe"])
        self.assertEqual(d.tabs, {1: [5, 6]})

    def test_both_playing(self):
        d = router.decide("up", [router.AppSource("spotify.exe", True, False)],
                          [tab(1, 5, playing=True)], 0.04)
        self.assertEqual(d.apps, ["spotify.exe"])
        self.assertEqual(d.tabs, {1: [5]})

    def test_mute_toggle(self):
        d = router.decide("mute", [router.AppSource("spotify.exe", False, True)],
                          [tab(1, 5, muted=False)], 0.04)
        self.assertIs(d.muted, True)        # one unmuted -> mute all
        self.assertIsNone(d.delta)
        d = router.decide("mute", [router.AppSource("spotify.exe", False, True)],
                          [tab(1, 5, muted=True)], 0.04)
        self.assertIs(d.muted, False)       # all muted -> unmute all

    def test_nothing_at_all(self):
        d = router.decide("up", [], [], 0.04)
        self.assertTrue(d.empty())


class ProtocolTests(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(protocol.decode(b'{"type":"x","a":1}')["type"], "x")
        for bad in (b"[1]", b"{}", b'{"type":1}', b"nope", b"\xff\xfe",
                    b'{"type":"x","p":"' + b"a" * protocol.MAX_LINE + b'"}'):
            with self.assertRaises(ValueError):
                protocol.decode(bad)

    def test_encode_one_line(self):
        data = protocol.encode({"type": "volume", "delta": 0.04})
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)

    def test_parse_state(self):
        tabs = protocol.parse_state({"type": "state", "tabs": [
            {"tab": 1, "site": "youtube", "playing": True, "volume": 1.7, "muted": False,
             "extra": "ignored"},
            {"tab": "2", "site": "youtube"},
            {"tab": 3, "site": "netflix"},
            {"tab": True, "site": "youtube"},
            {"tab": 4, "site": "ytmusic", "playing": "yes", "volume": "x"},
            "junk"]})
        self.assertEqual([t["tab"] for t in tabs], [1, 4])
        self.assertEqual(tabs[0]["volume"], 1.5)          # 1.7 clamped to the hard max
        self.assertFalse(tabs[1]["playing"])
        self.assertIsNone(tabs[1]["volume"])
        self.assertEqual((tabs[1]["max_volume"], tabs[1]["boost"]), (1.0, "off"))
        self.assertIsNone(protocol.parse_state({"type": "state", "tabs": "x"}))

    def test_parse_state_boost_fields(self):
        tabs = protocol.parse_state({"type": "state", "tabs": [
            {"tab": 1, "site": "youtube", "volume": 1.25, "max_volume": 1.5, "boost": "on"},
            {"tab": 2, "site": "youtube", "volume": None, "max_volume": 9, "boost": "weird"},
            {"tab": 3, "site": "ytmusic", "boost": "unavailable", "max_volume": 1.0}]})
        self.assertEqual((tabs[0]["volume"], tabs[0]["max_volume"], tabs[0]["boost"]),
                         (1.25, 1.5, "on"))
        self.assertEqual((tabs[1]["volume"], tabs[1]["max_volume"], tabs[1]["boost"]),
                         (None, 1.5, "off"))
        self.assertEqual(tabs[2]["boost"], "unavailable")

    def test_builders(self):
        self.assertEqual(protocol.volume(0.04, tabs=[1, 2], max_volume=1.5),
                         {"type": "volume", "delta": 0.04, "max": 1.5, "tabs": [1, 2]})
        self.assertEqual(protocol.volume(-0.04, site="youtube"),
                         {"type": "volume", "delta": -0.04, "max": 1.0, "site": "youtube"})
        self.assertEqual(protocol.set_volume(0.5, tabs=[4]),
                         {"type": "set_volume", "volume": 0.5, "tabs": [4]})
        self.assertEqual(protocol.set_volume(3)["volume"], 1.5)
        self.assertEqual(protocol.mute(True, tabs=[3]),
                         {"type": "mute", "muted": True, "tabs": [3]})
        self.assertEqual(protocol.mute(False), {"type": "mute", "muted": False, "site": "all"})
        h = protocol.hello()
        self.assertEqual((h["type"], h["protocol"], h["features"]),
                         ("hello", 2, ["boost", "set_volume"]))

    def test_hello_check(self):
        self.assertTrue(protocol.parse_hello({"protocol": 2}))
        for p in (1, 3, "2", True, None):
            self.assertFalse(protocol.parse_hello({"protocol": p}))
        self.assertEqual(protocol.parse_features({"features": ["boost", 3, "x"]}),
                         {"boost", "x"})
        self.assertEqual(protocol.parse_features({"features": "boost"}), frozenset())

    def test_pipe_name_env(self):
        with mock.patch.dict(os.environ, {"VOLUME_KNOB_PIPE": r"\\.\pipe\x"}):
            self.assertEqual(protocol.pipe_name(), r"\\.\pipe\x")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOLUME_KNOB_PIPE", None)
            self.assertEqual(protocol.pipe_name(), r"\\.\pipe\8bitdo-volume-knob")


if __name__ == "__main__":
    unittest.main()
