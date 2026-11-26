"""Catches the Volume Up / Down / Mute keys with RegisterHotKey.

A registered hotkey is consumed: Windows does not see the key, so its own
master-volume change and volume pop-up do not happen. No keyboard hook is
used (it would see every key typed)."""
import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger("knob.hotkeys")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
HOTKEYS = {1: (VK_VOLUME_UP, "up"), 2: (VK_VOLUME_DOWN, "down"), 3: (VK_VOLUME_MUTE, "mute")}
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x1
QS_ALLINPUT = 0x04FF
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
RETRY_SECONDS = 30

user32.RegisterHotKey.argtypes = (wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = (wt.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wt.BOOL
user32.MsgWaitForMultipleObjects.argtypes = (wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL,
                                             wt.DWORD, wt.DWORD)
user32.MsgWaitForMultipleObjects.restype = wt.DWORD
user32.PeekMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
user32.PeekMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.PostThreadMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wt.MSG),)
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wt.MSG),)
user32.DispatchMessageW.restype = ctypes.c_ssize_t


def register(hwnd=None):
    """Register all three keys on this thread (or for window `hwnd`, which must
    belong to this thread). Returns (ok, last error)."""
    err = 0
    done = []
    for hk_id, (vk, _) in HOTKEYS.items():
        if user32.RegisterHotKey(hwnd, hk_id, 0, vk):
            done.append(hk_id)
        else:
            err = ctypes.get_last_error()
    if len(done) != len(HOTKEYS):
        for hk_id in done:
            user32.UnregisterHotKey(hwnd, hk_id)
        return False, err
    return True, 0


def unregister(hwnd=None):
    for hk_id in HOTKEYS:
        user32.UnregisterHotKey(hwnd, hk_id)


class _LoopState:
    """Keys of the running loop; pause()/resume() change it from the same
    thread (the tray menu runs inside the loop)."""

    def __init__(self, hwnd, on_status):
        self.hwnd = hwnd
        self.on_status = on_status
        self.registered = False
        self.paused = False

    def status(self):
        if self.on_status:
            try:
                self.on_status(self.registered, self.paused)
            except Exception:  # a tray icon problem never stops the knob
                log.exception("key status display failed")

    def try_register(self, quiet=False):
        ok, err = register(self.hwnd)
        self.registered = ok
        if ok:
            log.info("volume keys registered")
        elif not quiet:
            log.warning("could not register volume keys (error %s); retrying every %s s",
                        err, RETRY_SECONDS)
        self.status()
        return ok


_current = None


def pause():
    """Release the keys so Windows handles them again (tray menu "Pause")."""
    st = _current
    if st is None or st.paused:
        return False
    st.paused = True
    if st.registered:
        unregister(st.hwnd)
        st.registered = False
    log.info("paused: volume keys released")
    st.status()
    return True


def resume():
    st = _current
    if st is None or not st.paused:
        return False
    st.paused = False
    log.info("resumed")
    st.try_register()
    return True


def is_paused():
    return bool(_current and _current.paused)


def run_loop(on_key, stop_event, max_keys=None, retry=RETRY_SECONDS, hwnd=None,
             on_status=None):
    """Pump this thread's messages until `stop_event` (a Windows event handle)
    is set or WM_QUIT arrives. Registers the keys (retrying every `retry` s if
    another program holds them), for window `hwnd` if given (its window
    procedure then also gets WM_HOTKEY while a menu is open). Window messages
    are dispatched. on_status(registered, paused) is told about changes.
    Returns the number of keys handled here."""
    global _current
    st = _LoopState(hwnd, on_status)
    _current = st
    st.try_register()
    handles = (wt.HANDLE * 1)(stop_event)
    msg = wt.MSG()
    seen = 0
    try:
        while True:
            waiting = not st.registered and not st.paused
            timeout = int(retry * 1000) if waiting else INFINITE
            r = user32.MsgWaitForMultipleObjects(1, handles, False, timeout, QS_ALLINPUT)
            if r == WAIT_OBJECT_0:
                return seen
            if not st.registered and not st.paused:
                st.try_register(quiet=True)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return seen
                if msg.message == WM_HOTKEY:
                    if msg.wParam in HOTKEYS and not st.paused:
                        on_key(HOTKEYS[msg.wParam][1])
                        seen += 1
                        if max_keys and seen >= max_keys:
                            return seen
                    continue
                if msg.hWnd:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _current = None
        if st.registered:
            unregister(hwnd)
