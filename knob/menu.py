"""The tray icon's right-click menu: our own small window drawn like the
volume pop-up, so it follows Windows' dark / light mode (the standard menu
stays light).

It is shown near the tray icon, takes the keyboard while open (arrows, Enter,
Space, Esc) and closes when an item is chosen, on Esc, on a click elsewhere
or when another app becomes active. Layout and keyboard logic are plain
functions so they can be tested without a window."""
import ctypes
import ctypes.wintypes as wt
import logging
import time

from . import gdi, osd

log = logging.getLogger("knob.menu")

user32 = gdi.user32


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND),
                ("dwHoverTime", wt.DWORD)]


gdi._decl(user32, "TrackMouseEvent", wt.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
gdi._decl(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
gdi._decl(user32, "GetForegroundWindow", wt.HWND)
gdi._decl(user32, "SetFocus", wt.HWND, wt.HWND)
gdi._decl(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
gdi._decl(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)

WM_TIMER = 0x113
WATCH_TIMER = 7
WATCH_MS = 150         # only while the menu is open
LEAVE_SECONDS = 3.0    # not in front and the mouse away this long: close
REOPEN_GUARD = 0.4     # a tray click that just closed the menu doesn't reopen it

WM_ACTIVATE, WM_KILLFOCUS, WM_KEYDOWN = 0x6, 0x8, 0x100
WM_MOUSEMOVE, WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE = 0x200, 0x202, 0x205, 0x2A3
WM_CANCELMODE = 0x1F
WA_INACTIVE = 0
VK_RETURN, VK_ESCAPE, VK_SPACE, VK_UP, VK_DOWN, VK_HOME, VK_END = (
    0x0D, 0x1B, 0x20, 0x26, 0x28, 0x24, 0x23)
TME_LEAVE = 0x2
SW_HIDE, SW_SHOW = 0, 5
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE = 0x1, 0x2

# window styles (ui.py creates the window): topmost tool window, layered for the
# rounded corners; it may take focus so the keyboard works
EX_STYLE = osd.WS_EX_LAYERED | osd.WS_EX_TOPMOST | osd.WS_EX_TOOLWINDOW

DARK = dict(bg="#2C2C2C", edge="#454545", fg="#FFFFFF", dim="#9A9A9A", hover="#3D3D3D",
            sep="#454545", accent="#4CC2FF")
LIGHT = dict(bg="#F9F9F9", edge="#D5D5D5", fg="#1A1A1A", dim="#6E6E6E", hover="#E8E8E8",
             sep="#E0E0E0", accent="#005FB8")

# layout at 100 % scaling
WIDTH = 230
PAD = 4
HEIGHTS = {"head": 32, "sep": 9, "item": 32}
RADIUS = 8
FONT = {"head": 11, "item": 13}


def kind_of(item):
    cmd, _text, enabled = item
    if cmd is None:
        return "sep"
    return "item" if enabled else "head"


def layout(items, scale=1.0):
    """(width, height, [(top, height)] per item) in pixels."""
    s = scale
    rows = []
    y = PAD * s
    for item in items:
        h = HEIGHTS[kind_of(item)] * s
        rows.append((y, h))
        y += h
    return int(round(WIDTH * s)), int(round(y + PAD * s)), rows


def hit(items, rows, width, x, y):
    """Index of the choosable item at (x, y) in the window, or None."""
    if not 0 <= x < width:
        return None
    for i, (top, h) in enumerate(rows):
        if top <= y < top + h:
            return i if kind_of(items[i]) == "item" else None
    return None


def step(items, current, direction):
    """Next choosable item going up (-1) or down (+1), wrapping round."""
    choosable = [i for i, it in enumerate(items) if kind_of(it) == "item"]
    if not choosable:
        return None
    if current not in choosable:
        return choosable[0] if direction > 0 else choosable[-1]
    k = choosable.index(current)
    return choosable[(k + direction) % len(choosable)]


def position(anchor, w, h, work):
    """Top-left so the menu sits above / beside the click point, inside the
    work area (l, t, r, b): opens up-left of the point like Windows' menus."""
    ax, ay = anchor
    left, top, right, bottom = work
    x = ax - w if ax - w >= left else ax
    x = max(left, min(x, right - w))
    y = ay - h
    if y < top:
        y = ay
    y = max(top, min(y, bottom - h))
    return int(x), int(y)


def render(items, hover=None, scale=1.0, light=False):
    """Draw the menu. Returns (w, h, premultiplied BGRA bytes)."""
    t = LIGHT if light else DARK
    s = scale
    w, h, rows = layout(items, s)
    with gdi.Bitmap(w, h) as bm:
        bm.fill(0, 0, w, h, t["edge"])
        bm.round_rect(1, 1, w - 3, h - 3, max(1.0, (RADIUS - 1) * s), t["bg"])
        for i, (item, (top, rh)) in enumerate(zip(items, rows)):
            kind = kind_of(item)
            if kind == "sep":
                bm.fill(12 * s, int(top + rh / 2), w - 24 * s, max(1, int(s)), t["sep"])
                continue
            if i == hover and kind == "item":
                bm.round_rect(4 * s, top + 1 * s, w - 8 * s, rh - 2 * s, 4 * s, t["hover"])
                bm.round_rect(4 * s, top + 9 * s, 3 * s, rh - 18 * s, 1.5 * s, t["accent"])
            bm.text(item[1], 16 * s, top, w - 32 * s, rh, FONT[kind] * s,
                    t["dim"] if kind == "head" else t["fg"])
        data = bm.read()
    data[3::4] = b"\xff" * (w * h)
    for (x, y), a in osd._corner_alpha(w, h, RADIUS * s).items():
        i = (y * w + x) * 4
        data[i] = data[i] * a // 255
        data[i + 1] = data[i + 1] * a // 255
        data[i + 2] = data[i + 2] * a // 255
        data[i + 3] = a
    return w, h, data


def _signed(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Menu:
    """The menu window (`hwnd` made hidden by ui.Ui). on_choose(cmd) is called
    after the menu has closed."""

    def __init__(self, hwnd, on_choose):
        self.hwnd = hwnd
        self.on_choose = on_choose
        self.items = []
        self.rows = []
        self.width = 0
        self.scale = 1.0
        self.light = False
        self.hover = None
        self.visible = False
        self._tracking = False
        self._origin = (0, 0)
        self._size = (0, 0)
        self.clock = time.monotonic
        self.closed_at = None       # when the menu last closed because it lost the front
        self._front_at_open = None  # the window in front right after opening
        self._away_since = None

    # --- open / close ---
    def open(self, items, anchor):
        """Show at anchor (screen point of the click) and take the keyboard."""
        self.items = list(items)
        work, self.scale = osd._monitor_area(osd._monitor_at(*anchor))
        self.light = gdi.light_theme()
        self.hover = None
        self.width, h, self.rows = layout(self.items, self.scale)
        self._size = (self.width, h)
        self._origin = position(anchor, self.width, h, work)
        self._draw()
        self.visible = True
        self._tracking = False
        self.closed_at = None
        self._activate()
        # fallback for when Windows refuses to bring the menu to the front
        # (then Esc and WM_ACTIVATE never reach it): see watch()
        self._front_at_open = self._foreground()
        self._away_since = None
        self._start_watch()

    def close(self, lost_front=False):
        if self.visible:
            self.visible = False
            self.hover = None
            self._stop_watch()
            user32.ShowWindow(self.hwnd, SW_HIDE)
            if lost_front:
                self.closed_at = self.clock()

    def just_closed(self):
        """True right after the menu closed because the user clicked elsewhere
        (e.g. on the tray icon: that click must not open it again)."""
        return self.closed_at is not None and self.clock() - self.closed_at < REOPEN_GUARD

    # --- the fallback watch (documented calls only, every 150 ms while open) ---
    def _foreground(self):
        return user32.GetForegroundWindow()

    def _cursor(self):
        return osd._cursor()

    def _start_watch(self):
        user32.SetTimer(self.hwnd, WATCH_TIMER, WATCH_MS, None)

    def _stop_watch(self):
        user32.KillTimer(self.hwnd, WATCH_TIMER)

    def _inside(self, pt):
        if pt is None:
            return False
        x, y = pt
        ox, oy = self._origin
        return ox <= x < ox + self._size[0] and oy <= y < oy + self._size[1]

    def watch(self):
        """Close a menu that isn't in front when another window comes to the
        front (a click elsewhere) or the mouse stays away LEAVE_SECONDS."""
        if not self.visible:
            return
        front = self._foreground()
        if front == self.hwnd:
            self._away_since = None
            return  # normal case: Esc / WM_ACTIVATE do the work
        if front != self._front_at_open:
            self.close(lost_front=True)
            return
        if self._inside(self._cursor()):
            self._away_since = None
        elif self._away_since is None:
            self._away_since = self.clock()
        elif self.clock() - self._away_since >= LEAVE_SECONDS:
            self.close()

    def choose(self, index):
        if index is None or kind_of(self.items[index]) != "item":
            return
        cmd = self.items[index][0]
        self.close()
        self.on_choose(cmd)

    # --- drawing (tests replace _present / _activate) ---
    def _draw(self):
        w, h, data = render(self.items, self.hover, self.scale, self.light)
        self._present(self._origin[0], self._origin[1], w, h, data)

    def _present(self, x, y, w, h, data):
        with gdi.Bitmap(w, h) as bm:
            bm.write(data)
            blend = osd.BLENDFUNCTION(0, 0, 255, osd.AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wt.POINT(x, y)),
                                            ctypes.byref(wt.SIZE(w, h)), bm.dc,
                                            ctypes.byref(wt.POINT(0, 0)), 0,
                                            ctypes.byref(blend), osd.ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed ({ctypes.get_last_error()})")

    def _activate(self):
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.SetForegroundWindow(self.hwnd)  # allowed: the user just clicked our icon
        user32.SetFocus(self.hwnd)

    def _set_hover(self, index):
        if index != self.hover:
            self.hover = index
            if self.visible:
                self._draw()

    def _track_leave(self):
        if not self._tracking:
            tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, self.hwnd, 0)
            self._tracking = bool(user32.TrackMouseEvent(ctypes.byref(tme)))

    # --- messages for the menu window; None = not handled ---
    def handle(self, msg, wparam, lparam):
        if not self.visible:
            return None
        if msg == WM_TIMER and wparam == WATCH_TIMER:
            self.watch()
            return 0
        if msg == WM_ACTIVATE:
            if wparam & 0xFFFF == WA_INACTIVE:
                self.close(lost_front=True)  # click elsewhere or another app became active
            return 0
        if msg in (WM_KILLFOCUS, WM_CANCELMODE):
            self.close(lost_front=True)
            return 0
        if msg == WM_MOUSEMOVE:
            self._track_leave()
            self._set_hover(hit(self.items, self.rows, self.width,
                                _signed(lparam), _signed(lparam >> 16)))
            return 0
        if msg == WM_MOUSELEAVE:
            self._tracking = False
            self._set_hover(None)
            return 0
        if msg in (WM_LBUTTONUP, WM_RBUTTONUP):
            self.choose(hit(self.items, self.rows, self.width,
                            _signed(lparam), _signed(lparam >> 16)))
            return 0
        if msg == WM_KEYDOWN:
            if wparam == VK_ESCAPE:
                self.close()
            elif wparam in (VK_UP, VK_DOWN):
                self._set_hover(step(self.items, self.hover, -1 if wparam == VK_UP else 1))
            elif wparam == VK_HOME:
                self._set_hover(step(self.items, None, 1))
            elif wparam == VK_END:
                self._set_hover(step(self.items, None, -1))
            elif wparam in (VK_RETURN, VK_SPACE):
                self.choose(self.hover)
            return 0
        return None
