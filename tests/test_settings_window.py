"""Settings window: value logic, layout, drawing, saving into a temp settings
file, and the real (hidden, never shown) window driven by fake mouse and
key messages. Never touches the real settings file."""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import tempfile
import unittest

from knob import config, gdi, ui
from knob import settings_window as sw
from knob.controller import Controller

from tests.fakes import FakeAudio, FakeClock, FakeSession, ProcessList, make_bridge, settings
from tests.test_display import FakeShell

user32 = ctypes.WinDLL("user32")
user32.SendMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.IsWindowVisible.argtypes = (wt.HWND,)


def fixed_measure(text, size, weight=400):
    return len(text) * size * 0.55


class TempFolder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="knob_settings_window_")
        self._env = os.environ.get("VOLUME_KNOB_DATA")
        os.environ["VOLUME_KNOB_DATA"] = self.dir
        self.path = os.path.join(self.dir, config.SETTINGS_NAME)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("VOLUME_KNOB_DATA", None)
        else:
            os.environ["VOLUME_KNOB_DATA"] = self._env
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def file(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class ValueLogicTests(unittest.TestCase):
    def test_every_window_key_is_a_setting(self):
        for key in sw.KEYS:
            self.assertIn(key, config.DEFAULTS)
        self.assertNotIn("tray_icon", sw.KEYS)   # file only: it would hide the way back
        self.assertNotIn("apps", sw.KEYS)

    def test_offered_values_are_valid(self):
        for row in sw.ROWS:
            if row["kind"] == "stepper":
                for v in row["values"]:
                    out, warn = config.validate({row["key"]: v})
                    self.assertEqual(warn, [], (row["key"], v))
            if row["kind"] == "choice":
                for v, _ in row["options"]:
                    self.assertEqual(config.validate({row["key"]: v})[1], [], (row["key"], v))

    def test_labels(self):
        self.assertEqual(sw.pct(0.013), "1.3 %")
        self.assertEqual(sw.pct(0.5), "50 %")
        self.assertEqual(sw.secs(1.5), "1.5 s")
        self.assertEqual(sw.secs(2.0), "2 s")

    def test_custom_choice_value_is_shown_and_kept(self):
        row = sw.row_of("boost_max")
        self.assertEqual([v for v, _ in sw.options_for(row, 1.5)], [1.0, 1.25, 1.5])
        opts = sw.options_for(row, 1.4)
        self.assertEqual([v for v, _ in opts], [1.0, 1.25, 1.4, 1.5])
        self.assertEqual(opts[2][1], "140 %")

    def test_stepper_next(self):
        self.assertEqual(sw.stepper_next(sw.STEPS, 0.013, 1), 0.015)
        self.assertEqual(sw.stepper_next(sw.STEPS, 0.013, -1), 0.01)
        self.assertIsNone(sw.stepper_next(sw.STEPS, 0.01, -1))
        self.assertIsNone(sw.stepper_next(sw.STEPS, 0.05, 1))
        self.assertEqual(sw.stepper_next(sw.STEPS, 0.017, 1), 0.02)   # hand-set value
        self.assertEqual(sw.stepper_next(sw.STEPS, 0.017, -1), 0.015)

    def test_change_for(self):
        v = settings()
        self.assertEqual(sw.change_for(v, ("osd", "toggle")), ("osd", False))
        self.assertEqual(sw.change_for(v, ("step", 1)), ("step", 0.015))
        self.assertEqual(sw.change_for(v, ("osd_monitor", 1)), ("osd_monitor", "mouse"))
        self.assertIsNone(sw.change_for(v, ("osd_monitor", 0)))       # already chosen
        self.assertEqual(sw.change_for(v, ("boost_max", 0)), ("boost_max", 1.0))
        self.assertEqual(sw.change_for(v, ("step", -1)), ("step", 0.01))
        self.assertIsNone(sw.change_for(settings(step=0.01), ("step", -1)))
        self.assertIsNone(sw.change_for(v, (sw.FILE, "link")))
        self.assertIsNone(sw.change_for(v, None))

    def test_key_target(self):
        v = settings()
        self.assertEqual(sw.key_target(v, "osd", sw.VK_SPACE), ("osd", "toggle"))
        self.assertEqual(sw.key_target(v, "osd", sw.VK_LEFT), ("osd", "toggle"))  # on -> off
        self.assertIsNone(sw.key_target(v, "osd", sw.VK_RIGHT))                   # already on
        self.assertEqual(sw.key_target(v, "step", sw.VK_RIGHT), ("step", 1))
        self.assertEqual(sw.key_target(v, "tray_style", sw.VK_RIGHT), ("tray_style", 1))
        self.assertIsNone(sw.key_target(v, "tray_style", sw.VK_LEFT))
        self.assertEqual(sw.key_target(v, sw.FILE, sw.VK_RETURN), (sw.FILE, None))
        self.assertIsNone(sw.key_target(v, sw.FILE, sw.VK_LEFT))


class LayoutTests(unittest.TestCase):
    def test_order_hit_and_disabled_rows(self):
        lay = sw.layout(settings(), 1.0, measure=fixed_measure)
        self.assertEqual(lay.order, list(sw.KEYS) + [sw.FILE])
        for name, part, (x, y, w, h) in lay.targets:
            self.assertEqual(sw.hit(lay, x + w / 2, y + h / 2), (name, part))
            self.assertTrue(0 <= x and x + w <= lay.width and y + h <= lay.height)
        self.assertIsNone(sw.hit(lay, 2, 2))
        off = sw.layout(settings(osd=False, failsafe=False), 1.0, measure=fixed_measure)
        for key in ("osd_seconds", "osd_monitor", "failsafe_level"):
            self.assertNotIn(key, off.order)
            self.assertFalse(any(n == key for n, _, _ in off.targets))
        self.assertEqual(off.height, lay.height)

    def test_broken_file_only_offers_file_and_reset(self):
        lay = sw.layout(settings(), 1.0, broken=True, measure=fixed_measure)
        self.assertEqual(lay.order, [sw.FILE, sw.RESET])
        self.assertEqual({n for n, _, _ in lay.targets}, {sw.FILE, sw.RESET})
        self.assertGreater(lay.height, sw.layout(settings(), 1.0, measure=fixed_measure).height)

    def test_focus_step_wraps(self):
        lay = sw.layout(settings(), 1.0, measure=fixed_measure)
        self.assertEqual(sw.focus_step(lay, None, 1), "step")
        self.assertEqual(sw.focus_step(lay, None, -1), sw.FILE)
        self.assertEqual(sw.focus_step(lay, sw.FILE, 1), "step")
        self.assertEqual(sw.focus_step(lay, "step", -1), sw.FILE)

    def test_scale(self):
        a = sw.layout(settings(), 1.0, measure=fixed_measure)
        b = sw.layout(settings(), 1.5, measure=fixed_measure)
        self.assertEqual(b.width, round(a.width * 1.5))
        self.assertAlmostEqual(b.height, a.height * 1.5, delta=2)


def pixel(img, x, y):
    w, h, data = img
    i = (y * w + x) * 4
    return "#%02X%02X%02X" % (data[i + 2], data[i + 1], data[i])


class RenderTests(unittest.TestCase):
    def test_dark_light_scale_broken(self):
        for light, t in ((False, sw.DARK), (True, sw.LIGHT)):
            img = sw.render(settings(), 1.0, light)
            w, h, data = img
            self.assertEqual(len(data), w * h * 4)
            self.assertEqual(pixel(img, 2, 2), t["win"])
            self.assertEqual(data[3], 255)   # opaque
            lay = sw.layout(settings(), 1.0)
            _, top, rh = next(r for r in lay.rows if r[0].get("key") == "step")
            self.assertEqual(pixel(img, w // 2, int(top + 3)), t["card"])
            # the osd switch is on: accent inside the pill
            x, y, tw, th = lay.rings["osd"]
            self.assertEqual(pixel(img, int(x + 6), int(y + th / 2)), t["accent"])
        big = sw.render(settings(), 1.5)
        self.assertEqual(big[0], round(sw.WIDTH * 1.5))
        broken = sw.render(settings(), 1.0, broken=True, focus=sw.RESET, failed=True)
        self.assertGreater(broken[1], sw.render(settings())[1])
        sw.render(settings(boost_max=1.4, step=0.017), 1.0, focus="boost_max",
                  hover=("boost_max", 2))


class SaveTests(TempFolder):
    def test_missing_file_gets_defaults_plus_change(self):
        out = config.save_changes({"step": 0.02}, self.dir)
        self.assertEqual(out, {"step": 0.02})
        data = self.file()
        self.assertEqual(data["step"], 0.02)
        self.assertEqual(data["osd"], True)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_keeps_hand_edits_and_unknown_keys(self):
        self.write({"step": 0.017, "apps": ["Spotify.exe", "foo.exe"], "my_note": "hi",
                    "hold_seconds": 99})
        config.save_changes({"osd": False}, self.dir)
        data = self.file()
        self.assertEqual(data, {"step": 0.017, "apps": ["Spotify.exe", "foo.exe"],
                                "my_note": "hi", "hold_seconds": 99, "osd": False})

    def test_bad_values_refused_and_nothing_written(self):
        self.write({"step": 0.02})
        for bad in ({"step": 0.5}, {"step": "x"}, {"osd": 1}, {"osd_monitor": "left"},
                    {"nope": 1}, {"boost_max": 3.0}):
            with self.assertRaises(ValueError, msg=bad):
                config.save_changes(bad, self.dir)
        self.assertEqual(self.file(), {"step": 0.02})

    def test_broken_file_never_overwritten(self):
        for text in ('{"step": 0.02,', "[1, 2]"):
            self.write(text)
            with self.assertRaises(config.SettingsFileError):
                config.save_changes({"osd": False}, self.dir)
            with self.assertRaises(config.SettingsFileError):
                config.read(self.dir)
            with open(self.path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)

    def test_reset_keeps_the_old_file(self):
        self.write('{"step": 0.02,')
        self.assertEqual(config.reset(self.dir), config.DEFAULTS)
        self.assertEqual(self.file(), config.DEFAULTS)
        with open(os.path.join(self.dir, config.BROKEN_NAME), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"step": 0.02,')

    def test_two_resets_keep_both_backups(self):
        self.write('{"first": ')
        config.reset(self.dir)
        self.write('{"second": ')
        config.reset(self.dir)
        self.write('{"third": ')
        config.reset(self.dir)
        for name, text in ((config.BROKEN_NAME, '{"first": '),
                           ("settings.broken-2.json", '{"second": '),
                           ("settings.broken-3.json", '{"third": ')):
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
        self.assertEqual(self.file(), config.DEFAULTS)

    def test_read(self):
        self.assertEqual(config.read(self.dir), config.DEFAULTS)   # no file yet
        self.write({"osd_seconds": 3, "step": 9})
        v = config.read(self.dir)
        self.assertEqual(v["osd_seconds"], 3.0)
        self.assertEqual(v["step"], 0.20)   # clamped like at start


def click(win, name, part):
    x, y, w, h = next(r for n, p, r in win.lay.targets if (n, p) == (name, part))
    cx, cy = int(x + w / 2), int(y + h / 2)
    return user32.SendMessageW(win.hwnd, sw.WM_LBUTTONUP, 0, (cy << 16) | cx)


def key(win, vk):
    return user32.SendMessageW(win.hwnd, sw.WM_KEYDOWN, vk, 0)


class WindowTests(TempFolder):
    """The real settings window through ui.Ui: created hidden, never shown."""

    def setUp(self):
        super().setUp()
        self.shell = FakeShell()
        self.changes, self.opened = [], []
        self.ui = ui.Ui(settings(), on_quit=lambda: None, notify=self.shell,
                        settings_path=self.path, opener=self.opened.append,
                        dpi_aware=False, on_settings=self.changes.append)
        self.ui.create()
        self.shown = []
        sw.SettingsWindow._show = lambda s: self.shown.append(s.hwnd)

    def tearDown(self):
        import tests
        sw.SettingsWindow._show = tests._blocked
        self.ui.close()
        super().tearDown()

    def open(self):
        self.ui.command(ui.CMD_SETTINGS)
        return self.ui.settings_win

    def test_opens_hidden_from_menu_command(self):
        win = self.open()
        self.assertEqual(self.shown, [win.hwnd])
        self.assertFalse(user32.IsWindowVisible(win.hwnd))
        self.assertTrue(win.visible)
        self.assertEqual(win.values, config.DEFAULTS)
        self.assertIsNotNone(win.image)
        self.assertEqual(user32.SendMessageW(win.hwnd, sw.WM_PAINT, 0, 0), 0)
        self.open()  # again: same window, brought to front
        self.assertEqual(len(self.shown), 2)

    def test_click_saves_and_applies_live(self):
        win = self.open()
        click(win, "step", 1)
        self.assertEqual(self.file()["step"], 0.015)
        self.assertEqual(self.changes, [{"step": 0.015}])
        click(win, "osd_seconds", 1)
        self.assertEqual(self.ui.osd_ms, 2000)
        click(win, "osd_monitor", 1)
        self.assertEqual(self.ui.osd.monitor, "mouse")
        n = len(self.shell.calls)
        click(win, "tray_style", 1)
        self.assertEqual(self.ui.tray_style, "knob")
        self.assertEqual(len(self.shell.calls), n + 1)   # icon redrawn in the new style
        click(win, "osd", "toggle")
        self.assertIsNone(self.ui.osd)
        self.assertNotIn("osd_seconds", win.lay.order)    # greyed out now
        click(win, "osd", "toggle")
        self.assertIsNotNone(self.ui.osd)
        data = self.file()
        self.assertEqual((data["osd_seconds"], data["osd_monitor"], data["tray_style"],
                          data["osd"]), (2.0, "mouse", "knob", True))

    def test_keyboard(self):
        win = self.open()
        for _ in range(3):
            key(win, sw.VK_TAB)
        self.assertEqual(win.focus, "brave_fallback")
        self.assertTrue(win.focus_visible)
        key(win, sw.VK_SPACE)
        self.assertEqual(self.file()["brave_fallback"], False)
        key(win, sw.VK_UP)
        key(win, sw.VK_RIGHT)   # spotify_control -> mixer
        self.assertEqual(self.file()["spotify_control"], "mixer")
        key(win, sw.VK_ESCAPE)
        self.assertFalse(win.visible)

    def test_footer_link_and_close(self):
        win = self.open()
        click(win, sw.FILE, "link")
        self.assertEqual(self.opened, [self.path])
        user32.SendMessageW(win.hwnd, sw.WM_CLOSE, 0, 0)
        self.assertFalse(win.visible)
        self.assertIsNotNone(self.ui.settings_win)   # only hidden

    def test_mouse_hover(self):
        win = self.open()
        x, y, w, h = win.lay.rings["osd"]
        user32.SendMessageW(win.hwnd, sw.WM_MOUSEMOVE, 0, (int(y + h / 2) << 16) | int(x + w / 2))
        self.assertEqual(win.hover, ("osd", "toggle"))
        user32.SendMessageW(win.hwnd, sw.WM_MOUSELEAVE, 0, 0)
        self.assertIsNone(win.hover)

    def test_broken_file(self):
        self.write('{"step": 0.02,')
        win = self.open()
        self.assertIsNotNone(win.broken)
        self.assertEqual(win.lay.order, [sw.FILE, sw.RESET])
        win.activate(("step", 1))      # disabled: nothing written
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"step": 0.02,')
        self.assertEqual(self.changes, [])
        click(win, sw.RESET, None)
        self.assertIsNone(win.broken)
        self.assertEqual(self.file(), config.DEFAULTS)
        self.assertTrue(os.path.exists(os.path.join(self.dir, config.BROKEN_NAME)))
        self.assertEqual(set(self.changes[0]), set(sw.KEYS))

    def test_file_broken_while_open(self):
        win = self.open()
        self.write("not json")
        click(win, "step", 1)
        self.assertIsNotNone(win.broken)
        self.assertEqual(self.changes, [])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "not json")

    def test_hand_edit_shows_on_next_open(self):
        win = self.open()
        win.close()
        self.write({"step": 0.03, "osd": False})
        self.open()
        self.assertEqual((win.values["step"], win.values["osd"]), (0.03, False))

    def test_dpi_change(self):
        win = self.open()
        r = wt.RECT(10, 10, 900, 1200)
        user32.SendMessageW(win.hwnd, sw.WM_DPICHANGED, 144 | (144 << 16),
                            ctypes.addressof(r))
        self.assertEqual(win.scale, 1.5)
        self.assertEqual(win.lay.width, round(sw.WIDTH * 1.5))
        self.assertEqual(win.image[0], round(sw.WIDTH * 1.5))
        self.assertFalse(user32.IsWindowVisible(win.hwnd))


class ControllerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge, _ = make_bridge(self.clock)
        self.audio = FakeAudio([FakeSession("spotify.exe", volume=0.5, peak=0.3)])
        self.ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                              process_lister=ProcessList())
        self.ctl.audio = self.audio

    def test_values_used_live(self):
        self.ctl.on_event(("settings", {"step": 0.03, "failsafe": False,
                                        "failsafe_level": 0.3, "boost_max": 1.25}))
        self.assertEqual(self.ctl.settings["step"], 0.03)
        self.assertFalse(self.ctl.failsafe.enabled)
        self.assertEqual(self.ctl.failsafe.level, 0.3)
        self.assertEqual(self.ctl.settings["boost_max"], 1.25)
        self.ctl.poll()
        d = self.ctl.handle("up")
        self.assertAlmostEqual(d.delta, 0.03)

    def test_spotify_switch(self):
        from tests.test_spotify import FakeSpotifyVolume
        made = []

        class Web(FakeSpotifyVolume):
            def start(self):
                self.started = True

            def close(self):
                self.closed = True
        self.ctl.spotify_factory = lambda: made.append(Web()) or made[-1]
        self.ctl.on_event(("settings", {"spotify_control": "web"}))
        self.assertIs(self.ctl.spotify, made[0])
        self.assertTrue(made[0].started)
        self.assertIsNotNone(made[0].on_change)
        self.ctl.on_event(("settings", {"spotify_control": "mixer"}))
        self.assertIsNone(self.ctl.spotify)
        import time
        for _ in range(100):
            if getattr(made[0], "closed", False):
                break
            time.sleep(0.01)
        self.assertTrue(made[0].closed)
        self.ctl.spotify_factory = None   # no factory: applies after a restart
        self.ctl.on_event(("settings", {"spotify_control": "web"}))
        self.assertIsNone(self.ctl.spotify)


if __name__ == "__main__":
    unittest.main()
