"""Volume knob -> app volume.

The keyboard's volume knob sends the normal Windows "Volume Up / Down / Mute"
keys. With the Arctis Nova Pro base station those keys change nothing, so this
program catches the three keys and changes the volume of the apps listed in
APPS instead (each app's own slider in the Windows Volume Mixer).

Only uses Python's built-in ctypes: no extra downloads.
Run hidden with:  pythonw volume_knob.py
Stop with:        "Stop volume knob.cmd"
Self-test:        python volume_knob.py --test
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

# ---- Settings ---------------------------------------------------------------
# Programs whose volume the knob controls (lower case .exe names).
# Browsers are here for YouTube: the whole browser's sound changes with it.
APPS = {"spotify.exe", "brave.exe", "firefox.exe", "chrome.exe", "msedge.exe", "opera.exe"}
STEP = 0.04  # how much one click of the knob changes the volume (0.04 = 4 %)
# -----------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(HERE, "volume_knob.pid")

ole32 = ctypes.OleDLL("ole32")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(self))


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = GUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = GUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISimpleAudioVolume = GUID("87CE5498-68D6-44E5-9215-6DA47EF883D8")
CLSCTX_ALL = 23
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1


def com_call(obj, index, restype, *args):
    """Call method number `index` of a COM object (a raw c_void_p)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    argtypes = [ctypes.c_void_p] + [type(a) if not isinstance(a, ctypes._Pointer)
                                    and not hasattr(a, "_obj") else ctypes.c_void_p
                                    for a in args]
    proto = ctypes.WINFUNCTYPE(restype, *argtypes)
    return proto(vtable[index])(obj, *args)


def call(obj, index, *args):
    hr = com_call(obj, index, ctypes.HRESULT, *args)
    return hr


def release(obj):
    if obj:
        com_call(obj, 2, ctypes.c_ulong)


def query(obj, iid):
    out = ctypes.c_void_p()
    call(obj, 0, ctypes.byref(iid), ctypes.byref(out))
    return out


def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def app_volumes():
    """Yield (exe name, ISimpleAudioVolume) for every sound session of an app in APPS,
    on every active output device. Caller must release() each volume object."""
    enum = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                           ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))
    devices = ctypes.c_void_p()
    call(enum, 3, ctypes.c_int(E_RENDER), wt.DWORD(DEVICE_STATE_ACTIVE), ctypes.byref(devices))
    n_dev = wt.UINT()
    call(devices, 3, ctypes.byref(n_dev))
    for d in range(n_dev.value):
        dev = ctypes.c_void_p()
        call(devices, 4, wt.UINT(d), ctypes.byref(dev))
        mgr = ctypes.c_void_p()
        try:
            call(dev, 3, ctypes.byref(IID_IAudioSessionManager2), wt.DWORD(CLSCTX_ALL),
                 ctypes.c_void_p(), ctypes.byref(mgr))
        except OSError:
            release(dev)
            continue
        sessions = ctypes.c_void_p()
        call(mgr, 5, ctypes.byref(sessions))
        n = ctypes.c_int()
        call(sessions, 3, ctypes.byref(n))
        for i in range(n.value):
            ctl = ctypes.c_void_p()
            call(sessions, 4, ctypes.c_int(i), ctypes.byref(ctl))
            ctl2 = query(ctl, IID_IAudioSessionControl2)
            pid = wt.DWORD()
            try:
                call(ctl2, 14, ctypes.byref(pid))
            except OSError:
                pid.value = 0
            name = process_name(pid.value) if pid.value else ""
            if name in APPS:
                yield name, query(ctl, IID_ISimpleAudioVolume)
            release(ctl2)
            release(ctl)
        release(sessions)
        release(mgr)
        release(dev)
    release(devices)
    release(enum)


def get_volume(vol):
    v = ctypes.c_float()
    call(vol, 4, ctypes.byref(v))
    return v.value


def set_volume(vol, value):
    call(vol, 3, ctypes.c_float(min(1.0, max(0.0, value))), ctypes.c_void_p())


def get_mute(vol):
    m = wt.BOOL()
    call(vol, 6, ctypes.byref(m))
    return bool(m.value)


def set_mute(vol, muted):
    call(vol, 5, wt.BOOL(muted), ctypes.c_void_p())


def change(action):
    found = list(app_volumes())
    if action == "mute":
        # If any of the apps is still playing, mute all; if all are muted, unmute all.
        mute_all = not all(get_mute(v) for _, v in found)
    for _, vol in found:
        try:
            if action == "mute":
                set_mute(vol, mute_all)
            else:
                if get_mute(vol):
                    set_mute(vol, False)
                set_volume(vol, get_volume(vol) + (STEP if action == "up" else -STEP))
        except OSError:
            pass
        release(vol)


VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
HOTKEYS = {1: (VK_VOLUME_UP, "up"), 2: (VK_VOLUME_DOWN, "down"), 3: (VK_VOLUME_MUTE, "mute")}
WM_HOTKEY = 0x0312


def register_keys():
    ok = True
    for hk_id, (vk, _) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hk_id, 0, vk):
            ok = False
    return ok


def run(on_key=change, max_keys=None):
    if not register_keys():
        return False
    msg = wt.MSG()
    seen = 0
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
            on_key(HOTKEYS[msg.wParam][1])
            seen += 1
            if max_keys and seen >= max_keys:
                break
    for hk_id in HOTKEYS:
        user32.UnregisterHotKey(None, hk_id)
    return True


def self_test():
    """Checks everything without changing anything you can hear."""
    ole32.CoInitialize(None)
    print("Apps with sound open right now:")
    found = list(app_volumes())
    for name, vol in found:
        before = get_volume(vol)
        set_volume(vol, before - 0.001 if before > 0.01 else before + 0.001)
        moved = abs(get_volume(vol) - before) > 0.0005
        set_volume(vol, before)
        print(f"  {name}: volume {before:.0%}, muted={get_mute(vol)}, can change={moved}, "
              f"restored={abs(get_volume(vol) - before) < 0.0005}")
        release(vol)
    if not found:
        print("  (none)")
    # Key test: catch fake Volume Up then Volume Down presses (they cancel out
    # even if catching failed), without changing any app.
    got = []
    import threading

    def press():
        import time
        time.sleep(0.3)
        for vk in (VK_VOLUME_UP, VK_VOLUME_DOWN):
            user32.keybd_event(vk, 0, 1, 0)
            user32.keybd_event(vk, 0, 3, 0)
            time.sleep(0.1)
        time.sleep(0.5)
        user32.PostThreadMessageW(main_tid, 0x0012, 0, 0)  # WM_QUIT if keys missed
    main_tid = kernel32.GetCurrentThreadId()
    threading.Thread(target=press, daemon=True).start()
    registered = run(on_key=got.append, max_keys=2)
    print(f"Keys taken over: {registered}, keys caught: {got}")


def main():
    if "--test" in sys.argv:
        self_test()
        return
    # Only one copy at a time.
    kernel32.CreateMutexW(None, False, "Local\\GetticVolumeKnob")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    ole32.CoInitialize(None)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    run()


if __name__ == "__main__":
    main()
