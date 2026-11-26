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


def register():
    """Register all three keys on this thread. Returns (ok, last error)."""
    err = 0
    done = []
    for hk_id, (vk, _) in HOTKEYS.items():
        if user32.RegisterHotKey(None, hk_id, 0, vk):
            done.append(hk_id)
        else:
            err = ctypes.get_last_error()
    if len(done) != len(HOTKEYS):
        for hk_id in done:
            user32.UnregisterHotKey(None, hk_id)
        return False, err
    return True, 0


def unregister():
    for hk_id in HOTKEYS:
        user32.UnregisterHotKey(None, hk_id)


def run_loop(on_key, stop_event, max_keys=None, retry=RETRY_SECONDS):
    """Pump this thread's messages until `stop_event` (a Windows event handle)
    is set or WM_QUIT arrives. Registers the keys (retrying every `retry` s if
    another program holds them). Returns the number of keys handled."""
    registered, err = register()
    if registered:
        log.info("volume keys registered")
    else:
        log.warning("could not register volume keys (error %s); retrying every %s s", err, retry)
    handles = (wt.HANDLE * 1)(stop_event)
    msg = wt.MSG()
    seen = 0
    try:
        while True:
            timeout = INFINITE if registered else int(retry * 1000)
            r = user32.MsgWaitForMultipleObjects(1, handles, False, timeout, QS_ALLINPUT)
            if r == WAIT_OBJECT_0:
                return seen
            if not registered:
                registered, err = register()
                if registered:
                    log.info("volume keys registered")
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return seen
                if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
                    on_key(HOTKEYS[msg.wParam][1])
                    seen += 1
                    if max_keys and seen >= max_keys:
                        return seen
    finally:
        if registered:
            unregister()
