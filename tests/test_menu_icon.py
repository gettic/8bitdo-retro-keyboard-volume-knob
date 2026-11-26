"""v1.3.0: dark/light tray menu window and the % number on the tray icon.
Headless: drawing in memory, menu window created hidden, its showing faked."""
import ctypes
import ctypes.wintypes as wt
import unittest

from knob import gdi, menu, ui

from tests.fakes import settings
from tests.test_display import FakeShell

ITEMS = ui.menu_items(False)          # head, sep, Pause, Open log, Open settings, sep, Quit
PAUSE, LOG, SETTINGS, QUIT = 2, 3, 4, 6


def px(data, size, x, y):
    i = (y * size + x) * 4
    return tuple(data[i:i + 4])  # b, g, r, a


class GdiTests(unittest.TestCase):
    def test_fill_at_least_one_pixel(self):
        with gdi.Bitmap(10, 10) as bm:
            bm.fill(2, 4.6, 6, 0.3, "#FFFFFF")    # rounds to zero height without the fix
            bm.fill(0, 0, 0, 5, "#FFFFFF")         # zero width: nothing
            data = bm.read()
        rows = {y for y in range(10) for x in range(10) if data[(y * 10 + x) * 4]}
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(1 for y in range(10) if data[(y * 10) * 4]), 0)


class StyleSettingTests(unittest.TestCase):
    def test_tray_style(self):
        from knob import config
        self.assertEqual(config.DEFAULTS["tray_style"], "knob_number")
        self.assertEqual(config.validate({"tray_style": "Knob"}), (dict(config.DEFAULTS, tray_style="knob"), []))
        s, w = config.validate({"tray_style": "digits"})
        self.assertEqual((s["tray_style"], len(w)), ("knob_number", 1))


class NumberIconTests(unittest.TestCase):
    def strip_rows(self, size):
        return range(size - ui.strip_height(size), size)

    def test_strip_and_digit_colours(self):
        for size in (16, 20, 24, 32):
            for pct, want in ((45, "white"), (135, "orange")):
                data = ui.icon_pixels(size, pct, number=True)
                rows = list(self.strip_rows(size))
                # strip covers the full width (also the transparent corners)
                self.assertGreaterEqual(px(data, size, 0, rows[-1])[3], ui.STRIP_ALPHA - 1)
                colours = [px(data, size, x, y) for y in rows for x in range(size)]
                if want == "white":
                    self.assertTrue(any(min(c[:3]) > 200 for c in colours), size)
                else:
                    self.assertTrue(any(c[2] > 200 and c[0] < 90 for c in colours), size)

    def test_muted_digits_grey(self):
        data = ui.icon_pixels(16, 45, muted=True, number=True)
        rows = self.strip_rows(16)
        colours = [px(data, 16, x, y) for y in rows for x in range(16)]
        self.assertTrue(any(c[2] > 100 and abs(c[2] - c[0]) < 12 for c in colours))
        self.assertFalse(any(min(c[:3]) > 200 for c in colours))

    def test_no_strip_when_paused_unknown_or_knob_style(self):
        plain = ui.icon_pixels(16, 45)
        self.assertEqual(ui.icon_pixels(16, None, number=True), ui.icon_pixels(16, None))
        self.assertEqual(ui.icon_pixels(16, None, paused=True, number=True),
                         ui.icon_pixels(16, None, paused=True))
        self.assertEqual(px(plain, 16, 0, 15)[3], 0)    # knob style: corner stays clear
        self.assertNotEqual(ui.icon_pixels(16, 45, number=True), plain)

    def test_three_digits_fit_at_16(self):
        data = ui.icon_pixels(16, 100, number=True)
        lit = [x for y in self.strip_rows(16) for x in range(16)
               if min(px(data, 16, x, y)[:3]) > 200]
        self.assertGreaterEqual(min(lit), 1)
        self.assertLessEqual(max(lit), 14)

    def test_cache_style(self):
        made = []
        old = ui.make_hicon
        ui.make_hicon = lambda size, pixels: made.append(bytes(pixels)) or len(made)
        try:
            ui.IconCache(style="knob").get(16, (45, False, False))
            ui.IconCache().get(16, (45, False, False))
        finally:
            ui.make_hicon = old
        self.assertEqual(made[0], bytes(ui.icon_pixels(16, 45)))
        self.assertEqual(made[1], bytes(ui.icon_pixels(16, 45, number=True)))


class MenuLogicTests(unittest.TestCase):
    def test_layout_and_hit(self):
        w, h, rows = menu.layout(ITEMS, 1.0)
        self.assertEqual(w, 230)
        self.assertEqual(h, 4 + 32 + 9 + 32 * 3 + 9 + 32 + 4)
        top, rh = rows[PAUSE]
        self.assertEqual(menu.hit(ITEMS, rows, w, 50, top + 5), PAUSE)
        self.assertIsNone(menu.hit(ITEMS, rows, w, 50, rows[0][0] + 5))    # heading
        self.assertIsNone(menu.hit(ITEMS, rows, w, 50, rows[1][0] + 2))    # separator
        self.assertIsNone(menu.hit(ITEMS, rows, w, -1, top + 5))
        w2, h2, _ = menu.layout(ITEMS, 1.5)
        self.assertEqual((w2, h2), (345, int(round(h * 1.5))))

    def test_keyboard_steps(self):
        self.assertEqual(menu.step(ITEMS, None, 1), PAUSE)
        self.assertEqual(menu.step(ITEMS, None, -1), QUIT)
        self.assertEqual(menu.step(ITEMS, SETTINGS, 1), QUIT)     # skips the separator
        self.assertEqual(menu.step(ITEMS, QUIT, 1), PAUSE)        # wraps
        self.assertEqual(menu.step(ITEMS, PAUSE, -1), QUIT)

    def test_position(self):
        work = (0, 0, 1920, 1032)
        self.assertEqual(menu.position((1800, 1050), 230, 250, work), (1570, 782))
        self.assertEqual(menu.position((100, 1050), 230, 250, work), (100, 782))
        self.assertEqual(menu.position((1900, 100), 230, 250, work), (1670, 100))
        self.assertEqual(menu.position((3000, 900), 230, 250, (1920, 0, 4480, 1400)),
                         (2770, 650))

    def test_render(self):
        for light in (False, True):
            w, h, data = menu.render(ITEMS, PAUSE, 1.0, light)
            self.assertEqual(len(data), w * h * 4)
            self.assertEqual(data[3], 0)
            mid = ((h // 2) * w + w // 2) * 4
            self.assertEqual(data[mid + 3], 255)
            b, g, r, a = data[mid:mid + 4]
            self.assertEqual(r > 128, light)          # dark menu is dark, light is light
        _, _, rows = menu.layout(ITEMS)
        top, rh = rows[PAUSE]
        w, h, on = menu.render(ITEMS, PAUSE)
        _, _, off = menu.render(ITEMS, None)
        i = (int(top + rh / 2) * w + 200) * 4
        self.assertNotEqual(on[i:i + 3], off[i:i + 3])   # hover background


def lp(x, y):
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


class MenuWindowTests(unittest.TestCase):
    """Menu message handling with the window's showing faked."""

    def setUp(self):
        self.chosen = []
        self.m = menu.Menu(0, self.chosen.append)
        self.drawn = []
        self.m._present = lambda *a: self.drawn.append(a)
        self.m._activate = lambda: None
        self.m._track_leave = lambda: None
        self.m._start_watch = self.m._stop_watch = lambda: None
        self.front = 500                       # another app stays in front (focus refused)
        self.m._foreground = lambda: self.front
        self.cursor = (1700, 1000)
        self.m._cursor = lambda: self.cursor
        self.t = 100.0
        self.m.clock = lambda: self.t
        old = menu.osd._monitor_at, menu.osd._monitor_area
        menu.osd._monitor_at = lambda x, y: 1
        menu.osd._monitor_area = lambda m: ((0, 0, 1920, 1032), 1.0)
        self.addCleanup(lambda: setattr(menu.osd, "_monitor_at", old[0]) or
                        setattr(menu.osd, "_monitor_area", old[1]))
        self.m.open(ITEMS, (1800, 1050))
        old_show = menu.user32.ShowWindow
        menu.user32.ShowWindow = lambda h, c: True
        self.addCleanup(lambda: setattr(menu.user32, "ShowWindow", old_show))

    def y_of(self, i):
        top, rh = self.m.rows[i]
        return int(top + rh / 2)

    def test_open_draws_above_click(self):
        self.assertTrue(self.m.visible)
        x, y, w, h, _ = self.drawn[0]
        self.assertLessEqual(y + h, 1032)
        self.assertLessEqual(x + w, 1800)

    def test_mouse_hover_and_click(self):
        self.m.handle(menu.WM_MOUSEMOVE, 0, lp(50, self.y_of(LOG)))
        self.assertEqual(self.m.hover, LOG)
        self.assertEqual(len(self.drawn), 2)
        self.m.handle(menu.WM_MOUSEMOVE, 0, lp(51, self.y_of(LOG)))
        self.assertEqual(len(self.drawn), 2)                 # same item: no redraw
        self.m.handle(menu.WM_LBUTTONUP, 0, lp(50, self.y_of(LOG)))
        self.assertEqual(self.chosen, [ui.CMD_LOG])
        self.assertFalse(self.m.visible)

    def test_click_on_heading_does_nothing(self):
        self.m.handle(menu.WM_LBUTTONUP, 0, lp(50, self.y_of(0)))
        self.assertEqual(self.chosen, [])
        self.assertTrue(self.m.visible)

    def test_keyboard(self):
        self.m.handle(menu.WM_KEYDOWN, menu.VK_DOWN, 0)
        self.m.handle(menu.WM_KEYDOWN, menu.VK_DOWN, 0)
        self.assertEqual(self.m.hover, LOG)
        self.m.handle(menu.WM_KEYDOWN, menu.VK_UP, 0)
        self.m.handle(menu.WM_KEYDOWN, menu.VK_RETURN, 0)
        self.assertEqual(self.chosen, [ui.CMD_PAUSE])

    def test_escape_and_deactivate_close(self):
        self.m.handle(menu.WM_KEYDOWN, menu.VK_ESCAPE, 0)
        self.assertFalse(self.m.visible)
        self.m.open(ITEMS, (1800, 1050))
        self.m.handle(menu.WM_ACTIVATE, 1, 0)                 # becoming active: stays
        self.assertTrue(self.m.visible)
        self.m.handle(menu.WM_ACTIVATE, menu.WA_INACTIVE, 0)  # click elsewhere / app switch
        self.assertFalse(self.m.visible)
        self.assertEqual(self.chosen, [])
        self.assertIsNone(self.m.handle(menu.WM_KEYDOWN, menu.VK_RETURN, 0))  # closed

    def tick(self, seconds=0.15):
        self.t += seconds
        self.m.handle(menu.WM_TIMER, menu.WATCH_TIMER, 0)

    def test_watch_closes_when_another_window_comes_to_front(self):
        self.tick()
        self.assertTrue(self.m.visible)
        self.front = 600                       # user clicked another window
        self.tick()
        self.assertFalse(self.m.visible)
        self.assertTrue(self.m.just_closed())
        self.t += 1
        self.assertFalse(self.m.just_closed())

    def test_watch_closes_after_mouse_away(self):
        self.cursor = (self.m._origin[0] + 10, self.m._origin[1] + 10)   # inside
        self.tick(5)
        self.tick(5)
        self.assertTrue(self.m.visible)
        self.cursor = (100, 100)                                          # away
        self.tick()
        self.tick(menu.LEAVE_SECONDS - 0.5)
        self.assertTrue(self.m.visible)
        self.tick(0.6)
        self.assertFalse(self.m.visible)
        self.assertFalse(self.m.just_closed())

    def test_watch_idle_when_menu_in_front(self):
        self.front = self.m.hwnd
        self.cursor = (100, 100)
        for _ in range(5):
            self.tick(2)
        self.assertTrue(self.m.visible)

    def test_mouse_leave_clears_hover(self):
        self.m.handle(menu.WM_MOUSEMOVE, 0, lp(50, self.y_of(QUIT)))
        self.m.handle(menu.WM_MOUSELEAVE, 0, 0)
        self.assertIsNone(self.m.hover)


user32 = ctypes.WinDLL("user32")
user32.SendMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.IsWindowVisible.argtypes = (wt.HWND,)
user32.PeekMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wt.MSG),)


class UiMenuTests(unittest.TestCase):
    def setUp(self):
        self.shell = FakeShell()
        self.events = []
        self.ui = ui.Ui(settings(), on_quit=lambda: self.events.append("quit"),
                        on_pause=lambda on: self.events.append(("pause", on)),
                        notify=self.shell, opener=self.events.append, dpi_aware=False)
        self.ui.create()
        self.addCleanup(self.ui.close)
        self.opened = []
        self.ui.menu._present = lambda *a: self.opened.append(a)
        self.ui.menu._activate = lambda: None

    def pump(self):
        msg = wt.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), self.ui.hwnd, 0, 0, 1):
            user32.DispatchMessageW(ctypes.byref(msg))

    def test_menu_window_hidden_with_style(self):
        self.assertFalse(user32.IsWindowVisible(self.ui.menu.hwnd))
        self.assertEqual(self.ui.tray.icons.maker.__class__.__name__, "function")

    def test_right_click_opens_at_point_and_choice_runs(self):
        self.ui.tray.v4 = True
        user32.SendMessageW(self.ui.hwnd, ui.WM_APP_TRAY, lp(1700, 1060), ui.WM_CONTEXTMENU)
        self.assertTrue(self.ui.menu.visible)
        self.assertEqual(len(self.opened), 1)
        old_show = menu.user32.ShowWindow
        menu.user32.ShowWindow = lambda h, c: True
        try:
            self.ui.menu.choose(PAUSE)
        finally:
            menu.user32.ShowWindow = old_show
        self.assertFalse(self.ui.menu.visible)
        self.assertEqual(self.events, [])      # runs after the menu closed, from the queue
        self.pump()
        self.assertEqual(self.events, [("pause", True)])

    def right_click(self):
        user32.SendMessageW(self.ui.hwnd, ui.WM_APP_TRAY, lp(1700, 1060), ui.WM_CONTEXTMENU)

    def test_right_click_toggles(self):
        self.ui.tray.v4 = True
        self.right_click()
        self.assertTrue(self.ui.menu.visible)
        self.right_click()                      # open (focus refused): closes, no reopen
        self.assertFalse(self.ui.menu.visible)
        self.assertEqual(len(self.opened), 1)

    def test_click_that_deactivated_does_not_reopen(self):
        self.ui.tray.v4 = True
        t = [100.0]
        self.ui.menu.clock = lambda: t[0]
        self.right_click()
        self.ui.menu.handle(menu.WM_ACTIVATE, menu.WA_INACTIVE, 0)   # the tray click took the front
        self.right_click()
        self.assertFalse(self.ui.menu.visible)
        t[0] += 1                                                    # a later click opens it again
        self.right_click()
        self.assertTrue(self.ui.menu.visible)
        self.assertEqual(len(self.opened), 2)

    def test_style_setting(self):
        self.ui.close()
        u = ui.Ui(settings(tray_style="knob"), on_quit=lambda: None, notify=self.shell,
                  dpi_aware=False)
        u.create()
        try:
            made = []
            old = ui.make_hicon
            ui.make_hicon = lambda size, pixels: made.append(bytes(pixels)) or 1
            try:
                u.tray.icons.get(16, (45, False, False))
            finally:
                ui.make_hicon = old
            self.assertEqual(made, [bytes(ui.icon_pixels(16, 45))])
        finally:
            u.close()


if __name__ == "__main__":
    unittest.main()
