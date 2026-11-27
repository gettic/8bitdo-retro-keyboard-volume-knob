"""Windows Core Audio through ctypes (no extra packages).

Only per-app session volumes (ISimpleAudioVolume) are ever changed; the
device (master) volume is only read, never written.
All objects must be used on the thread that created the CoreAudio object.
"""
import ctypes
import ctypes.wintypes as wt
import os

ole32 = ctypes.OleDLL("ole32")
_ole32_void = ctypes.WinDLL("ole32")  # for functions that return nothing (no HRESULT check)
_ole32_void.CoUninitialize.restype = None
_ole32_void.CoUninitialize.argtypes = ()
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                ctypes.POINTER(wt.DWORD))
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.CloseHandle.restype = wt.BOOL

COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 23
E_RENDER = 0
E_MULTIMEDIA = 1
DEVICE_STATE_ACTIVE = 1
SESSION_STATE_EXPIRED = 2


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
IID_IAudioMeterInformation = GUID("C02216F6-8C67-4B5B-9D00-D008E73E0064")
IID_IAudioEndpointVolume = GUID("5CDF2C82-841E-4546-9722-0CF74078229A")

_protos = {}


def _com_call(obj, index, restype, *args):
    """Call method number `index` of a COM object (a raw c_void_p)."""
    argtypes = tuple(ctypes.c_void_p if (isinstance(a, ctypes._Pointer) or hasattr(a, "_obj"))
                     else type(a) for a in args)
    key = (restype, argtypes)
    proto = _protos.get(key)
    if proto is None:
        proto = _protos[key] = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtable[index])(obj, *args)


def _call(obj, index, *args):
    """HRESULT method: raises OSError on failure."""
    return _com_call(obj, index, ctypes.HRESULT, *args)


def _release(obj):
    if obj:
        _com_call(obj, 2, ctypes.c_ulong)


def _query(obj, iid):
    out = ctypes.c_void_p()
    _call(obj, 0, ctypes.byref(iid), ctypes.byref(out))
    return out


_ole32_void.CoTaskMemFree.restype = None
_ole32_void.CoTaskMemFree.argtypes = (ctypes.c_void_p,)


def _string(obj, index):
    """A COM method that hands out a CoTaskMem string (GetId, GetSessionInstanceIdentifier)."""
    p = ctypes.c_void_p()
    _call(obj, index, ctypes.byref(p))
    try:
        return ctypes.wstring_at(p.value) if p.value else ""
    finally:
        if p.value:
            _ole32_void.CoTaskMemFree(p)


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


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


kernel32.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.restype = wt.BOOL


def running_exes(names):
    """Which of `names` (lower-case exe names) have a running process. Read only."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wt.HANDLE(-1).value:
        return set()
    found = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if name in names:
                found.add(name)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return found


class Session:
    """One app's sound session on one output device."""

    def __init__(self, pid, exe, volume_obj, meter_obj):
        self.pid = pid
        self.exe = exe
        self._vol = volume_obj
        self._meter = meter_obj

    def volume(self):
        v = ctypes.c_float()
        _call(self._vol, 4, ctypes.byref(v))
        return v.value

    def set_volume(self, value):
        _call(self._vol, 3, ctypes.c_float(min(1.0, max(0.0, value))), ctypes.c_void_p())

    def muted(self):
        m = wt.BOOL()
        _call(self._vol, 6, ctypes.byref(m))
        return bool(m.value)

    def set_mute(self, muted):
        _call(self._vol, 5, wt.BOOL(bool(muted)), ctypes.c_void_p())

    def peak(self):
        if not self._meter:
            return 0.0
        p = ctypes.c_float()
        _call(self._meter, 3, ctypes.byref(p))
        return p.value

    def release(self):
        _release(self._vol)
        _release(self._meter)
        self._vol = self._meter = None


class CoreAudio:
    """Lists app sound sessions. Create and use on one thread.

    The device enumerator, each device's session manager and each session's
    `Session` object are kept between calls of sessions(): a refresh only
    lists what is there and asks about new sessions (much less cross-process
    COM work every 2 s). CoreAudio owns the Session objects: callers don't
    release them; gone ones are released here, the rest by close()."""

    def __init__(self):
        try:
            ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            self._uninit = True
        except OSError:  # already initialised differently on this thread
            self._uninit = False
        self._enum = None
        self._mgrs = {}    # device id -> IAudioSessionManager2
        self._known = {}   # session instance id -> Session (a wanted app)
        self._names = {}   # session instance id -> (pid, exe name) of any app seen

    def close(self):
        self.reset()
        if self._uninit:
            _ole32_void.CoUninitialize()
            self._uninit = False

    def reset(self):
        """Forget every kept object (after an error: the next sessions() starts clean)."""
        for s in self._known.values():
            s.release()
        for mgr in self._mgrs.values():
            _release(mgr)
        _release(self._enum)
        self._enum, self._mgrs, self._known, self._names = None, {}, {}, {}

    def _enumerator(self):
        enum = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                               ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))
        return enum

    def sessions(self, apps=None):
        """Sessions of the given exe names (None = all named apps), on every
        active output device. The objects stay valid until the next call
        drops them (session gone), reset() or close()."""
        try:
            return self._list(apps)
        except OSError:
            self.reset()  # something kept went stale (audio service restart...): once more
            return self._list(apps)

    def _list(self, apps):
        if self._enum is None:
            self._enum = self._enumerator()
        found, seen_dev, seen = [], set(), set()
        devices = ctypes.c_void_p()
        try:
            _call(self._enum, 3, ctypes.c_int(E_RENDER), wt.DWORD(DEVICE_STATE_ACTIVE),
                  ctypes.byref(devices))
            n_dev = wt.UINT()
            _call(devices, 3, ctypes.byref(n_dev))
            for d in range(n_dev.value):
                dev = ctypes.c_void_p()
                try:
                    _call(devices, 4, wt.UINT(d), ctypes.byref(dev))
                    dev_id = _string(dev, 5)  # IMMDevice::GetId
                    mgr = self._mgrs.get(dev_id)
                    if mgr is None:
                        mgr = ctypes.c_void_p()
                        _call(dev, 3, ctypes.byref(IID_IAudioSessionManager2),
                              wt.DWORD(CLSCTX_ALL), ctypes.c_void_p(), ctypes.byref(mgr))
                        self._mgrs[dev_id] = mgr
                    seen_dev.add(dev_id)
                    found.extend(self._device_sessions(mgr, apps, seen))
                except OSError:
                    pass
                finally:
                    _release(dev)
        finally:
            _release(devices)
        for dev_id in [k for k in self._mgrs if k not in seen_dev]:  # device unplugged
            _release(self._mgrs.pop(dev_id))
        for inst in [k for k in self._known if k not in seen]:      # session gone
            self._known.pop(inst).release()
        self._names = {k: v for k, v in self._names.items() if k in seen}
        return found

    def _device_sessions(self, mgr, apps, seen):
        out = []
        sessions = ctypes.c_void_p()
        try:
            _call(mgr, 5, ctypes.byref(sessions))
            n = ctypes.c_int()
            _call(sessions, 3, ctypes.byref(n))
            for i in range(n.value):
                ctl = ctypes.c_void_p()
                ctl2 = ctypes.c_void_p()
                try:
                    _call(sessions, 4, ctypes.c_int(i), ctypes.byref(ctl))
                    state = ctypes.c_int()
                    _call(ctl, 3, ctypes.byref(state))
                    if state.value == SESSION_STATE_EXPIRED:
                        continue
                    ctl2 = _query(ctl, IID_IAudioSessionControl2)
                    inst = _string(ctl2, 13)  # GetSessionInstanceIdentifier
                    seen.add(inst)
                    known = self._known.get(inst)
                    if known is not None:
                        if apps is None or known.exe in apps:
                            out.append(known)
                        continue
                    info = self._names.get(inst)
                    if info is None:
                        pid = wt.DWORD()
                        try:
                            _call(ctl2, 14, ctypes.byref(pid))
                        except OSError:  # multi-process session: no single pid
                            pid.value = 0
                        info = (pid.value, process_name(pid.value) if pid.value else "")
                        self._names[inst] = info
                    pid, name = info
                    if not name or (apps is not None and name not in apps):
                        continue
                    vol = _query(ctl, IID_ISimpleAudioVolume)
                    try:
                        meter = _query(ctl, IID_IAudioMeterInformation)
                    except OSError:
                        meter = None
                    s = self._known[inst] = Session(pid, name, vol, meter)
                    out.append(s)
                except OSError:
                    pass
                finally:
                    _release(ctl2)
                    _release(ctl)
        except OSError:
            pass
        finally:
            _release(sessions)
        return out

    def master_volume(self):
        """Default output device volume 0..1 (read only), or None."""
        enum = self._enumerator()
        dev = ctypes.c_void_p()
        ep = ctypes.c_void_p()
        try:
            _call(enum, 4, ctypes.c_int(E_RENDER), ctypes.c_int(E_MULTIMEDIA), ctypes.byref(dev))
            _call(dev, 3, ctypes.byref(IID_IAudioEndpointVolume), wt.DWORD(CLSCTX_ALL),
                  ctypes.c_void_p(), ctypes.byref(ep))
            v = ctypes.c_float()
            _call(ep, 9, ctypes.byref(v))  # GetMasterVolumeLevelScalar
            return v.value
        except OSError:
            return None
        finally:
            _release(ep)
            _release(dev)
            _release(enum)
