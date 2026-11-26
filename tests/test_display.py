"""Volume pop-up and tray icon: logic, controller wiring, drawing in memory, and
the real windows created hidden (never shown; the tray is faked)."""
import ctypes
import ctypes.wintypes as wt
import math
import unittest

from knob import config, display, hotkeys, osd, ui
from knob.controller import Controller
from knob.display import Row

from tests.fakes import (FakeAudio, FakeClock, FakeSession, ProcessList, connect_host,
                         make_bridge, settings)

STEP = settings()["step"]


def tab(tid, playing=False, volume=0.5, muted=False, site="youtube", boost="off"):
    return {"tab": tid, "site": site, "playing": playing, "volume": volume, "muted": muted,
            "max_volume": 1.5, "boost": boost}


class LogicTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(display.name_for("spotify.exe"), "Spotify")
        self.assertEqual(display.name_for("ytmusic"), "YouTube Music")
        self.assertEqual(display.name_for("youtube"), "YouTube")
        self.assertEqual(display.name_for("brave.exe"), "Brave")
        self.assertEqual(display.name_for("foobar.exe"), "Foobar")

    def test_percent(self):
        self.assertEqual(display.percent(0.45), 45)
        self.assertEqual(display.percent(1.015), 102)
        self.assertEqual(display.percent(0.0), 0)
        self.assertEqual(display.percent(1.5), 150)

    def test_predict_tab_like_the_extension(self):
        p = display.predict_tab
        self.assertAlmostEqual(p(0.5, 0.015, 1.5), 0.515)
        self.assertAlmostEqual(p(0.99, 0.015, 1.5), 1.0)     # stops on 100 % once
        self.assertAlmostEqual(p(1.0, 0.015, 1.5), 1.015)
        self.assertAlmostEqual(p(1.01, -0.015, 1.5), 1.0)
        self.assertAlmostEqual(p(1.49, 0.015, 1.5), 1.5)     # clamped to max
        self.assertAlmostEqual(p(0.995, 0.015, 1.0), 1.0)
        self.assertAlmostEqual(p(0.005, -0.015, 1.5), 0.0)
        self.assertAlmostEqual(p(0.5, 0.001, 1.5), 0.51)     # at least 1 %

    def test_angle(self):
        a = display.angle_for
        self.assertAlmostEqual(a(0.0), -150)   # 7 o'clock
        self.assertAlmostEqual(a(0.5), 0)      # 12 o'clock
        self.assertAlmostEqual(a(1.0), 150)    # 5 o'clock
        self.assertAlmostEqual(a(1.5), 180)    # 6 o'clock (boost)
        self.assertAlmostEqual(a(3.0), 180)
        self.assertAlmostEqual(a(-1), -150)

    def test_tooltip(self):
        t = display.tooltip
        self.assertEqual(t([]), "8BitDo Volume Knob - running")
        self.assertEqual(t([Row("Spotify", 0.45, False, 1.0)]), "8BitDo Volume Knob - Spotify 45%")
        self.assertEqual(t([Row("Spotify", 0.4, True, 1.0), Row("YouTube", 0.7, False, 1.5)]),
                         "8BitDo Volume Knob - Spotify muted, YouTube 70%")
        self.assertIn("paused", t([Row("Spotify", 0.4, False, 1.0)], paused=True))
        self.assertIn("in use by another program", t([], keys_ok=False))
        long = [Row("X" * 60, 0.5, False, 1.0)] * 4
        self.assertLessEqual(len(t(long)), display.TIP_MAX)

    def test_icon_key(self):
        self.assertEqual(display.icon_key([]), (None, False, False))
        self.assertEqual(display.icon_key([Row("S", 0.45, False, 1)], paused=True),
                         (None, False, True))
        self.assertEqual(display.icon_key([Row("S", 1.35, False, 1.5), Row("Y", 0.2, 0, 1)]),
                         (135, False, False))
        self.assertEqual(display.icon_key([Row("S", 0.3, True, 1.0)]), (30, True, False))

    def test_bar_parts(self):
        self.assertEqual(display.bar_parts(Row("S", 0.5, False, 1.0)), (0.5, 0.0, None))
        n, b, tick = display.bar_parts(Row("Y", 1.2, False, 1.5))
        self.assertAlmostEqual(n, 1 / 1.5)
        self.assertAlmostEqual(b, 0.2 / 1.5)
        self.assertAlmostEqual(tick, 1 / 1.5)

    def test_settings(self):
        self.assertTrue(config.DEFAULTS["osd"])
        self.assertTrue(config.DEFAULTS["tray_icon"])
        s, w = config.validate({"osd": False, "tray_icon": "no", "osd_seconds": 99})
        self.assertFalse(s["osd"])
        self.assertTrue(s["tray_icon"])
        self.assertEqual(s["osd_seconds"], 10.0)
        self.assertEqual(len(w), 2)


class ControllerDisplayTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)
        self.spotify = FakeSession("spotify.exe", volume=0.5)
        self.audio = FakeAudio([self.spotify])
        self.shown = []
        self.ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                              process_lister=ProcessList(),
                              display=lambda rows, popup: self.shown.append((rows, popup)))
        self.ctl.audio = self.audio

    def test_spotify_turn(self):
        self.spotify._peak = 0.3
        self.ctl.poll()
        self.ctl.handle("up")
        rows, popup = self.shown[-1]
        self.assertTrue(popup)
        self.assertEqual(rows, [Row("Spotify", 0.5 + STEP, False, 1.0)])

    def test_before_first_turn_shows_spotify(self):
        self.ctl.poll()
        self.assertEqual(self.shown, [([Row("Spotify", 0.5, False, 1.0)], False)])
        self.ctl.poll()  # nothing changed, within the refresh interval
        self.clock.advance(2.5)
        self.ctl.poll()  # refreshed, still nothing changed
        self.assertEqual(len(self.shown), 1)

    def test_volume_changed_elsewhere_follows(self):
        self.ctl.poll()
        self.spotify._volume = 0.8
        self.clock.advance(2.5)
        self.ctl.poll()
        self.assertEqual(self.shown[-1], ([Row("Spotify", 0.8, False, 1.0)], False))

    def test_tab_prediction_over_fast_turns(self):
        connect_host(self.bridge, 1, [tab(7, playing=True, volume=0.99)])
        self.ctl.poll()
        for _ in range(3):
            self.ctl.handle("up")
        rows, popup = self.shown[-1]
        self.assertTrue(popup)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "YouTube")
        self.assertAlmostEqual(rows[0].level, 1.0 + 2 * STEP)  # 99 -> 100 (stop) -> 2 steps
        self.assertEqual(rows[0].max_level, 1.5)
        # the reported state wins once the guess is old
        self.clock.advance(1.5)
        connect_host(self.bridge, 1, [tab(7, playing=True, volume=0.6)])
        self.ctl.on_event(("tabs", 1, []))
        self.assertAlmostEqual(self.shown[-1][0][0].level, 0.6)
        self.assertFalse(self.shown[-1][1])

    def test_tab_mute_and_no_boost(self):
        connect_host(self.bridge, 1, [tab(7, playing=True, volume=1.0, boost="unavailable")])
        self.ctl.poll()
        self.ctl.handle("up")
        self.assertEqual(self.shown[-1][0], [Row("YouTube", 1.0, False, 1.0)])
        self.ctl.handle("mute")
        self.assertEqual(self.shown[-1][0], [Row("YouTube", 1.0, True, 1.0)])

    def test_nothing_playing_both_one_row_per_site(self):
        connect_host(self.bridge, 1, [tab(7), tab(8), tab(9, site="ytmusic")])
        self.ctl.poll()
        self.ctl.handle("down")
        names = [r.name for r in self.shown[-1][0]]
        self.assertEqual(names, ["Spotify", "YouTube", "YouTube Music"])

    def test_spotify_mute(self):
        self.spotify._peak = 0.3
        self.ctl.poll()
        self.ctl.handle("mute")
        self.assertEqual(self.shown[-1][0], [Row("Spotify", 0.5, True, 1.0)])

    def test_gone_source_falls_back(self):
        connect_host(self.bridge, 1, [tab(7, playing=True)])
        self.ctl.poll()
        self.ctl.handle("up")
        self.bridge.on_disconnect(1)
        self.ctl.on_event(("gone", 1))
        self.assertEqual(self.shown[-1][0], [Row("Spotify", 0.5, False, 1.0)])

    def test_display_error_does_not_stop_the_knob(self):
        def boom(rows, popup):
            raise RuntimeError("x")
        self.ctl.display = boom
        self.spotify._peak = 0.3
        self.ctl.poll()
        self.ctl.handle("up")
        self.assertAlmostEqual(self.spotify._volume, 0.5 + STEP)


def pixel(data, size, x, y):
    i = (y * size + x) * 4
    return tuple(data[i:i + 4])  # b, g, r, a


class IconTests(unittest.TestCase):
    def at_angle(self, size, deg, radius=8.0):
        a = math.radians(deg)
        u = size / 32.0
        return int((16 + math.sin(a) * radius) * u), int((16 - math.cos(a) * radius) * u)

    def test_shape_and_colours(self):
        for size in (16, 20, 24, 32):
            px = ui.icon_pixels(size, 50)
            self.assertEqual(len(px), size * size * 4)
            self.assertEqual(pixel(px, size, 0, 0)[3], 0)              # transparent corner
            self.assertEqual(pixel(px, size, size // 2, size // 2)[3], 255)
        px = ui.icon_pixels(32, 50)
        b, g, r, a = pixel(px, 32, 16, 1)                               # ring at 12 o'clock
        self.assertTrue(r > 150 and g < 80 and b < 80)                  # red
        boost = ui.icon_pixels(32, 135)
        b, g, r, a = pixel(boost, 32, 1, 16)
        self.assertTrue(r > 200 and 100 < g < 200 and b < 80)           # orange
        for key in ((45, True, False), (None, False, True)):
            b, g, r, a = pixel(ui.icon_pixels(32, *key), 32, 1, 16)
            self.assertTrue(abs(r - g) < 10 and abs(g - b) < 10)        # grey

    def test_pointer_follows_volume(self):
        for pct, deg in ((0, -150), (50, 0), (100, 150), (150, 180)):
            px = ui.icon_pixels(32, pct)
            x, y = self.at_angle(32, deg)
            b, g, r, a = pixel(px, 32, x, y)
            if pct > 100:  # boost: orange pointer
                self.assertTrue(r > 200 and b < 80, f"{pct}%: pointer not at {deg} deg")
            else:
                self.assertTrue(min(b, g, r) > 200, f"{pct}%: pointer not at {deg} deg")
            ox, oy = self.at_angle(32, deg + 90)
            self.assertLess(min(pixel(px, 32, ox, oy)[:3]), 100)       # body elsewhere

    def test_cache_limit(self):
        made, destroyed = [], []
        cache = ui.IconCache(limit=3, maker=lambda size, key: made.append(key) or len(made))
        cache.destroy = destroyed.append
        for p in (1, 2, 3, 1, 4):
            cache.get(24, (p, False, False))
        self.assertEqual(len(made), 4)
        self.assertEqual(destroyed, [2])   # least recently used
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_real_hicon(self):
        icon = ui.make_hicon(24, ui.icon_pixels(24, 70))
        self.assertTrue(icon)
        self.assertTrue(ui.user32.DestroyIcon(icon))


class FakeShell:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def __call__(self, message, data):
        self.calls.append((message, data.szTip, data.hIcon))
        return message not in self.fail


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.shell = FakeShell()
        cache = ui.IconCache(maker=lambda size, key: hash(key) & 0xFFFF)
        cache.destroy = lambda h: None
        self.tray = ui.Tray(1234, notify=self.shell, icons=cache, size=24)

    def kinds(self):
        return [c[0] for c in self.shell.calls]

    def test_add_update_remove(self):
        self.assertTrue(self.tray.add((50, False, False), "tip a"))
        self.assertEqual(self.kinds(), [ui.NIM_ADD, ui.NIM_SETVERSION])
        self.assertFalse(self.tray.update((50, False, False), "tip a"))   # unchanged
        self.assertTrue(self.tray.update((51, False, False), "tip b"))
        self.assertEqual(self.kinds()[-1], ui.NIM_MODIFY)
        self.assertEqual(self.shell.calls[-1][1], "tip b")
        self.tray.remove()
        self.assertEqual(self.kinds()[-1], ui.NIM_DELETE)

    def test_modify_fails_then_readd(self):
        self.tray.add((50, False, False), "a")
        self.shell.fail.add(ui.NIM_MODIFY)
        self.tray.update((60, False, False), "b")
        self.assertEqual(self.kinds()[-2:], [ui.NIM_ADD, ui.NIM_SETVERSION])

    def test_taskbar_not_ready_then_created(self):
        self.shell.fail.add(ui.NIM_ADD)
        self.assertFalse(self.tray.add((None, False, False), "a"))
        self.shell.fail.clear()
        self.tray.readd()
        self.assertTrue(self.tray.added)


class HotkeyPauseTests(unittest.TestCase):
    def setUp(self):
        self.reg, self.unreg, self.status = [], [], []
        self._r, self._u = hotkeys.register, hotkeys.unregister
        hotkeys.register = lambda hwnd=None: self.reg.append(hwnd) or (True, 0)
        hotkeys.unregister = lambda hwnd=None: self.unreg.append(hwnd)
        st = hotkeys._LoopState(77, lambda r, p: self.status.append((r, p)))
        st.registered = True
        hotkeys._current = st

    def tearDown(self):
        hotkeys.register, hotkeys.unregister = self._r, self._u
        hotkeys._current = None

    def test_pause_resume(self):
        self.assertTrue(hotkeys.pause())
        self.assertTrue(hotkeys.is_paused())
        self.assertEqual(self.unreg, [77])
        self.assertEqual(self.status[-1], (False, True))
        self.assertFalse(hotkeys.pause())
        self.assertTrue(hotkeys.resume())
        self.assertEqual(self.reg, [77])
        self.assertEqual(self.status[-1], (True, False))


class OsdDrawTests(unittest.TestCase):
    def test_sizes_and_placement(self):
        self.assertEqual(osd.card_size(1, 1.0), (300, 76))
        self.assertEqual(osd.card_size(2, 1.5), (450, 201))
        x, y = osd.placement((0, 0, 1920, 1032), 300, 76, 1.0)
        self.assertEqual((x, y), (810, 1032 - 76 - 48))
        x, y = osd.placement((1920, 0, 4480, 1400), 450, 114, 1.5)
        self.assertEqual(x, 1920 + (2560 - 450) // 2)

    def test_render(self):
        for light in (False, True):
            w, h, data = osd.render([Row("YouTube Music", 1.35, False, 1.5)], 1.0, light)
            self.assertEqual(len(data), w * h * 4)
            self.assertEqual(data[3], 0)                         # rounded corner: transparent
            mid = ((h // 2) * w + w // 2) * 4
            self.assertEqual(data[mid + 3], 255)                 # opaque body
            # boost part of the bar (orange-ish) at 85 % of the bar width
            bx = int(16 + (w - 32) * 0.8)
            by = 12 + 40 + 2
            b, g, r, a = data[(by * w + bx) * 4:(by * w + bx) * 4 + 4]
            self.assertTrue(r > 150 and b < 100, (r, g, b))

    def fake_monitors(self, cursor):
        """Main screen 0..1920 at 100 %, second screen 1920..4480 at 150 %."""
        mons = {1: ((0, 0, 1920, 1032), 1.0), 2: ((1920, 0, 4480, 1400), 1.5)}
        saved = osd._cursor, osd._monitor_at, osd._monitor_area
        osd._cursor = lambda: cursor
        osd._monitor_at = lambda x, y: 2 if x >= 1920 else 1
        osd._monitor_area = lambda m: mons[m]
        self.addCleanup(lambda: setattr(osd, "_cursor", saved[0]) or
                        setattr(osd, "_monitor_at", saved[1]) or
                        setattr(osd, "_monitor_area", saved[2]))

    def test_monitor_choice(self):
        self.fake_monitors((3000, 500))  # mouse on the second screen
        self.assertEqual(osd.target_monitor("primary"), ((0, 0, 1920, 1032), 1.0))
        self.assertEqual(osd.target_monitor(), ((0, 0, 1920, 1032), 1.0))
        self.assertEqual(osd.target_monitor("mouse"), ((1920, 0, 4480, 1400), 1.5))
        osd._cursor = lambda: None       # no mouse position: main screen
        self.assertEqual(osd.target_monitor("mouse"), ((0, 0, 1920, 1032), 1.0))

    def test_show_uses_setting(self):
        self.fake_monitors((3000, 500))
        old = osd.fullscreen_app
        osd.fullscreen_app = lambda: False
        self.addCleanup(lambda: setattr(osd, "fullscreen_app", old))
        for mode, inside in (("primary", (0, 1920)), ("mouse", (1920, 4480))):
            o = osd.Osd(0, mode)
            got = []
            o._present = lambda x, y, w, h, data: got.append((x, w))
            self.assertTrue(o.show([Row("Spotify", 0.5, False, 1.0)]))
            x, w = got[0]
            self.assertTrue(inside[0] <= x and x + w <= inside[1], (mode, x, w))

    def test_monitor_setting(self):
        self.assertEqual(config.DEFAULTS["osd_monitor"], "primary")
        s, w = config.validate({"osd_monitor": "Mouse"})
        self.assertEqual((s["osd_monitor"], w), ("mouse", []))
        s, w = config.validate({"osd_monitor": "left"})
        self.assertEqual(s["osd_monitor"], "primary")
        self.assertEqual(len(w), 1)
        s, w = config.validate({"osd_monitor": 3})
        self.assertEqual((s["osd_monitor"], len(w)), ("primary", 1))

    def test_fullscreen_skips(self):
        o = osd.Osd(0)
        old = osd.fullscreen_app
        osd.fullscreen_app = lambda: True
        try:
            self.assertFalse(o.show([Row("Spotify", 0.5, False, 1.0)]))  # _present never called
        finally:
            osd.fullscreen_app = old
        self.assertFalse(o.show([]))


user32 = ctypes.WinDLL("user32")
user32.IsWindowVisible.argtypes = (wt.HWND,)
user32.GetWindowLongPtrW.argtypes = (wt.HWND, ctypes.c_int)
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.SendMessageW.restype = ctypes.c_ssize_t


class UiWindowTests(unittest.TestCase):
    """Real windows, created hidden and never shown; tray and pop-up faked."""

    def setUp(self):
        self.shell = FakeShell()
        self.events = []
        self.opened = []
        self.ui = ui.Ui(settings(), on_quit=lambda: self.events.append("quit"),
                        on_pause=lambda on: self.events.append(("pause", on)),
                        on_key=lambda k: self.events.append(("key", k)),
                        log_path="L", settings_path="S", notify=self.shell,
                        opener=self.opened.append, dpi_aware=False)
        self.ui.create()
        self.presented = []
        self.ui.osd._present = lambda *a: self.presented.append(a)

    def tearDown(self):
        self.ui.close()

    def test_windows_hidden_and_styles(self):
        self.assertFalse(user32.IsWindowVisible(self.ui.hwnd))
        self.assertFalse(user32.IsWindowVisible(self.ui.osd.hwnd))
        ex = user32.GetWindowLongPtrW(self.ui.osd.hwnd, -20)  # GWL_EXSTYLE
        self.assertEqual(ex & osd.EX_STYLE, osd.EX_STYLE)
        self.assertEqual([c[0] for c in self.shell.calls], [ui.NIM_ADD, ui.NIM_SETVERSION])
        self.assertEqual(self.shell.calls[0][1], "8BitDo Volume Knob - running")

    def test_show_pending(self):
        rows = [Row("Spotify", 0.45, False, 1.0)]
        self.ui.show(rows, popup=True)
        self.ui.show(rows, popup=False)    # merged: still a pop-up
        self.ui.apply_pending()
        self.assertEqual(len(self.presented), 1)
        self.assertTrue(self.ui.osd.visible)
        self.assertEqual(self.shell.calls[-1][1], "8BitDo Volume Knob - Spotify 45%")
        user32.SendMessageW(self.ui.hwnd, ui.WM_TIMER, ui.OSD_TIMER, 0)   # hide timer
        self.assertFalse(self.ui.osd.visible)
        self.ui.show([Row("Spotify", 0.5, False, 1.0)], popup=False)
        self.ui.apply_pending()
        self.assertEqual(len(self.presented), 1)   # hidden: no pop-up for outside changes

    def test_menu_commands_and_status(self):
        self.ui.command(ui.CMD_PAUSE)
        self.ui.command(ui.CMD_LOG)
        self.ui.command(ui.CMD_SETTINGS)
        self.ui.command(ui.CMD_QUIT)
        self.assertEqual(self.events, [("pause", True), "quit"])
        self.assertEqual(self.opened, ["L", "S"])
        self.ui.keys_status(False, True)
        self.assertIn("paused", self.shell.calls[-1][1])
        items = ui.menu_items(True)
        self.assertEqual([t for c, t, e in items if c == ui.CMD_PAUSE], ["Resume"])
        self.assertEqual([t for c, t, e in ui.menu_items(False)][2:5],
                         ["Pause", "Open log", "Open settings"])
        self.ui.keys_status(False, False)
        self.assertIn("in use", self.shell.calls[-1][1])

    def test_taskbar_created_readds(self):
        n = len(self.shell.calls)
        user32.SendMessageW(self.ui.hwnd, self.ui._taskbar_created, 0, 0)
        self.assertEqual([c[0] for c in self.shell.calls[n:]], [ui.NIM_ADD, ui.NIM_SETVERSION])

    def test_hotkey_while_menu_open(self):
        user32.SendMessageW(self.ui.hwnd, ui.WM_HOTKEY, 1, 0)
        self.assertEqual(self.events, [("key", "up")])

    def test_close_removes_icon(self):
        self.ui.close()
        self.assertEqual(self.shell.calls[-1][0], ui.NIM_DELETE)

    def test_run_loop_dispatches_and_pauses(self):
        """The real message loop with fake key registration: window messages
        reach the window, the menu's Pause releases the (fake) keys, the stop
        event ends the loop."""
        from tests import REAL_RUN_LOOP
        from knob.app import kernel32
        reg, unreg = [], []
        saved = hotkeys.register, hotkeys.unregister
        hotkeys.register = lambda hwnd=None: reg.append(hwnd) or (True, 0)
        hotkeys.unregister = lambda hwnd=None: unreg.append(hwnd)
        stop = kernel32.CreateEventW(None, True, False, None)
        self.ui.on_pause = lambda on: (hotkeys.pause if on else hotkeys.resume)()
        self.ui.on_quit = lambda: kernel32.SetEvent(stop)
        try:
            self.ui.show([Row("Spotify", 0.3, False, 1.0)], popup=True)  # posts WM_APP_SHOW
            ui.user32.SetTimer(self.ui.hwnd, 99, 50, None)
            orig = self.ui._wndproc

            def on_timer():
                ui.user32.KillTimer(self.ui.hwnd, 99)
                self.ui.command(ui.CMD_PAUSE)   # pause
                self.ui.command(ui.CMD_QUIT)    # sets the stop event
            def proc(hwnd, msg, wp, lp):
                if msg == ui.WM_TIMER and wp == 99:
                    on_timer()
                    return 0
                return orig(hwnd, msg, wp, lp)
            self.ui._wndproc = proc
            REAL_RUN_LOOP(lambda k: None, stop, hwnd=self.ui.hwnd,
                          on_status=self.ui.keys_status)
        finally:
            hotkeys.register, hotkeys.unregister = saved
            kernel32.CloseHandle(stop)
        self.assertEqual(len(self.presented), 1)          # pop-up drawn from the loop
        self.assertEqual(reg, [self.ui.hwnd])
        self.assertEqual(unreg, [self.ui.hwnd])           # released by Pause
        self.assertTrue(self.ui.paused)
        self.assertIsNone(hotkeys._current)

    def test_app_wiring(self):
        import logging
        from knob import app
        lg = logging.getLogger("knob.test")
        self.assertIsNone(app._make_ui(settings(osd=False, tray_icon=False), None, None, lg))
        # in tests the real tray call is blocked: the program goes on without the windows
        self.ui.close()
        self.assertIsNone(app._make_ui(settings(), Controller.__new__(Controller), None, lg))

    def test_status_error_does_not_stop_the_loop(self):
        def broken(registered, paused):
            raise OSError("CreateIconIndirect failed")
        st = hotkeys._LoopState(None, broken)
        st.status()  # logged, not raised

    def test_post_failure_clears_pending(self):
        saved = ui.user32.PostMessageW
        posts = []
        ui.user32.PostMessageW = lambda *a: posts.append(a) and False
        try:
            self.ui.show([Row("Spotify", 0.3, False, 1.0)], popup=True)
            self.assertIsNone(self.ui._pending)
            self.ui.show([Row("Spotify", 0.4, False, 1.0)], popup=True)
            self.assertEqual(len(posts), 2)  # posted again, not stuck
        finally:
            ui.user32.PostMessageW = saved

    def test_app_ui_constructor_error_falls_back(self):
        import logging
        from knob import app
        saved = ui.Ui
        ui.Ui = lambda *a, **k: (_ for _ in ()).throw(OSError("ctypes failure"))
        try:
            self.assertIsNone(app._make_ui(settings(), Controller.__new__(Controller), None,
                                           logging.getLogger("knob.test")))
        finally:
            ui.Ui = saved

    def test_settings_off(self):
        self.ui.close()
        u = ui.Ui(settings(osd=False, tray_icon=False), on_quit=lambda: None,
                  notify=self.shell, dpi_aware=False)
        n = len(self.shell.calls)
        u.create()
        try:
            self.assertIsNone(u.osd)
            self.assertIsNone(u.tray)
            self.assertEqual(len(self.shell.calls), n)
        finally:
            u.close()


if __name__ == "__main__":
    unittest.main()
