"""Small GDI / user32 helpers for the pop-up and the tray icon (ctypes only).

Everything draws into memory bitmaps; nothing here shows a window."""
import ctypes
import ctypes.wintypes as wt
import winreg

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _decl(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, args
    return f


_decl(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
_decl(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
_decl(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.c_void_p, wt.UINT,
      ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_decl(gdi32, "CreateBitmap", wt.HBITMAP, ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT,
      ctypes.c_void_p)
_decl(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_decl(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
_decl(gdi32, "CreateSolidBrush", wt.HBRUSH, wt.COLORREF)
_decl(gdi32, "CreatePen", wt.HPEN, ctypes.c_int, ctypes.c_int, wt.COLORREF)
_decl(gdi32, "Ellipse", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
_decl(gdi32, "RoundRect", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, ctypes.c_int)
_decl(gdi32, "Polygon", wt.BOOL, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.c_int)
_decl(gdi32, "SetPolyFillMode", ctypes.c_int, wt.HDC, ctypes.c_int)
_decl(gdi32, "SetBkMode", ctypes.c_int, wt.HDC, ctypes.c_int)
_decl(gdi32, "SetTextColor", wt.COLORREF, wt.HDC, wt.COLORREF)
_decl(gdi32, "CreateFontW", wt.HFONT, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
      wt.DWORD, wt.LPCWSTR)
_decl(gdi32, "GetTextFaceW", ctypes.c_int, wt.HDC, ctypes.c_int, wt.LPWSTR)
_decl(gdi32, "GdiFlush", wt.BOOL)
_decl(user32, "FillRect", ctypes.c_int, wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH)
_decl(user32, "DrawTextW", ctypes.c_int, wt.HDC, wt.LPCWSTR, ctypes.c_int,
      ctypes.POINTER(wt.RECT), wt.UINT)

PS_NULL = 5
TRANSPARENT = 1
WINDING = 2
DT_LEFT, DT_CENTER, DT_RIGHT = 0x0, 0x1, 0x2
DT_VCENTER, DT_SINGLELINE, DT_NOPREFIX, DT_END_ELLIPSIS = 0x4, 0x20, 0x800, 0x8000
ANTIALIASED_QUALITY = 4  # greyscale: no ClearType colour fringes on a layered window


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def colorref(hexcolor):
    """'#RRGGBB' -> COLORREF (0x00BBGGRR)."""
    r, g, b = int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)
    return r | g << 8 | b << 16


def dib(dc, w, h):
    """Top-down 32-bit DIB section. Returns (HBITMAP, pointer to its bits)."""
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbm or not bits.value:
        raise OSError("CreateDIBSection failed")
    return hbm, bits


class Bitmap:
    """A memory DC with a 32-bit top-down bitmap selected (all black at start)."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.dc = gdi32.CreateCompatibleDC(None)
        if not self.dc:
            raise OSError("CreateCompatibleDC failed")
        try:
            self.hbm, self.bits = dib(self.dc, w, h)
        except OSError:
            gdi32.DeleteDC(self.dc)
            raise
        self._old = gdi32.SelectObject(self.dc, self.hbm)
        gdi32.SetBkMode(self.dc, TRANSPARENT)
        gdi32.SetPolyFillMode(self.dc, WINDING)

    def close(self):
        if self.dc:
            gdi32.SelectObject(self.dc, self._old)
            gdi32.DeleteObject(self.hbm)
            gdi32.DeleteDC(self.dc)
            self.dc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _with_brush(self, color, draw):
        brush = gdi32.CreateSolidBrush(colorref(color))
        pen = gdi32.CreatePen(PS_NULL, 0, 0)
        ob, op = gdi32.SelectObject(self.dc, brush), gdi32.SelectObject(self.dc, pen)
        try:
            draw()
        finally:
            gdi32.SelectObject(self.dc, ob)
            gdi32.SelectObject(self.dc, op)
            gdi32.DeleteObject(brush)
            gdi32.DeleteObject(pen)

    def fill(self, x, y, w, h, color):
        if w <= 0 or h <= 0:
            return
        x0, y0 = int(round(x)), int(round(y))
        # at least 1 px: a thin line at a fractional position must not round away
        x1, y1 = max(x0 + 1, int(round(x + w))), max(y0 + 1, int(round(y + h)))
        brush = gdi32.CreateSolidBrush(colorref(color))
        r = wt.RECT(x0, y0, x1, y1)
        user32.FillRect(self.dc, ctypes.byref(r), brush)
        gdi32.DeleteObject(brush)

    def round_rect(self, x, y, w, h, radius, color):
        # null pen: RoundRect fills up to right-1 / bottom-1, so add one
        x0, y0 = int(round(x)), int(round(y))
        x1, y1 = int(round(x + w)) + 1, int(round(y + h)) + 1
        d = max(0, int(round(radius * 2)))
        self._with_brush(color, lambda: gdi32.RoundRect(self.dc, x0, y0, x1, y1, d, d))

    def ellipse(self, cx, cy, d, color):
        x0, y0 = int(round(cx - d / 2)), int(round(cy - d / 2))
        x1, y1 = int(round(cx + d / 2)) + 1, int(round(cy + d / 2)) + 1
        self._with_brush(color, lambda: gdi32.Ellipse(self.dc, x0, y0, x1, y1))

    def polygon(self, pts, color):
        arr = (wt.POINT * len(pts))(*[wt.POINT(int(round(px)), int(round(py))) for px, py in pts])
        self._with_brush(color, lambda: gdi32.Polygon(self.dc, arr, len(pts)))

    def text(self, s, x, y, w, h, size, color, face="Segoe UI", weight=400, align=DT_LEFT,
             width=0):
        """width: average character width in px (0 = normal, smaller = narrower)."""
        font = gdi32.CreateFontW(-int(round(size)), int(width), 0, 0, weight, 0, 0, 0, 1, 0, 0,
                                 ANTIALIASED_QUALITY, 0, face)
        old = gdi32.SelectObject(self.dc, font)
        gdi32.SetTextColor(self.dc, colorref(color))
        r = wt.RECT(int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))
        user32.DrawTextW(self.dc, s, -1, ctypes.byref(r),
                         align | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS)
        gdi32.SelectObject(self.dc, old)
        gdi32.DeleteObject(font)

    def face_exists(self, face):
        font = gdi32.CreateFontW(-12, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, face)
        old = gdi32.SelectObject(self.dc, font)
        buf = ctypes.create_unicode_buffer(64)
        gdi32.GetTextFaceW(self.dc, 64, buf)
        gdi32.SelectObject(self.dc, old)
        gdi32.DeleteObject(font)
        return buf.value.lower() == face.lower()

    def read(self):
        gdi32.GdiFlush()
        return bytearray(ctypes.string_at(self.bits, self.w * self.h * 4))

    def write(self, data):
        ctypes.memmove(self.bits, bytes(data), self.w * self.h * 4)


def downsample(raw, n, s):
    """Box-filter an n*n BGRA buffer by s. Returns a bytearray (n/s)^2 * 4."""
    m = n // s
    out = bytearray(m * m * 4)
    k = s * s
    row_bytes = n * 4
    for oy in range(m):
        for ox in range(m):
            b = g = r = a = 0
            for dy in range(s):
                i = (oy * s + dy) * row_bytes + ox * s * 4
                for _ in range(s):
                    b += raw[i]
                    g += raw[i + 1]
                    r += raw[i + 2]
                    a += raw[i + 3]
                    i += 4
            o = (oy * m + ox) * 4
            out[o] = b // k
            out[o + 1] = g // k
            out[o + 2] = r // k
            out[o + 3] = a // k
    return out


def light_theme():
    """True when Windows' taskbar / flyouts use the light theme."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "SystemUsesLightTheme")[0])
    except OSError:
        return False
