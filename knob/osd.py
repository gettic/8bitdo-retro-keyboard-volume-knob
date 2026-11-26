"""The volume pop-up ("look B"): a card bottom-centre with, per app, the name,
the percent and a bar (orange above 100 %, tick at 100 %).

The card is drawn into a memory bitmap and shown with UpdateLayeredWindow on a
window that never takes focus, lets clicks through and stays on top. It is
created hidden once and only shown on knob turns."""
import ctypes
import ctypes.wintypes as wt
import logging
import math

from . import display as view
from . import gdi

log = logging.getLogger("knob.osd")

user32, gdi32 = gdi.user32, gdi.gdi32
shcore = ctypes.WinDLL("shcore", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


gdi._decl(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
gdi._decl(user32, "MonitorFromPoint", wt.HMONITOR, wt.POINT, wt.DWORD)
gdi._decl(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFO))
gdi._decl(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
          ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
          ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
gdi._decl(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
gdi._decl(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, wt.UINT)
gdi._decl(shcore, "GetDpiForMonitor", ctypes.c_long, wt.HMONITOR, ctypes.c_int,
          ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))
gdi._decl(shell32, "SHQueryUserNotificationState", ctypes.c_long, ctypes.POINTER(ctypes.c_int))

MONITOR_DEFAULTTOPRIMARY = 1
QUNS_RUNNING_D3D_FULL_SCREEN = 3
ULW_ALPHA = 2
AC_SRC_ALPHA = 1
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

# window styles for the pop-up (used by ui.py when it creates the window)
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE

DARK = dict(bg="#2B2B2B", edge="#3D3D3D", fg="#FFFFFF", dim="#C5C5C5", track="#8A8A8A",
            fill="#4CC2FF", boost="#FFB340")
LIGHT = dict(bg="#F3F3F3", edge="#D9D9D9", fg="#1A1A1A", dim="#5C5C5C", track="#8F8F8F",
             fill="#005FB8", boost="#C25E00")
ICON_FACES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
GLYPH_VOLUME, GLYPH_MUTE = "", ""

# layout at 100 % scaling (96 dpi)
WIDTH = 300
PAD = 12           # top / bottom
BLOCK = 52         # one app: 28 header + 12 gap + 4 bar + 8
GAP = 6            # between apps
RADIUS = 8
ABOVE_TASKBAR = 48
MAX_ROWS = 4


def card_size(n_rows, scale):
    n = max(1, min(MAX_ROWS, n_rows))
    h = PAD * 2 + n * BLOCK + (n - 1) * GAP
    return int(round(WIDTH * scale)), int(round(h * scale))


def placement(work, w, h, scale):
    """Bottom-centre of the work area (taskbar excluded). work = (l, t, r, b)."""
    left, top, right, bottom = work
    x = left + (right - left - w) // 2
    y = bottom - h - int(round(ABOVE_TASKBAR * scale))
    return x, max(top, y)


def _corner_alpha(w, h, r):
    """{(x, y): alpha} for the pixels in the four rounded corners (4x4 samples)."""
    out = {}
    if r <= 0:
        return out
    for cy in range(int(math.ceil(r))):
        for cx in range(int(math.ceil(r))):
            inside = 0
            for sy in range(4):
                for sx in range(4):
                    dx = r - (cx + (sx + 0.5) / 4)
                    dy = r - (cy + (sy + 0.5) / 4)
                    if dx <= 0 or dy <= 0 or dx * dx + dy * dy <= r * r:
                        inside += 1
            a = inside * 255 // 16
            if a < 255:
                for x, y in ((cx, cy), (w - 1 - cx, cy), (cx, h - 1 - cy), (w - 1 - cx, h - 1 - cy)):
                    out[(x, y)] = a
    return out


def _icon_face(bm):
    for face in ICON_FACES:
        if bm.face_exists(face):
            return face
    return None


def render(rows, scale=1.0, light=False):
    """Draw the card. Returns (w, h, premultiplied BGRA bytes)."""
    rows = list(rows)[:MAX_ROWS]
    t = LIGHT if light else DARK
    w, h = card_size(len(rows), scale)
    s = scale
    with gdi.Bitmap(w, h) as bm:
        bm.fill(0, 0, w, h, t["edge"])
        bm.round_rect(1, 1, w - 3, h - 3, max(1.0, RADIUS * s - 1), t["bg"])
        face = _icon_face(bm)
        for i, row in enumerate(rows):
            top = (PAD + i * (BLOCK + GAP)) * s
            boost = row.level > 1.0 + 1e-9 and not row.muted
            if face:
                bm.text(GLYPH_MUTE if row.muted else GLYPH_VOLUME, 16 * s, top, 24 * s, 28 * s,
                        16 * s, t["fg"], face=face)
            name_x = (46 if face else 16) * s
            bm.text(row.name, name_x, top, w - name_x - 90 * s, 28 * s, 13 * s, t["fg"])
            label = "Muted" if row.muted else f"{view.percent(row.level)}%"
            colour = t["dim"] if row.muted else (t["boost"] if boost else t["fg"])
            bm.text(label, w - 96 * s, top, 80 * s, 28 * s, 15 * s, colour, weight=600,
                    align=gdi.DT_RIGHT)
            # bar
            bx, by, bw, bh = 16 * s, top + 40 * s, w - 32 * s, max(2.0, 4 * s)
            rad = bh / 2
            normal, extra, tick = view.bar_parts(row)
            bm.round_rect(bx, by, bw, bh, rad, t["track"])
            fill = t["track"] if row.muted else t["fill"]
            if normal > 0 and not row.muted:
                bm.round_rect(bx, by, max(bh, bw * normal), bh, rad, fill)
            if extra > 0 and not row.muted:
                bm.round_rect(bx + bw * normal - rad, by, bw * extra + rad, bh, rad, t["boost"])
            if tick is not None:
                tw = max(1.0, 2 * s)
                bm.fill(bx + bw * tick - tw / 2, by - 4 * s, tw, bh + 8 * s, t["dim"])
        data = bm.read()
    # opaque everywhere (GDI leaves the alpha byte 0), then round the corners
    data[3::4] = b"\xff" * (w * h)
    for (x, y), a in _corner_alpha(w, h, RADIUS * s).items():
        i = (y * w + x) * 4
        data[i] = data[i] * a // 255
        data[i + 1] = data[i + 1] * a // 255
        data[i + 2] = data[i + 2] * a // 255
        data[i + 3] = a
    return w, h, data


def fullscreen_app():
    """True while a Direct3D app runs exclusive full screen (no pop-up then)."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except OSError:
        return False
    return state.value == QUNS_RUNNING_D3D_FULL_SCREEN


def mouse_monitor():
    """(work area (l, t, r, b), scale) of the monitor with the mouse, else the main one."""
    pt = wt.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        pt = wt.POINT(0, 0)
    mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTOPRIMARY)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        raise OSError("no monitor")
    dx, dy = wt.UINT(96), wt.UINT(96)
    try:
        if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) != 0:
            dx.value = 96
    except OSError:
        dx.value = 96
    w = mi.rcWork
    return (w.left, w.top, w.right, w.bottom), dx.value / 96.0


class Osd:
    """The pop-up window. `hwnd` is created by ui.Ui (hidden, layered)."""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.visible = False

    def show(self, rows):
        """Draw and show (no focus change). False if nothing was shown."""
        if not rows or fullscreen_app():
            return False
        work, scale = mouse_monitor()
        w, h, data = render(rows, scale, gdi.light_theme())
        x, y = placement(work, w, h, scale)
        self._present(x, y, w, h, data)
        self.visible = True
        return True

    def redraw(self, rows):
        """Update an open pop-up (volume changed elsewhere); no-op when hidden."""
        if self.visible and rows:
            return self.show(rows)
        return False

    def _present(self, x, y, w, h, data):
        with gdi.Bitmap(w, h) as bm:
            bm.write(data)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wt.POINT(x, y)),
                                            ctypes.byref(wt.SIZE(w, h)), bm.dc,
                                            ctypes.byref(wt.POINT(0, 0)), 0,
                                            ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed ({ctypes.get_last_error()})")
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
