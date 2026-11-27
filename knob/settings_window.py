"""The settings window (tray menu > Settings): a normal window with Windows'
own title bar, its inside drawn by us like the menu and the pop-up (dark /
light, Segoe UI). Three kinds of controls: on/off switch, a row of choice
buttons and a [-] value [+] stepper.

Every change is checked, written into the settings file at once (only that
key; hand edits and other keys stay) and applied live by the program. A
settings file that can't be read is never overwritten: the window then only
offers to open the file or reset it (the old file is kept).

Layout, hit-testing, keyboard and value logic are plain functions so they
can be tested without a window."""
import ctypes
import ctypes.wintypes as wt
import logging

from . import config, gdi, menu, osd

log = logging.getLogger("knob.settings")

user32 = gdi.user32


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


_d = gdi._decl
_d(user32, "BeginPaint", wt.HDC, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
_d(user32, "EndPaint", wt.BOOL, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
_d(user32, "InvalidateRect", wt.BOOL, wt.HWND, ctypes.c_void_p, wt.BOOL)
_d(user32, "AdjustWindowRectEx", wt.BOOL, ctypes.POINTER(wt.RECT), wt.DWORD, wt.BOOL, wt.DWORD)
_d(user32, "GetKeyState", ctypes.c_short, ctypes.c_int)
_d(user32, "SetCursor", wt.HANDLE, wt.HANDLE)
_d(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p)
_d(user32, "IsIconic", wt.BOOL, wt.HWND)
_d(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_d(user32, "SetCapture", wt.HWND, wt.HWND)
_d(user32, "ReleaseCapture", wt.BOOL)

WS_CAPTION, WS_SYSMENU, WS_MINIMIZEBOX = 0x00C00000, 0x00080000, 0x00020000
STYLE = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX   # fixed size, Windows' title bar
EX_STYLE = 0                                       # a normal window (taskbar, Alt+Tab)

WM_CLOSE, WM_PAINT, WM_ERASEBKGND, WM_SETCURSOR = 0x10, 0xF, 0x14, 0x20
WM_SETTINGCHANGE, WM_THEMECHANGED, WM_DPICHANGED = 0x1A, 0x31A, 0x2E0
WM_KEYDOWN, WM_MOUSEMOVE, WM_LBUTTONUP, WM_MOUSELEAVE = 0x100, 0x200, 0x202, 0x2A3
WM_LBUTTONDOWN = 0x201
VK_TAB, VK_RETURN, VK_SHIFT, VK_ESCAPE, VK_SPACE = 0x09, 0x0D, 0x10, 0x1B, 0x20
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
HTCLIENT, IDC_HAND = 1, 32649
SW_HIDE, SW_SHOWNORMAL, SW_RESTORE = 0, 1, 9
SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
DWMWA_USE_IMMERSIVE_DARK_MODE = 20

TITLE = "8BitDo Volume Knob settings"

DARK = dict(win="#202020", card="#2B2B2B", edge="#363636", fg="#FFFFFF", dim="#A8A8A8",
            off="#5A5A5A", accent="#4CC2FF", on_knob="#000000", toggle_edge="#9A9A9A",
            link="#4CC2FF", warn_bg="#433519", warn_edge="#6B5427", warn="#FCE100")
LIGHT = dict(win="#F3F3F3", card="#FBFBFB", edge="#E3E3E3", fg="#1A1A1A", dim="#5C5C5C",
             off="#B0B0B0", accent="#005FB8", on_knob="#FFFFFF", toggle_edge="#7A7A7A",
             link="#005FB8", warn_bg="#FFF4CE", warn_edge="#F0D58C", warn="#9D5D00")

# layout at 100 % scaling
WIDTH = 560
TOP = 8
MARGIN = 16
HEAD_H = 34
ROW_H, ROW_H_SUB = 50, 62
GAP = 4
FOOT_H = 52
CTRL_H = 30
BANNER_H = 104
FONT, FONT_SUB, FONT_HEAD, FONT_CTRL = 14, 12, 14, 13

# the values the window offers (a hand-set value in between is kept and shown)
STEPS = (0.01, 0.013, 0.015, 0.02, 0.03, 0.05)
OSD_SECONDS = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
FAILSAFE_LEVELS = tuple(round(k / 10, 1) for k in range(1, 11))


def pct(v):
    return f"{v * 100:.1f}".rstrip("0").rstrip(".") + "%"


def secs(v):
    return f"{v:g} s"


ROWS = [
    dict(kind="head", label="Knob"),
    dict(kind="stepper", key="step", label="Each knob click changes the volume by",
         values=STEPS, fmt=pct),
    dict(kind="choice", key="spotify_control", label="Spotify volume",
         sub="Its own slider needs a one-time login",
         options=(("web", "Spotify's own"), ("mixer", "Windows mixer"))),
    dict(kind="toggle", key="brave_fallback",
         label="Without the extension, change all of Brave's sound"),
    dict(kind="head", label="YouTube and YouTube Music"),
    dict(kind="choice", key="boost_max", label="Can go louder than 100%",
         options=((1.0, "No"), (1.25, "125%"), (1.5, "150%")), fmt=pct),
    dict(kind="toggle", key="failsafe", label="Turn tabs over 100% down when they open",
         sub="So a boosted video never starts too loud"),
    dict(kind="stepper", key="failsafe_level", label="Turn them down to",
         values=FAILSAFE_LEVELS, fmt=pct, needs="failsafe"),
    dict(kind="head", label="Volume pop-up"),
    dict(kind="toggle", key="osd", label="Show the pop-up when I turn the knob"),
    dict(kind="stepper", key="osd_seconds", label="Pop-up stays for", values=OSD_SECONDS,
         fmt=secs, needs="osd"),
    dict(kind="choice", key="osd_monitor", label="Show it on", needs="osd",
         options=(("primary", "Main screen"), ("mouse", "Screen with the mouse"))),
    dict(kind="head", label="Taskbar icon"),
    dict(kind="choice", key="tray_style", label="The icon shows",
         options=(("knob_number", "Knob and number"), ("knob", "Knob only"))),
]
KEYS = tuple(r["key"] for r in ROWS if r["kind"] != "head")
FILE, RESET = "file", "reset"   # the footer link and the broken-file banner's button
LINK_TEXT = "Open the settings file (advanced)"
SAVED_TEXT = "Changes are saved right away"
FAILED_TEXT = "Couldn't save. Try again, or see Open log"
BANNER_TEXT = "Your settings file can't be read, so changes can't be saved."
BANNER_SUB = "Fix the file, or reset to defaults (a copy of the old file is kept)."


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def options_for(row, value):
    """[(value, label)] of a choice row; a hand-set value that isn't one of
    them is added in order so it is shown (and kept) as it is."""
    opts = list(row["options"])
    if not any(_same(v, value) for v, _ in opts) and "fmt" in row and value is not None:
        opts.append((value, row["fmt"](value)))
        opts.sort(key=lambda o: o[0])
    return opts


def stepper_next(values, current, direction):
    """Next allowed value up (+1) / down (-1) from current, or None at the end."""
    if direction > 0:
        return next((v for v in values if v > current + 1e-9), None)
    return next((v for v in reversed(values) if v < current - 1e-9), None)


def enabled(row, values, broken=False):
    if broken:
        return False
    need = row.get("needs")
    return need is None or bool(values.get(need))


# ------------------------------------------------------------ layout
class Layout:
    """Pixel positions of everything at one scale. targets: [(name, part,
    rect)] that can be clicked; rings: name -> focus ring rect; order: the
    names Tab goes through."""

    def __init__(self, width, height, scale):
        self.width, self.height, self.scale = width, height, scale
        self.rows = []      # (row dict, top, height)
        self.targets = []
        self.rings = {}
        self.order = []
        self.banner = None  # (top, height)
        self.foot = 0


def _toggle_rect(right, cy, s):
    w, h = 40 * s, 20 * s
    return (right - w, cy - h / 2, w, h)


def layout(values, scale=1.0, broken=False, measure=None):
    """Positions for `values`. measure(text, size, weight) -> width in px
    (default: GDI)."""
    own = None
    if measure is None:
        own = gdi.Bitmap(1, 1)
        measure = lambda text, size, weight=400: own.text_width(text, size, weight=weight)
    try:
        return _layout(values, scale, broken, measure)
    finally:
        if own:
            own.close()


def _layout(values, s, broken, measure):
    w = int(round(WIDTH * s))
    y = TOP * s
    lay = Layout(w, 0, s)
    x0, cw = MARGIN * s, w - 2 * MARGIN * s
    if broken:
        lay.banner = (y, BANNER_H * s)
        bx, by, bh = x0 + 16 * s, y + 62 * s, CTRL_H * s
        for name, text in ((FILE, "Open the file"), (RESET, "Reset to defaults")):
            bw = measure(text, FONT_CTRL * s, 600) + 28 * s
            lay.targets.append((name, None, (bx, by, bw, bh)))
            lay.rings[name] = (bx, by, bw, bh)
            lay.order.append(name)
            bx += bw + 8 * s
        y += (BANNER_H + 8) * s
    for row in ROWS:
        if row["kind"] == "head":
            h = HEAD_H * s
            lay.rows.append((row, y, h))
            y += h
            continue
        h = (ROW_H_SUB if row.get("sub") else ROW_H) * s
        lay.rows.append((row, y, h))
        key, on = row["key"], enabled(row, values, broken)
        right, cy = x0 + cw - 16 * s, y + h / 2
        ch = CTRL_H * s
        if row["kind"] == "toggle":
            rect = _toggle_rect(right, cy, s)
            parts = [("toggle", rect)]
            ring = rect
        elif row["kind"] == "stepper":
            bw, vw = 32 * s, 64 * s
            x = right - 2 * bw - vw
            parts = [(-1, (x, cy - ch / 2, bw, ch)), (1, (x + bw + vw, cy - ch / 2, bw, ch))]
            ring = (x, cy - ch / 2, 2 * bw + vw, ch)
        else:
            opts = options_for(row, values.get(key))
            widths = [measure(label, FONT_CTRL * s, 600) + 24 * s for _, label in opts]
            gap = 4 * s
            x = right - sum(widths) - gap * (len(opts) - 1)
            ring = (x, cy - ch / 2, right - x, ch)
            parts = []
            for k, bw in enumerate(widths):
                parts.append((k, (x, cy - ch / 2, bw, ch)))
                x += bw + gap
        if on:
            lay.targets += [(key, part, rect) for part, rect in parts]
            lay.rings[key] = ring
            lay.order.append(key)
        y += h + GAP * s
    lay.foot = y
    tw = measure(LINK_TEXT, FONT_CTRL * s)
    rect = (x0 + 2 * s, y + (FOOT_H * s - 22 * s) / 2, tw, 22 * s)
    lay.targets.append((FILE, "link", rect))
    if FILE not in lay.rings:
        lay.rings[FILE] = rect
        lay.order.append(FILE)
    lay.height = int(round(y + FOOT_H * s))
    return lay


def hit(lay, x, y):
    """(name, part) under the point, or None."""
    for name, part, (rx, ry, rw, rh) in lay.targets:
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return (name, part)
    return None


def focus_step(lay, current, direction):
    """Next focusable name (Tab / Shift+Tab, wraps)."""
    if not lay.order:
        return None
    if current not in lay.order:
        return lay.order[0] if direction > 0 else lay.order[-1]
    return lay.order[(lay.order.index(current) + direction) % len(lay.order)]


def row_of(key):
    return next((r for r in ROWS if r.get("key") == key), None)


def change_for(values, target):
    """(key, new value) a click on target means, or None (nothing changes)."""
    if target is None:
        return None
    name, part = target
    row = row_of(name)
    if row is None:
        return None
    value = values.get(name)
    if row["kind"] == "toggle":
        return (name, not value)
    if row["kind"] == "stepper":
        new = stepper_next(row["values"], value, part)
        return None if new is None else (name, new)
    opts = options_for(row, value)
    if not isinstance(part, int) or not 0 <= part < len(opts):
        return None
    new = opts[part][0]
    return None if _same(new, value) else (name, new)


def key_target(values, focus, vk):
    """Target a key press on the focused control means (as if clicked)."""
    row = row_of(focus)
    if row is None:
        return (focus, None) if focus in (FILE, RESET) and vk in (VK_RETURN, VK_SPACE) else None
    if row["kind"] == "toggle":
        if vk in (VK_RETURN, VK_SPACE):
            return (focus, "toggle")
        if vk in (VK_LEFT, VK_RIGHT) and bool(values.get(focus)) != (vk == VK_RIGHT):
            return (focus, "toggle")
        return None
    if vk not in (VK_LEFT, VK_RIGHT):
        return None
    d = -1 if vk == VK_LEFT else 1
    if row["kind"] == "stepper":
        return (focus, d)
    opts = options_for(row, values.get(focus))
    k = next((i for i, (v, _) in enumerate(opts) if _same(v, values.get(focus))), 0)
    if 0 <= k + d < len(opts):
        return (focus, k + d)
    return None


# ------------------------------------------------------------ drawing
def _ring_box(bm, x, y, w, h, r, ring, fill, s, width=1.0):
    bm.round_rect(x, y, w, h, r, ring)
    bm.round_rect(x + width * s, y + width * s, w - 2 * width * s, h - 2 * width * s,
                  max(0.5, r - width * s), fill)


def _focus_ring(bm, rect, t, s, bg):
    x, y, w, h = rect
    p = 3 * s
    _ring_box(bm, x - p, y - p, w + 2 * p, h + 2 * p, 7 * s, t["fg"], bg, s, 2)


def render(values, scale=1.0, light=False, focus=None, hover=None, broken=False,
           failed=False):
    """Draw the window's inside. Returns (w, h, BGRA bytes) (opaque)."""
    t = LIGHT if light else DARK
    b = menu.BUTTONS["light" if light else "dark"]
    s = scale
    with gdi.Bitmap(1, 1) as m:
        lay = layout(values, s, broken,
                     lambda text, size, weight=400: m.text_width(text, size, weight=weight))
    w, h = lay.width, lay.height
    with gdi.Bitmap(w, h) as bm:
        bm.fill(0, 0, w, h, t["win"])
        x0, cw = MARGIN * s, w - 2 * MARGIN * s
        if lay.banner:
            top, bh = lay.banner
            _ring_box(bm, x0, top, cw, bh, 6 * s, t["warn_edge"], t["warn_bg"], s)
            bm.text(BANNER_TEXT, x0 + 16 * s, top + 10 * s, cw - 32 * s, 22 * s, FONT * s,
                    t["fg"], weight=600)
            bm.text(BANNER_SUB, x0 + 16 * s, top + 32 * s, cw - 32 * s, 20 * s, FONT_SUB * s,
                    t["fg"])
            for name, label in ((FILE, "Open the file"), (RESET, "Reset to defaults")):
                rect = next(r for n, p, r in lay.targets if n == name and p is None)
                if focus == name:
                    _focus_ring(bm, rect, t, s, t["warn_bg"])
                bx, by, bw, bh2 = rect
                fill = b["hover"] if hover == (name, None) else b["bg"]
                bm.round_rect(bx, by, bw, bh2, 5 * s, fill)
                bm.text(label, bx, by, bw, bh2, FONT_CTRL * s, t["fg"], weight=600,
                        align=gdi.DT_CENTER)
        for row, top, rh in lay.rows:
            if row["kind"] == "head":
                bm.text(row["label"], x0 + 2 * s, top + 6 * s, cw, rh - 6 * s, FONT_HEAD * s,
                        t["fg"], weight=600)
                continue
            key = row["key"]
            on = enabled(row, values, broken)
            fg = t["fg"] if on else t["off"]
            _ring_box(bm, x0, top, cw, rh, 6 * s, t["edge"], t["card"], s)
            lx, right, cy = x0 + 16 * s, x0 + cw - 16 * s, top + rh / 2
            ring = lay.rings.get(key)
            if ring is None:  # disabled: same place, worked out again
                ring = _layout_ring(row, values, s, right, cy)
            lw = ring[0] - lx - 16 * s - (48 * s if row["kind"] == "toggle" else 0)
            if row.get("sub"):
                bm.text(row["label"], lx, top + 10 * s, lw, 22 * s, FONT * s, fg)
                bm.text(row["sub"], lx, top + 32 * s, lw, 18 * s, FONT_SUB * s,
                        t["dim"] if on else t["off"])
            else:
                bm.text(row["label"], lx, top, lw, rh, FONT * s, fg)
            if focus == key and on:
                _focus_ring(bm, ring, t, s, t["card"])
            hv = hover[1] if hover and hover[0] == key else None
            if row["kind"] == "toggle":
                _draw_toggle(bm, ring, bool(values.get(key)), t, s, on)
            elif row["kind"] == "stepper":
                _draw_stepper(bm, row, ring, values.get(key), t, b, s, on, hv)
            else:
                _draw_choice(bm, row, ring, values.get(key), t, b, s, on, hv)
        fy = lay.foot
        link = next(r for n, p, r in lay.targets if n == FILE and p == "link")
        if focus == FILE and not broken:
            _focus_ring(bm, link, t, s, t["win"])
        lx, ly, lw, lh = link
        bm.text(LINK_TEXT, lx, ly, lw + 4 * s, lh, FONT_CTRL * s, t["link"])
        if hover == (FILE, "link"):
            bm.fill(lx, ly + lh - 2 * s, lw, max(1, int(s)), t["link"])
        note, colour = (FAILED_TEXT, t["warn"]) if failed else (SAVED_TEXT, t["dim"])
        if not broken:
            bm.text(note, w - 300 * s - MARGIN * s, fy, 300 * s, FOOT_H * s, FONT_SUB * s,
                    colour, align=gdi.DT_RIGHT)
        data = bm.read()
    data[3::4] = b"\xff" * (w * h)
    return w, h, data


def _layout_ring(row, values, s, right, cy):
    """Control area of a disabled row (it has no targets in the layout)."""
    ch = CTRL_H * s
    if row["kind"] == "toggle":
        return _toggle_rect(right, cy, s)
    if row["kind"] == "stepper":
        return (right - 128 * s, cy - ch / 2, 128 * s, ch)
    with gdi.Bitmap(1, 1) as m:
        widths = [m.text_width(label, FONT_CTRL * s, weight=600) + 24 * s
                  for _, label in options_for(row, values.get(row["key"]))]
    total = sum(widths) + 4 * s * (len(widths) - 1)
    return (right - total, cy - ch / 2, total, ch)


def _draw_toggle(bm, rect, on, t, s, enabled_):
    x, y, w, h = rect
    cy = y + h / 2
    if on:
        bm.round_rect(x, y, w, h, h / 2, t["accent"] if enabled_ else t["off"])
        bm.ellipse(x + w - h / 2, cy, 12 * s, t["on_knob"])
    else:
        edge = t["toggle_edge"] if enabled_ else t["off"]
        _ring_box(bm, x, y, w, h, h / 2, edge, t["card"], s)
        bm.ellipse(x + h / 2, cy, 10 * s, edge)
    bm.text("On" if on else "Off", x - 44 * s, y, 36 * s, h, FONT_CTRL * s,
            t["fg"] if enabled_ else t["off"], align=gdi.DT_RIGHT)


def _draw_stepper(bm, row, rect, value, t, b, s, on, hover):
    x, y, w, h = rect
    bw, vw = 32 * s, w - 64 * s
    for d, bx, glyph in ((-1, x, "−"), (1, x + bw + vw, "+")):
        can = on and stepper_next(row["values"], value, d) is not None
        bm.round_rect(bx, y, bw, h, 5 * s, b["hover"] if hover == d and can else b["bg"])
        bm.text(glyph, bx, y - 1 * s, bw, h, 16 * s, t["fg"] if can else t["off"], weight=600,
                align=gdi.DT_CENTER)
    bm.text(row["fmt"](value), x + bw, y, vw, h, FONT * s, t["fg"] if on else t["off"],
            weight=600, align=gdi.DT_CENTER)


def _draw_choice(bm, row, rect, value, t, b, s, on, hover):
    x, y, w, h = rect
    opts = options_for(row, value)
    widths = [bm.text_width(label, FONT_CTRL * s, weight=600) + 24 * s for _, label in opts]
    fg = t["fg"] if on else t["off"]
    for k, ((v, label), bw) in enumerate(zip(opts, widths)):
        fill = b["hover"] if hover == k and on else b["bg"]
        chosen = _same(v, value)
        if chosen:
            _ring_box(bm, x, y, bw, h, 5 * s, t["accent"] if on else t["off"], fill, s, 1.5)
        else:
            bm.round_rect(x, y, bw, h, 5 * s, fill)
        bm.text(label, x, y, bw, h, FONT_CTRL * s, fg, weight=600 if chosen else 400,
                align=gdi.DT_CENTER)
        x += bw + 4 * s


# ------------------------------------------------------------ the window
def _signed(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class SettingsWindow:
    """hwnd: made hidden by ui.Ui (STYLE / EX_STYLE). folder: where
    settings.json is. on_change(changes dict) after a change was saved;
    open_file() for the footer link. Tests replace _show / _invalidate."""

    def __init__(self, hwnd, folder, on_change, open_file):
        self.hwnd = hwnd
        self.folder = folder
        self.on_change = on_change
        self.open_file = open_file
        self.values = dict(config.DEFAULTS)
        self.broken = None   # why the file can't be used, or None
        self.failed = False  # the last save failed (shown in the footer)
        self.scale = 1.0
        self.light = False
        self.focus = None
        self.focus_visible = False
        self.hover = None
        self.visible = False
        self.lay = None
        self.image = None    # (w, h, data) shown in WM_PAINT
        self._tracking = False
        self._pressed = None  # target under the mouse-down (a click needs down + up on it)

    # --- open / close ---
    def reload(self):
        try:
            self.values = config.read(self.folder)
            self.broken = None
        except config.SettingsFileError as e:
            self.values = dict(config.DEFAULTS)
            self.broken = str(e)
        self.failed = False

    def open(self):
        """Show (or bring to front) with the file's current values."""
        self.reload()
        self.light = gdi.light_theme()
        self.hover = None
        self.focus, self.focus_visible = None, False
        if not self.visible:
            pt = osd._cursor() or (0, 0)
            work, self.scale = osd._monitor_area(osd._monitor_at(*pt))
            self._place(work)
        self._redraw()
        self._dark_title()
        self.visible = True
        self._show()

    def close(self):
        if self.visible:
            self.visible = False
            self.hover = None
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def _frame(self, w, h):
        r = wt.RECT(0, 0, w, h)
        try:
            user32.AdjustWindowRectExForDpi.restype = wt.BOOL
            user32.AdjustWindowRectExForDpi.argtypes = (ctypes.POINTER(wt.RECT), wt.DWORD,
                                                         wt.BOOL, wt.DWORD, wt.UINT)
            ok = user32.AdjustWindowRectExForDpi(ctypes.byref(r), STYLE, False, EX_STYLE,
                                                 int(round(self.scale * 96)))
        except AttributeError:
            ok = user32.AdjustWindowRectEx(ctypes.byref(r), STYLE, False, EX_STYLE)
        if not ok:
            return w, h
        return r.right - r.left, r.bottom - r.top

    def _window_scale(self):
        try:
            user32.GetDpiForWindow.restype = wt.UINT
            user32.GetDpiForWindow.argtypes = (wt.HWND,)
            dpi = user32.GetDpiForWindow(self.hwnd)
        except AttributeError:
            dpi = 0
        return dpi / 96.0 if dpi else None

    def _place(self, work):
        """Centre on the work area (l, t, r, b) at self.scale."""
        for _ in range(2):  # a second pass if the window's real DPI differs
            self.lay = layout(self.values, self.scale, bool(self.broken))
            fw, fh = self._frame(self.lay.width, self.lay.height)
            left, top, right, bottom = work
            x = left + max(0, (right - left - fw) // 2)
            y = top + max(0, (bottom - top - fh) // 2)
            user32.SetWindowPos(self.hwnd, None, x, y, fw, fh, SWP_NOZORDER | SWP_NOACTIVATE)
            real = self._window_scale()
            if real is None or abs(real - self.scale) < 0.01:
                break
            self.scale = real

    def _resize(self):
        """Keep the top-left, fit the size to the layout (rows can appear)."""
        fw, fh = self._frame(self.lay.width, self.lay.height)
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        if (r.right - r.left, r.bottom - r.top) != (fw, fh):
            user32.SetWindowPos(self.hwnd, None, r.left, r.top, fw, fh,
                                SWP_NOZORDER | SWP_NOACTIVATE)

    def _dark_title(self):
        try:
            dwm = ctypes.WinDLL("dwmapi")
            on = ctypes.c_int(0 if self.light else 1)
            dwm.DwmSetWindowAttribute(wt.HWND(self.hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                      ctypes.byref(on), ctypes.sizeof(on))
        except (OSError, AttributeError):
            pass  # older Windows: light title bar

    def _show(self):
        user32.ShowWindow(self.hwnd, SW_RESTORE if user32.IsIconic(self.hwnd) else SW_SHOWNORMAL)
        menu.user32.SetForegroundWindow(self.hwnd)  # allowed: the user just used our menu

    # --- drawing ---
    def _redraw(self):
        old = (self.lay.width, self.lay.height) if self.lay else None
        self.lay = layout(self.values, self.scale, bool(self.broken))
        self.image = render(self.values, self.scale, self.light,
                            self.focus if self.focus_visible else None, self.hover,
                            bool(self.broken), self.failed)
        if old and old != (self.lay.width, self.lay.height):
            self._resize()
        self._invalidate()

    def _invalidate(self):
        user32.InvalidateRect(self.hwnd, None, False)

    def _paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            if hdc and self.image:
                w, h, data = self.image
                gdi.blit(hdc, 0, 0, w, h, data)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    # --- changes ---
    def activate(self, target):
        """A click (or key) on target."""
        if target is None:
            return
        name = target[0]
        if name == FILE:
            self.open_file()
            return
        if name == RESET:
            self.reset()
            return
        if self.broken:
            return
        change = change_for(self.values, target)
        if change is not None:
            self.save({change[0]: change[1]})

    def save(self, changes):
        try:
            done = config.save_changes(changes, self.folder)
        except config.SettingsFileError as e:
            log.warning("settings not saved: %s", e)
            self.broken = str(e)
            self.values = dict(config.DEFAULTS)
            self._redraw()
            return False
        except (OSError, ValueError) as e:
            log.warning("settings not saved (%s)", e)
            self.failed = True
            self._redraw()
            return False
        self.failed = False
        self.values.update(done)
        log.info("settings changed: %s", ", ".join(f"{k}={v}" for k, v in done.items()))
        self._redraw()
        self.on_change(done)
        return True

    def reset(self):
        try:
            defaults = config.reset(self.folder)
        except OSError as e:
            log.warning("settings not reset (%s)", e)
            self.failed = True
            self._redraw()
            return False
        log.info("settings file reset to the defaults (old file kept as %s)", config.BROKEN_NAME)
        self.broken, self.failed = None, False
        self.values = defaults
        self.focus = None
        self._redraw()
        self.on_change({k: defaults[k] for k in KEYS})
        return True

    # --- messages; None = not handled ---
    def _set_hover(self, target):
        if target != self.hover:
            self.hover = target
            self._redraw()

    def handle(self, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._paint()
            return 0
        if msg == WM_ERASEBKGND:
            return 1  # everything is painted from the bitmap
        if msg == WM_CLOSE:
            self.close()
            return 0
        if msg == WM_MOUSEMOVE:
            if not self._tracking:
                tme = menu.TRACKMOUSEEVENT(ctypes.sizeof(menu.TRACKMOUSEEVENT), menu.TME_LEAVE,
                                           self.hwnd, 0)
                self._tracking = bool(user32.TrackMouseEvent(ctypes.byref(tme)))
            if self.lay:
                self._set_hover(hit(self.lay, _signed(lparam), _signed(lparam >> 16)))
            return 0
        if msg == WM_MOUSELEAVE:
            self._tracking = False
            self._set_hover(None)
            return 0
        if msg == WM_SETCURSOR:
            if lparam & 0xFFFF == HTCLIENT and self.hover is not None:
                user32.SetCursor(user32.LoadCursorW(None, IDC_HAND))
                return 1
            return None
        if msg == WM_LBUTTONDOWN:
            self._pressed = hit(self.lay, _signed(lparam), _signed(lparam >> 16)) \
                if self.lay else None
            if self._pressed is not None:
                user32.SetCapture(self.hwnd)
            return 0
        if msg == WM_LBUTTONUP:
            pressed, self._pressed = self._pressed, None
            user32.ReleaseCapture()
            if self.lay:
                target = hit(self.lay, _signed(lparam), _signed(lparam >> 16))
                if target is None or target != pressed:
                    return 0  # only a press and release on the same item counts
                self.focus, self.focus_visible = target[0], False
                self.activate(target)
            return 0
        if msg == WM_KEYDOWN:
            return self._key(wparam)
        if msg == WM_DPICHANGED:
            self.scale = (wparam & 0xFFFF) / 96.0 or 1.0
            suggested = ctypes.cast(lparam, ctypes.POINTER(wt.RECT)).contents
            self.lay = layout(self.values, self.scale, bool(self.broken))
            fw, fh = self._frame(self.lay.width, self.lay.height)
            user32.SetWindowPos(self.hwnd, None, suggested.left, suggested.top, fw, fh,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            self._redraw()
            return 0
        if msg in (WM_SETTINGCHANGE, WM_THEMECHANGED):
            light = gdi.light_theme()
            if light != self.light:
                self.light = light
                self._dark_title()
                self._redraw()
            return None
        return None

    def _key(self, vk):
        if vk == VK_ESCAPE:
            self.close()
            return 0
        if not self.lay:
            return 0
        if vk in (VK_TAB, VK_UP, VK_DOWN):
            back = vk == VK_UP or (vk == VK_TAB and user32.GetKeyState(VK_SHIFT) < 0)
            self.focus = focus_step(self.lay, self.focus if self.focus_visible else None,
                                    -1 if back else 1)
            self.focus_visible = True
            self._redraw()
            return 0
        if self.focus is not None:
            self.focus_visible = True
            target = key_target(self.values, self.focus, vk)
            self.activate(target)
            if self.focus not in self.lay.order:  # e.g. Reset done: the banner is gone
                self.focus = None
            self._redraw()
        return 0
