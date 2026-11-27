"""v1.3.0: dark/light tray menu window and the % number on the tray icon.
Headless: drawing in memory, menu window created hidden, its showing faked."""
import ctypes
import ctypes.wintypes as wt
import unittest

from knob import gdi, menu, ui

from tests.fakes import settings
from tests.test_display import FakeShell

ITEMS = ui.menu_items(False)          # head, row, sep, Pause, Open log, Settings, sep, Quit
ROW_I, PAUSE, LOG, SETTINGS, QUIT = 1, 3, 4, 5, 7   # head, row, sep, ..., sep, Quit
MUTE_K = 4                                          # the speaker button in the row


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
        self.assertEqual(h, 4 + 32 + 40 + 9 + 32 * 3 + 9 + 32 + 4)
        top, rh = rows[PAUSE]
        self.assertEqual(menu.hit(ITEMS, rows, w, 50, top + 5), PAUSE)
        self.assertIsNone(menu.hit(ITEMS, rows, w, 50, rows[0][0] + 5))    # heading
        self.assertIsNone(menu.hit(ITEMS, rows, w, 50, rows[1][0] + 2))    # separator
        self.assertIsNone(menu.hit(ITEMS, rows, w, -1, top + 5))
        w2, h2, _ = menu.layout(ITEMS, 1.5)
        self.assertEqual((w2, h2), (345, int(round(h * 1.5))))

    def test_keyboard_steps(self):
        self.assertEqual(menu.step(ITEMS, None, 1), (ROW_I, 0))
        self.assertEqual(menu.step(ITEMS, None, -1), QUIT)
        self.assertEqual(menu.step(ITEMS, SETTINGS, 1), QUIT)     # skips the separator
        self.assertEqual(menu.step(ITEMS, QUIT, 1), (ROW_I, 0))   # wraps onto the row
        self.assertEqual(menu.step(ITEMS, (ROW_I, 2), 1), PAUSE)
        self.assertEqual(menu.step(ITEMS, PAUSE, -1), (ROW_I, 0))
        marked = ui.menu_items(False, level=0.72)
        self.assertEqual(menu.step(marked, PAUSE, -1), (ROW_I, 2))  # enters on 75 %

    def test_row_left_right_and_commands(self):
        self.assertEqual(menu.side(ITEMS, (ROW_I, 0), 1), (ROW_I, 1))
        self.assertEqual(menu.side(ITEMS, (ROW_I, 0), -1), (ROW_I, MUTE_K))   # wraps
        self.assertEqual(menu.side(ITEMS, (ROW_I, 3), 1), (ROW_I, MUTE_K))    # onto mute
        self.assertEqual(menu.side(ITEMS, (ROW_I, MUTE_K), 1), (ROW_I, 0))
        self.assertEqual(menu.side(ITEMS, PAUSE, 1), PAUSE)              # not in the row
        self.assertEqual([menu.target_cmd(ITEMS, (ROW_I, k)) for k in range(5)],
                         [125, 150, 175, 200, ui.CMD_MUTE])
        self.assertEqual(menu.target_cmd(ITEMS, PAUSE), ui.CMD_PAUSE)
        self.assertIsNone(menu.target_cmd(ITEMS, 0))

    def test_mark_closest_and_boost(self):
        info = lambda lv: ui.quick_row(lv)[1]
        self.assertIsNone(info(None)["mark"])
        self.assertEqual([info(v)["mark"] for v in (0.0, 0.3, 0.49, 0.72, 0.9, 1.0)],
                         [0, 0, 1, 2, 3, 3])
        self.assertEqual((info(1.35)["mark"], info(1.35)["boosted"]), (3, True))
        self.assertFalse(info(1.0)["boosted"])

    def test_mute_button_label_and_state(self):
        on, off = ui.quick_row(0.72, muted=True)[1], ui.quick_row(0.72)[1]
        self.assertEqual(off["buttons"][MUTE_K], (ui.CMD_MUTE, "Mute", menu.ICON_SPEAKER))
        self.assertEqual(on["buttons"][MUTE_K], (ui.CMD_MUTE, "Unmute", menu.ICON_MUTED))
        self.assertEqual(off["mark"], 2)                       # 75 % outlined
        self.assertEqual(on["mark"], MUTE_K)                   # no % outline, mute outlined
        boosted = ui.quick_row(1.35, muted=True)[1]
        self.assertEqual((boosted["mark"], boosted["boosted"]), (MUTE_K, False))
        self.assertEqual(ui.menu_items(False, level=0.5, muted=True)[ROW_I][1]["mark"], MUTE_K)

    def test_mute_button_drawn_and_outlined(self):
        def button(items, k, light):
            """(left-edge pixel, inner pixels) of button k, as (r, g, b)."""
            w, h, data = menu.render(items, None, 1.0, light)
            _, _, rows = menu.layout(items)
            top, rh = rows[ROW_I]
            bx, by, bw, bh = (int(v) for v in menu.button_rects(w, top, rh, 1.0, 5)[k])
            pix = lambda x, y: tuple(data[(y * w + x) * 4:(y * w + x) * 4 + 3][::-1])
            inner = [pix(x, y) for y in range(by + 3, by + bh - 3)
                     for x in range(bx + 3, bx + bw - 3)]
            return pix(bx + 1, by + bh // 2), inner   # +1: inside the outline
        for light in (False, True):
            hexc = (menu.LIGHT if light else menu.DARK)["accent"]
            accent = tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))
            plain = ui.menu_items(False, level=0.72)
            muted = ui.menu_items(False, level=0.72, muted=True)
            edge_off, icon_off = button(plain, MUTE_K, light)
            edge_on, icon_on = button(muted, MUTE_K, light)
            self.assertNotEqual(edge_off, accent)             # not outlined while not muted
            self.assertEqual(edge_on, accent)                 # outlined when muted
            self.assertNotEqual(icon_off, icon_on)            # speaker vs muted speaker
            self.assertGreater(len(set(icon_off)), 2)         # something drawn on it
            self.assertEqual(button(plain, 2, light)[0], accent)     # 75 % outlined ...
            self.assertNotEqual(button(muted, 2, light)[0], accent)  # ... not when muted

    def test_mute_icon_fallback_shape(self):
        old = menu._icon_face_cache[:]
        menu._icon_face_cache[:] = [None]                      # no icon font installed
        try:
            a = menu.render(ui.menu_items(False), None)[2]
            b = menu.render(ui.menu_items(False, muted=True), None)[2]
        finally:
            menu._icon_face_cache[:] = old
        self.assertNotEqual(a, b)

    def test_row_hit(self):
        w, h, rows = menu.layout(ITEMS, 1.5)
        top, rh = rows[ROW_I]
        rects = menu.button_rects(w, top, rh, 1.5, 5)
        for k, (bx, by, bw, bh) in enumerate(rects):
            self.assertEqual(menu.hit(ITEMS, rows, w, bx + bw / 2, by + bh / 2, 1.5), (ROW_I, k))
        bx, by, bw, bh = rects[0]
        self.assertIsNone(menu.hit(ITEMS, rows, w, bx + bw + 1, by + 5, 1.5))   # in the gap
        self.assertLessEqual(rects[-1][0] + rects[-1][2], w)

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
        self.assertEqual(self.m.hover, (ROW_I, 0))              # the row (nothing marked)
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

    def test_click_row_button(self):
        top, rh = self.m.rows[ROW_I]
        bx, by, bw, bh = menu.button_rects(self.m.width, top, rh, 1.0, 5)[1]
        self.m.handle(menu.WM_MOUSEMOVE, 0, lp(int(bx + 5), int(by + 5)))
        self.assertEqual(self.m.hover, (ROW_I, 1))
        self.m.handle(menu.WM_LBUTTONUP, 0, lp(int(bx + 5), int(by + 5)))
        self.assertEqual(self.chosen, [150])
        self.assertFalse(self.m.visible)

    def test_keyboard_in_row(self):
        for key in (menu.VK_DOWN, menu.VK_RIGHT, menu.VK_RIGHT, menu.VK_RETURN):
            self.m.handle(menu.WM_KEYDOWN, key, 0)
        self.assertEqual(self.chosen, [175])

    def test_click_mute_button(self):
        top, rh = self.m.rows[ROW_I]
        bx, by, bw, bh = menu.button_rects(self.m.width, top, rh, 1.0, 5)[MUTE_K]
        self.m.handle(menu.WM_MOUSEMOVE, 0, lp(int(bx + bw / 2), int(by + bh / 2)))
        self.assertEqual(self.m.hover, (ROW_I, MUTE_K))
        self.m.handle(menu.WM_LBUTTONUP, 0, lp(int(bx + bw / 2), int(by + bh / 2)))
        self.assertEqual(self.chosen, [ui.CMD_MUTE])

    def test_keyboard_reaches_mute(self):
        for key in (menu.VK_DOWN, menu.VK_LEFT, menu.VK_RETURN):    # row, wrap left onto mute
            self.m.handle(menu.WM_KEYDOWN, key, 0)
        self.assertEqual(self.chosen, [ui.CMD_MUTE])

    def test_keyboard_enters_muted_row_on_mute(self):
        self.m.close()
        self.m.open(ui.menu_items(False, level=0.5, muted=True), (1800, 1050))
        for key in (menu.VK_DOWN, menu.VK_RETURN):
            self.m.handle(menu.WM_KEYDOWN, key, 0)
        self.assertEqual(self.chosen, [ui.CMD_MUTE])

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

    def test_menu_window_has_arrow_cursor(self):
        # no class cursor -> the busy/app-starting cursor stayed over the menu
        u32 = ctypes.WinDLL("user32", use_last_error=True)  # own copy: own argtypes
        u32.GetClassLongPtrW.restype = ctypes.c_void_p
        u32.GetClassLongPtrW.argtypes = (wt.HWND, ctypes.c_int)
        u32.LoadCursorW.restype = ctypes.c_void_p
        u32.LoadCursorW.argtypes = (wt.HINSTANCE, ctypes.c_void_p)
        arrow = u32.LoadCursorW(None, ui.IDC_ARROW)
        self.assertTrue(arrow)
        self.assertEqual(u32.GetClassLongPtrW(self.ui.menu.hwnd, -12), arrow)  # GCLP_HCURSOR

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

    def test_quick_set_command_goes_to_controller(self):
        keys = []
        self.ui.on_key = keys.append
        self.ui.command(150)
        self.ui.command(200)
        self.assertEqual(keys, [("set", 0.5), ("set", 1.0)])

    def test_menu_marks_current_volume(self):
        from knob.display import Row
        self.ui.rows = [Row("YouTube", 1.35, False, 1.5)]
        self.ui.open_menu((1700, 1060))
        info = self.ui.menu.items[ROW_I][1]
        self.assertEqual((info["mark"], info["boosted"]), (3, True))
        self.assertEqual(info["buttons"][MUTE_K][1], "Mute")

    def test_mute_command_is_the_mute_key(self):
        keys = []
        self.ui.on_key = keys.append
        self.ui.command(ui.CMD_MUTE)
        self.assertEqual(keys, ["mute"])          # same path as the knob's Mute key

    def test_menu_shows_unmute_only_when_all_muted(self):
        from knob.display import Row
        self.ui.rows = [Row("Spotify", 0.4, True, 1.0), Row("YouTube", 0.6, False, 1.5)]
        self.ui.open_menu((1700, 1060))
        info = self.ui.menu.items[ROW_I][1]
        self.assertEqual((info["buttons"][MUTE_K][1], info["mark"]), ("Mute", 1))
        self.ui.menu.close()
        self.ui.menu.closed_at = None
        self.ui.rows = [Row("Spotify", 0.4, True, 1.0), Row("YouTube", 0.6, True, 1.5)]
        self.ui.open_menu((1700, 1060))
        info = self.ui.menu.items[ROW_I][1]
        self.assertEqual((info["buttons"][MUTE_K][1], info["mark"]), ("Unmute", MUTE_K))

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


class QuickSetControllerTests(unittest.TestCase):
    """Controller.set_level with fake audio, fake browser link, fake Spotify."""

    def setUp(self):
        from knob.controller import Controller
        from tests.fakes import (FakeAudio, FakeClock, FakeSession, ProcessList, connect_host,
                                 make_bridge)
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)
        self.connect = connect_host
        self.spotify = FakeSession("spotify.exe", volume=0.4, muted=True)
        self.audio = FakeAudio([self.spotify])
        self.shown = []
        self.make = lambda sp=None: Controller(
            settings(), lambda: self.audio, self.bridge, clock=self.clock,
            process_lister=ProcessList(), display=lambda r, p: self.shown.append((r, p)),
            spotify=sp)
        self.ctl = self.make()
        self.ctl.audio = self.audio

    def tab(self, tid, playing, volume=0.5, muted=False):
        return {"tab": tid, "site": "youtube", "playing": playing, "volume": volume,
                "muted": muted, "max_volume": 1.5, "boost": "on"}

    def test_playing_tab_only_boost_dropped_and_unmuted(self):
        self.connect(self.bridge, 1, [self.tab(7, True, 1.35, muted=True), self.tab(8, False)])
        self.ctl.poll()
        self.ctl.on_event(("set", 0.5))
        sent = [m for _, m in self.rec.sent if m["type"] != "hello"]
        self.assertEqual(sent, [{"type": "mute", "muted": False, "tabs": [7]},
                                {"type": "set_volume", "volume": 0.5, "tabs": [7]}])
        self.assertEqual(self.spotify._volume, 0.4)          # not playing: untouched
        rows, popup = self.shown[-1]
        self.assertTrue(popup)
        self.assertEqual((rows[0].name, rows[0].level, rows[0].muted), ("YouTube", 0.5, False))

    def test_nothing_playing_sets_both(self):
        self.connect(self.bridge, 1, [self.tab(7, False)])
        self.ctl.poll()
        self.ctl.on_event(("set", 0.75))
        self.assertEqual((self.spotify._volume, self.spotify._muted), (0.75, False))
        self.assertEqual(self.rec.sent[-1][1]["type"], "set_volume")
        self.assertEqual([r.name for r in self.shown[-1][0]], ["Spotify", "YouTube"])

    def test_spotify_web_bar(self):
        from tests.test_spotify import FakeSpotifyVolume

        class Web(FakeSpotifyVolume):
            def set_to(self, level):
                self.set = level
                self._level = level
                return level
        sp = Web(level=0.4)
        ctl = self.make(sp)
        ctl.audio = self.audio
        self.spotify._peak = 0.3
        ctl.poll()
        ctl.on_event(("set", 1.0))
        self.assertEqual(sp.set, 1.0)
        self.assertEqual(self.spotify._volume, 0.4)          # mixer volume untouched
        self.assertFalse(self.spotify._muted)                 # mixer mute cleared

    def test_spotify_web_unknown_falls_back_to_mixer(self):
        from tests.test_spotify import FakeSpotifyVolume

        class Unknown(FakeSpotifyVolume):
            def set_to(self, level):
                return None
        ctl = self.make(Unknown())
        ctl.audio = self.audio
        self.spotify._peak = 0.3
        ctl.poll()
        ctl.on_event(("set", 0.25))
        self.assertEqual(self.spotify._volume, 0.25)


from tests import test_spotify as _ts  # noqa: E402  (fake Spotify on a local port)


class SpotifySetToTests(_ts.Base):
    def test_set_to(self):
        self.login_file("dev-pc")
        v = _ts.api.SpotifyVolume(self.path, self.fake.api, self.fake.accounts, pc_name=_ts.PC,
                                  tick=0.01)
        self.addCleanup(v.close)
        self.assertIsNone(v.set_to(0.5))           # volume not known yet: nothing queued
        v.start()
        v.set_present(True)
        self.assertTrue(_ts.wait_for(v.usable))
        self.assertAlmostEqual(v.set_to(0.75), 0.75)
        self.assertTrue(_ts.wait_for(lambda: self.fake.volume == 75))
        self.assertAlmostEqual(v.set_to(1.4), 1.0)  # no boost for Spotify
        self.assertTrue(_ts.wait_for(lambda: self.fake.volume == 100))


if __name__ == "__main__":
    unittest.main()
