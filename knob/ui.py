"""The program's only windows: one hidden window (tray icon, menu, timers) and
the volume pop-up (osd.py). Both live on the main thread, which already pumps
messages in hotkeys.run_loop.

Tray icon: a knob in the Brave extension's look whose pointer shows the volume
(7 o'clock = 0 %, 5 o'clock = 100 %, orange towards 6 o'clock for the boost,
grey + red slash when muted, grey + pause sign when paused). Right-click menu:
Pause / Open log / Settings (settings_window.py) / Quit."""
import collections
import ctypes
import ctypes.wintypes as wt
import logging
import math
import os
import subprocess
import threading

from version import VERSION

from . import display as view
from . import gdi, menu, osd
from . import settings_window

log = logging.getLogger("knob.ui")

user32, gdi32 = gdi.user32, gdi.gdi32
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


_d = gdi._decl
_d(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_d(user32, "UnregisterClassW", wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
_d(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
   ctypes.c_void_p)
_d(user32, "DestroyWindow", wt.BOOL, wt.HWND)
_d(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
_d(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_d(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_d(user32, "SendMessageW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_d(user32, "RegisterWindowMessageW", wt.UINT, wt.LPCWSTR)
_d(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
_d(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
_d(user32, "CreatePopupMenu", wt.HMENU)
_d(user32, "AppendMenuW", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
_d(user32, "DestroyMenu", wt.BOOL, wt.HMENU)
_d(user32, "TrackPopupMenu", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
   ctypes.c_int, wt.HWND, ctypes.c_void_p)
_d(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
_d(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_d(user32, "CreateIconIndirect", wt.HICON, ctypes.POINTER(ICONINFO))
_d(user32, "DestroyIcon", wt.BOOL, wt.HICON)
_d(user32, "ChangeWindowMessageFilterEx", wt.BOOL, wt.HWND, wt.UINT, wt.DWORD, ctypes.c_void_p)
_d(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p)
_d(shell32, "Shell_NotifyIconW", wt.BOOL, wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_d(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)

CLASS_NAME = "8BitDoVolumeKnob.Window"
IDC_ARROW = 32512  # class cursor: without one the cursor over the menu keeps its old shape
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_HOTKEY = 0x2, 0x10, 0x113, 0x312
WM_NULL, WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0, 0x21, 3
WM_CONTEXTMENU, WM_RBUTTONUP = 0x7B, 0x205
WM_SETICON = 0x80
WM_APP = 0x8000
WM_APP_SHOW, WM_APP_TRAY, WM_APP_CMD = WM_APP + 1, WM_APP + 2, WM_APP + 3
OSD_TIMER = 1
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x80
NOTIFYICON_VERSION_4 = 4
TRAY_ID = 1
SM_CXSMICON = 49
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
MSGFLT_ALLOW = 1
CMD_PAUSE, CMD_LOG, CMD_SETTINGS, CMD_QUIT, CMD_MUTE = 1, 2, 3, 4, 5
ICON_CACHE = 64
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def _signed16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ------------------------------------------------------------ the knob icon
RED, ORANGE, GREY, BODY, WHITE = "#C82832", "#FF9F1A", "#8A8A8A", "#2D2D34", "#FFFFFF"
SUPERSAMPLE = 4


def knob_shapes(pct, muted, paused):
    """Shapes on a 32x32 grid: [(kind, args, colour)] (kind "ellipse"/"polygon").
    pct None = no reading yet: pointer at 12 o'clock, like the extension icon."""
    boost = pct is not None and pct > 100 and not muted and not paused
    ring = GREY if (muted or paused) else (ORANGE if boost else RED)
    shapes = [("ellipse", (16, 16, 31), ring), ("ellipse", (16, 16, 23), BODY)]
    if paused:
        shapes.append(("polygon", [(11.5, 10), (14.5, 10), (14.5, 22), (11.5, 22)], WHITE))
        shapes.append(("polygon", [(17.5, 10), (20.5, 10), (20.5, 22), (17.5, 22)], WHITE))
        return shapes
    a = math.radians(0.0 if pct is None else view.angle_for(pct / 100.0))
    dx, dy = math.sin(a), -math.cos(a)
    nx, ny = -dy, dx
    r0, r1, hw = 3.0, 10.5, 2.0
    pts = [(16 + dx * r0 + nx * hw, 16 + dy * r0 + ny * hw),
           (16 + dx * r1 + nx * hw, 16 + dy * r1 + ny * hw),
           (16 + dx * r1 - nx * hw, 16 + dy * r1 - ny * hw),
           (16 + dx * r0 - nx * hw, 16 + dy * r0 - ny * hw)]
    shapes.append(("polygon", pts, GREY if muted else (ORANGE if boost else WHITE)))
    if muted:
        shapes.append(("polygon", [(9, 22.5), (22.5, 9), (24, 10.5), (10.5, 24)], RED))
    return shapes


# 3x5 pixel digits for the number strip on small icons (16-20 px)
DIGITS_3X5 = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
              "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
              "6": "111100111101111", "7": "111001001001001", "8": "111101111101111",
              "9": "111101111001111"}
STRIP = "#111114"
STRIP_ALPHA = 235
TRAY_STYLES = ("knob_number", "knob")


def strip_height(size):
    """Height of the number strip along the bottom of the icon."""
    return 6 if size <= 20 else int(size * 0.36)


def _over(out, i, colour, alpha):
    """Paint colour (#RRGGBB) with alpha over straight-alpha BGRA pixel i."""
    if alpha <= 0:
        return
    r, g, b = int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)
    da = out[i + 3]
    oa = alpha + da * (255 - alpha) // 255
    if oa <= 0:
        return
    for k, src in ((0, b), (1, g), (2, r)):
        out[i + k] = min(255, (src * alpha + out[i + k] * da * (255 - alpha) // 255) // oa)
    out[i + 3] = oa


def _pixel_digits(out, size, text, top, colour):
    w, h = 3, 5
    total = len(text) * w + (len(text) - 1)
    x0 = (size - total) // 2
    for k, ch in enumerate(text):
        bits = DIGITS_3X5[ch]
        for yy in range(h):
            for xx in range(w):
                if bits[yy * w + xx] == "1":
                    x, y = x0 + k * (w + 1) + xx, top + yy
                    if 0 <= x < size and 0 <= y < size:
                        _over(out, (y * size + x) * 4, colour, 255)


def _font_digits(out, size, text, top, height, colour):
    """Bold narrow Segoe UI digits (24 px and up), anti-aliased."""
    with gdi.Bitmap(size, height) as bm:
        bm.text(text, 0, 0, size, height, height * 1.05, "#FFFFFF", weight=700,
                align=gdi.DT_CENTER, width=int(height * 0.42))
        cov = bm.read()
    for y in range(height):
        for x in range(size):
            a = cov[(y * size + x) * 4 + 1]
            if a and 0 <= top + y < size:
                _over(out, ((top + y) * size + x) * 4, colour, a)


def add_number(out, size, pct, muted):
    """The % on a dark strip along the bottom (tray_style "knob_number")."""
    sh = strip_height(size)
    top = size - sh
    for y in range(top, size):
        for x in range(size):
            _over(out, (y * size + x) * 4, STRIP, STRIP_ALPHA)
    colour = GREY if muted else (ORANGE if pct > 100 else WHITE)
    text = str(max(0, min(150, int(pct))))
    if size <= 20:
        _pixel_digits(out, size, text, top + (sh - 5) // 2, colour)
    else:
        _font_digits(out, size, text, top, sh, colour)


def icon_pixels(size, pct, muted=False, paused=False, number=False):
    """size x size straight-alpha BGRA (top-down) of the knob icon; with
    number=True the % sits on a strip along the bottom (not when paused or
    before the first reading)."""
    out = _knob_pixels(size, pct, muted, paused)
    if number and pct is not None and not paused:
        add_number(out, size, pct, muted)
    return out


def _knob_pixels(size, pct, muted, paused):
    n = size * SUPERSAMPLE
    u = n / 32.0
    with gdi.Bitmap(n, n) as colour, gdi.Bitmap(n, n) as mask:
        for kind, args, col in knob_shapes(pct, muted, paused):
            if kind == "ellipse":
                cx, cy, d = args
                colour.ellipse(cx * u, cy * u, d * u, col)
            else:
                colour.polygon([(x * u, y * u) for x, y in args], col)
        mask.ellipse(16 * u, 16 * u, 31 * u, WHITE)
        c = gdi.downsample(colour.read(), n, SUPERSAMPLE)
        m = gdi.downsample(mask.read(), n, SUPERSAMPLE)
    out = bytearray(size * size * 4)
    for i in range(0, len(out), 4):
        a = m[i]  # mask is white: any channel is the coverage
        if a:
            # colour was drawn on black, so it is premultiplied by the coverage
            out[i] = min(255, c[i] * 255 // a)
            out[i + 1] = min(255, c[i + 1] * 255 // a)
            out[i + 2] = min(255, c[i + 2] * 255 // a)
            out[i + 3] = a
    return out


def make_hicon(size, pixels):
    dc = gdi32.CreateCompatibleDC(None)
    try:
        hbm, bits = gdi.dib(dc, size, size)
    finally:
        gdi32.DeleteDC(dc)
    ctypes.memmove(bits, bytes(pixels), size * size * 4)
    zeros = ctypes.create_string_buffer(((size + 15) // 16) * 2 * size)
    hmask = gdi32.CreateBitmap(size, size, 1, 1, zeros)
    try:
        info = ICONINFO(True, 0, 0, hmask, hbm)
        icon = user32.CreateIconIndirect(ctypes.byref(info))
    finally:
        gdi32.DeleteObject(hbm)
        gdi32.DeleteObject(hmask)
    if not icon:
        raise OSError("CreateIconIndirect failed")
    return icon


class IconCache:
    """HICONs per (size, percent, muted, paused); the oldest are destroyed.
    style: "knob_number" (knob + % strip) or "knob"."""

    def __init__(self, limit=ICON_CACHE, maker=None, style="knob_number"):
        self.limit = limit
        number = style != "knob"
        self.maker = maker or (lambda size, key: make_hicon(
            size, icon_pixels(size, *key, number=number)))
        self._icons = collections.OrderedDict()
        self.destroy = user32.DestroyIcon

    def get(self, size, key):
        k = (size,) + tuple(key)
        if k in self._icons:
            self._icons.move_to_end(k)
            return self._icons[k]
        icon = self.maker(size, key)
        self._icons[k] = icon
        while len(self._icons) > self.limit:
            _, old = self._icons.popitem(last=False)
            self.destroy(old)
        return icon

    def clear(self):
        while self._icons:
            self.destroy(self._icons.popitem()[1])

    def __len__(self):
        return len(self._icons)


# ------------------------------------------------------------ tray icon
def shell_notify(message, data):
    """The real Shell_NotifyIconW (tests replace this)."""
    return bool(shell32.Shell_NotifyIconW(message, ctypes.byref(data)))


class Tray:
    """The notification-area icon; only redraws when the picture or the
    tooltip changes."""

    def __init__(self, hwnd, notify=None, icons=None, size=None):
        self.hwnd = hwnd
        self.notify = notify
        self.icons = icons if icons is not None else IconCache()  # (an empty cache is falsy)
        self.size = size
        self.added = False
        self.v4 = False
        self.key = None
        self.tip = None

    def _send(self, message, data):
        fn = self.notify or shell_notify  # looked up at call time (tests replace it)
        return fn(message, data)

    def _data(self, flags):
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        d.hWnd = self.hwnd
        d.uID = TRAY_ID
        d.uFlags = flags
        return d

    def icon_size(self):
        return self.size or max(16, user32.GetSystemMetrics(SM_CXSMICON) or 16)

    def _fill(self, d, key, tip):
        d.uFlags |= NIF_ICON | NIF_TIP | NIF_SHOWTIP
        d.hIcon = self.icons.get(self.icon_size(), key)
        d.szTip = tip[:view.TIP_MAX]

    def add(self, key, tip):
        d = self._data(NIF_MESSAGE)
        d.uCallbackMessage = WM_APP_TRAY
        self._fill(d, key, tip)
        self.key, self.tip = key, tip  # remembered for a later TaskbarCreated
        self.added = self._send(NIM_ADD, d)
        if self.added:
            v = self._data(0)
            v.uVersion = NOTIFYICON_VERSION_4
            self.v4 = self._send(NIM_SETVERSION, v)
        return self.added

    def update(self, key, tip):
        if not self.added:
            return self.add(key, tip)  # Explorer was not there at start
        if key == self.key and tip == self.tip:
            return False
        d = self._data(0)
        self._fill(d, key, tip)
        if self._send(NIM_MODIFY, d):
            self.key, self.tip = key, tip
            return True
        self.added = False
        return self.add(key, tip)

    def readd(self):
        """Explorer restarted (TaskbarCreated): the icon is gone; size may differ."""
        key, tip = self.key, self.tip
        self.added = False
        self.icons.clear()
        return self.add(key or (None, False, False), tip or view.tooltip([]))

    def remove(self):
        if self.added:
            self._send(NIM_DELETE, self._data(0))
            self.added = False
        self.icons.clear()


# ------------------------------------------------------------ opening files
def notepad():
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "notepad.exe")


def open_file(path):
    """Open a text file in Notepad (full path), or its folder if it doesn't exist."""
    if os.path.isfile(path):
        subprocess.Popen([notepad(), path], close_fds=True)
    else:
        folder = os.path.dirname(path)
        if os.path.isdir(folder):
            os.startfile(folder)


QUICK_LEVELS = (25, 50, 75, 100)
CMD_SET_BASE = 100  # command 125 = set 25 %, ... 200 = set 100 %


def quick_row(level=None, muted=False):
    """The 25/50/75/100 % row plus a speaker (mute) button. The % button
    closest to `level` (0-1.5) is marked, orange-marked (100) when boosted
    above 100 %; when muted the speaker button is marked instead."""
    mark = None
    if muted:
        mark = len(QUICK_LEVELS)
    elif level is not None:
        lv = min(level, 1.0) * 100
        mark = min(range(len(QUICK_LEVELS)), key=lambda k: abs(QUICK_LEVELS[k] - lv))
    buttons = [(CMD_SET_BASE + v, f"{v}%") for v in QUICK_LEVELS]
    buttons.append((CMD_MUTE, "Unmute" if muted else "Mute",
                    menu.ICON_MUTED if muted else menu.ICON_SPEAKER))
    return (menu.ROW, {"buttons": buttons, "mark": mark,
                       "boosted": not muted and level is not None and level > 1.0 + 1e-9},
            True)


def menu_items(paused, keys_ok=True, level=None, muted=False):
    """[(command id or None for a separator, text, enabled)]; the second line
    is the quick-volume row (menu.ROW) with the mute button."""
    head = f"{view.TITLE} {VERSION}"
    if not keys_ok and not paused:
        head += " (keys used elsewhere)"
    return [(0, head, False), quick_row(level, muted), (None, "", False),
            (CMD_PAUSE, "Resume" if paused else "Pause", True),
            (CMD_LOG, "Open log", True),
            (CMD_SETTINGS, "Settings", True),
            (None, "", False),
            (CMD_QUIT, "Quit", True)]


# ------------------------------------------------------------ the window
class Ui:
    """Create on the thread that runs hotkeys.run_loop; show() may be called
    from any thread."""

    def __init__(self, settings, on_quit, on_pause=None, on_key=None, log_path=None,
                 settings_path=None, notify=None, opener=None, dpi_aware=True,
                 on_settings=None):
        self.osd_on = bool(settings.get("osd", True))
        self.on_settings = on_settings  # changes saved in the settings window -> controller
        self.settings_win = None
        self._win_icons = []
        self.tray_on = bool(settings.get("tray_icon", True))
        self.osd_ms = int(float(settings.get("osd_seconds", 1.5)) * 1000)
        self.osd_monitor = settings.get("osd_monitor", "primary")
        self.tray_style = settings.get("tray_style", "knob_number")
        self.menu = None
        self.on_quit = on_quit
        self.on_pause = on_pause
        self.on_key = on_key
        self.log_path = log_path
        self.settings_path = settings_path
        self.notify = notify
        self.opener = opener or open_file
        self.dpi_aware = dpi_aware
        self.hwnd = None
        self.osd = None
        self.tray = None
        self.rows = []
        self.paused = False
        self.keys_ok = True
        self._lock = threading.Lock()
        self._pending = None  # (rows, popup) waiting for the window thread
        self._post_failed = False
        # looked up per call, so tests can wrap _wndproc
        self._proc = WNDPROC(lambda h, m, w, l: self._wndproc(h, m, w, l))
        self._taskbar_created = 0
        self._hinst = None

    # --- setup / teardown (window thread) ---
    def create(self):
        if self.dpi_aware:
            try:
                user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
                user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
                user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            except (AttributeError, OSError):
                pass  # older Windows: system DPI
        self._hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = self._hinst
        wc.lpszClassName = CLASS_NAME
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
        try:
            # a normal (never shown) top-level window: message-only windows
            # don't get the TaskbarCreated broadcast
            self.hwnd = user32.CreateWindowExW(osd.WS_EX_TOOLWINDOW, CLASS_NAME, view.TITLE,
                                               osd.WS_POPUP, 0, 0, 0, 0, None, None,
                                               self._hinst, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            if self._taskbar_created:
                user32.ChangeWindowMessageFilterEx(self.hwnd, self._taskbar_created,
                                                   MSGFLT_ALLOW, None)
            if self.osd_on:
                self._make_osd()
            if self.tray_on:
                h = user32.CreateWindowExW(menu.EX_STYLE, CLASS_NAME, view.TITLE, osd.WS_POPUP,
                                           0, 0, 0, 0, None, None, self._hinst, None)
                if not h:
                    raise OSError(f"CreateWindowExW (menu) failed ({ctypes.get_last_error()})")
                self.menu = menu.Menu(h, self._choose)
                self.tray = Tray(self.hwnd, notify=self.notify,
                                 icons=IconCache(style=self.tray_style))
                if not self.tray.add(self.icon_key(), self.tip()):
                    log.info("taskbar icon not added yet (taskbar not ready); waiting for it")
        except Exception:
            self.close()
            raise
        return self.hwnd

    def _make_osd(self):
        h = user32.CreateWindowExW(osd.EX_STYLE, CLASS_NAME, view.TITLE, osd.WS_POPUP,
                                   0, 0, 0, 0, None, None, self._hinst, None)
        if not h:
            raise OSError(f"CreateWindowExW (pop-up) failed ({ctypes.get_last_error()})")
        self.osd = osd.Osd(h, self.osd_monitor)

    def _drop_osd(self):
        if self.osd:
            user32.KillTimer(self.hwnd, OSD_TIMER)
            hwnd, self.osd = self.osd.hwnd, None
            user32.DestroyWindow(hwnd)

    def _make_settings_win(self):
        """The settings window, made hidden on first use."""
        sw = settings_window
        h = user32.CreateWindowExW(sw.EX_STYLE, CLASS_NAME, sw.TITLE, sw.STYLE,
                                   0, 0, 0, 0, None, None, self._hinst, None)
        if not h:
            raise OSError(f"CreateWindowExW (settings) failed ({ctypes.get_last_error()})")
        folder = os.path.dirname(self.settings_path) if self.settings_path else None
        self.settings_win = sw.SettingsWindow(
            h, folder, self.settings_changed,
            lambda: self.settings_path and self.opener(self.settings_path))
        try:  # the knob as the window's icon (title bar, taskbar, Alt+Tab)
            for kind, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
                icon = make_hicon(size, icon_pixels(size, None, False, False))
                self._win_icons.append(icon)
                user32.SendMessageW(h, WM_SETICON, kind, icon)
        except OSError:
            pass
        return self.settings_win

    def open_settings(self):
        if not self.settings_path:
            return
        win = self.settings_win or self._make_settings_win()
        win.open()

    def settings_changed(self, changes):
        """Saved in the settings window (window thread): apply what this
        window side shows now, pass the rest on (controller)."""
        if "osd_seconds" in changes:
            self.osd_ms = int(float(changes["osd_seconds"]) * 1000)
        if "osd_monitor" in changes:
            self.osd_monitor = changes["osd_monitor"]
            if self.osd:
                self.osd.monitor = self.osd_monitor
        if "osd" in changes and bool(changes["osd"]) != self.osd_on:
            self.osd_on = bool(changes["osd"])
            if self.osd_on and not self.osd:
                try:
                    self._make_osd()
                except OSError as e:
                    log.error("volume pop-up not available (%s)", e)
            elif not self.osd_on:
                self._drop_osd()
        if "tray_style" in changes and changes["tray_style"] != self.tray_style:
            self.tray_style = changes["tray_style"]
            if self.tray:
                old = self.tray.icons
                self.tray.icons = IconCache(style=self.tray_style)
                self.tray.key = None  # redraw with the new style
                self._update_tray()
                old.clear()
        if self.on_settings:
            self.on_settings(dict(changes))

    def close(self):
        if self.settings_win:
            hwnd, self.settings_win = self.settings_win.hwnd, None
            user32.DestroyWindow(hwnd)
        while self._win_icons:
            user32.DestroyIcon(self._win_icons.pop())
        if self.tray:
            try:
                self.tray.remove()
            except OSError:
                pass
            self.tray = None
        if self.osd:
            user32.DestroyWindow(self.osd.hwnd)
            self.osd = None
        if self.menu:
            hwnd, self.menu = self.menu.hwnd, None
            user32.DestroyWindow(hwnd)
        if self.hwnd:
            user32.KillTimer(self.hwnd, OSD_TIMER)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self._hinst:
            user32.UnregisterClassW(CLASS_NAME, self._hinst)
            self._hinst = None

    # --- any thread ---
    def show(self, rows, popup=False):
        """New rows from the audio thread: popup=True for a knob turn."""
        with self._lock:
            post = self._pending is None
            old_popup = self._pending[1] if self._pending else False
            self._pending = (list(rows), popup or old_popup)
        if post and self.hwnd:
            if not user32.PostMessageW(self.hwnd, WM_APP_SHOW, 0, 0):
                with self._lock:
                    self._pending = None  # so the next show() posts again
                if not self._post_failed:
                    self._post_failed = True
                    log.warning("display update could not be posted (%s)",
                                ctypes.get_last_error())

    # --- window thread ---
    def keys_status(self, registered, paused):
        """From hotkeys.run_loop: are the keys ours / paused?"""
        self.paused = paused
        self.keys_ok = registered or paused
        self._update_tray()

    def icon_key(self):
        return view.icon_key(self.rows, self.paused)

    def tip(self):
        return view.tooltip(self.rows, self.paused, self.keys_ok)

    def _update_tray(self):
        if self.tray:
            self.tray.update(self.icon_key(), self.tip())

    def apply_pending(self):
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        rows, popup = pending
        self.rows = rows
        self._update_tray()
        if not self.osd:
            return
        if popup and not self.paused:
            if self.osd.show(rows):
                user32.SetTimer(self.hwnd, OSD_TIMER, self.osd_ms, None)
        else:
            self.osd.redraw(rows)

    def command(self, cmd):
        if CMD_SET_BASE < cmd <= CMD_SET_BASE + 100:
            if self.on_key:  # controller.press: ("set", level) -> Controller.set_level
                self.on_key(("set", (cmd - CMD_SET_BASE) / 100.0))
        elif cmd == CMD_MUTE:
            if self.on_key:  # same as the knob's Mute key (playing app, else all)
                self.on_key("mute")
        elif cmd == CMD_PAUSE:
            if self.on_pause:
                self.on_pause(not self.paused)
        elif cmd == CMD_LOG and self.log_path:
            self.opener(self.log_path)
        elif cmd == CMD_SETTINGS:
            self.open_settings()
        elif cmd == CMD_QUIT:
            if self.osd:
                self.osd.hide()
            if self.menu:
                self.menu.close()
            if self.settings_win:
                self.settings_win.close()
            self.on_quit()

    def _choose(self, cmd):
        """Menu item chosen (the menu is already closed): run it from the queue."""
        user32.PostMessageW(self.hwnd, WM_APP_CMD, cmd, 0)

    def open_menu(self, anchor=None):
        """Our dark/light menu at the click point (screen coordinates)."""
        if not self.menu:
            return
        if self.menu.visible:
            self.menu.close()  # right-click on the icon while open: toggle closed
            return
        if self.menu.just_closed():
            return  # this click took the menu out of front and closed it: stay closed
        if anchor is None:
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            anchor = (pt.x, pt.y)
        level = self.rows[0].level if self.rows else None
        # "muted" only when everything on the card is: the Mute key then unmutes
        muted = bool(self.rows) and all(r.muted for r in self.rows)
        self.menu.open(menu_items(self.paused, self.keys_ok, level, muted), anchor)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if self.menu and hwnd == self.menu.hwnd:
                r = self.menu.handle(msg, wparam, lparam)
                if r is not None:
                    return r
            elif self.settings_win and hwnd == self.settings_win.hwnd:
                r = self.settings_win.handle(msg, wparam, lparam)
                if r is not None:
                    return r
            elif hwnd == self.hwnd:
                if msg == WM_APP_SHOW:
                    self.apply_pending()
                    return 0
                if msg == WM_APP_CMD:
                    self.command(wparam)
                    return 0
                if msg == WM_APP_TRAY:
                    event = lparam & 0xFFFF
                    if event == (WM_CONTEXTMENU if self.tray and self.tray.v4 else WM_RBUTTONUP):
                        anchor = None
                        if self.tray and self.tray.v4:  # version 4: wParam = click point
                            anchor = (_signed16(wparam), _signed16(wparam >> 16))
                        self.open_menu(anchor)
                    return 0
                if msg == WM_TIMER and wparam == OSD_TIMER:
                    user32.KillTimer(hwnd, OSD_TIMER)
                    if self.osd:
                        self.osd.hide()
                    return 0
                if msg == WM_HOTKEY:  # normally handled in run_loop before dispatch
                    if self.on_key and not self.paused:
                        from .hotkeys import HOTKEYS
                        if wparam in HOTKEYS:
                            self.on_key(HOTKEYS[wparam][1])
                    return 0
                if self._taskbar_created and msg == self._taskbar_created:
                    if self.tray:
                        self.tray.readd()
                    return 0
                if msg == WM_CLOSE:
                    return 0  # only Quit stops the program
            elif self.osd and hwnd == self.osd.hwnd and msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
        except Exception:
            log.exception("window message %#x failed", msg)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
