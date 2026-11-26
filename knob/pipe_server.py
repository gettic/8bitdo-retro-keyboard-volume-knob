"""Named pipe server for the Brave native-messaging host (see PROTOCOL.md).

Overlapped I/O, first-instance only, current-user-only access, local only.
Also a small blocking client used by tests and --self-test."""
import ctypes
import ctypes.wintypes as wt
import itertools
import logging
import threading
import time

from . import protocol

log = logging.getLogger("knob.pipe")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_MORE_DATA = 234
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0x0
PIPE_REJECT_REMOTE_CLIENTS = 0x8
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
TOKEN_QUERY = 0x8
TOKEN_USER_CLASS = 1
BUF_SIZE = 64 * 1024


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


LPOV = ctypes.POINTER(OVERLAPPED)
CreateNamedPipeW = _sig(kernel32, "CreateNamedPipeW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                        wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p)
ConnectNamedPipe = _sig(kernel32, "ConnectNamedPipe", wt.BOOL, wt.HANDLE, LPOV)
DisconnectNamedPipe = _sig(kernel32, "DisconnectNamedPipe", wt.BOOL, wt.HANDLE)
CreateEventW = _sig(kernel32, "CreateEventW", wt.HANDLE, ctypes.c_void_p, wt.BOOL, wt.BOOL,
                    wt.LPCWSTR)
SetEvent = _sig(kernel32, "SetEvent", wt.BOOL, wt.HANDLE)
ResetEvent = _sig(kernel32, "ResetEvent", wt.BOOL, wt.HANDLE)
CloseHandle = _sig(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
WaitForMultipleObjects = _sig(kernel32, "WaitForMultipleObjects", wt.DWORD, wt.DWORD,
                              ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD)
WaitForSingleObject = _sig(kernel32, "WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
ReadFile = _sig(kernel32, "ReadFile", wt.BOOL, wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                ctypes.POINTER(wt.DWORD), LPOV)
WriteFile = _sig(kernel32, "WriteFile", wt.BOOL, wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                 ctypes.POINTER(wt.DWORD), LPOV)
GetOverlappedResult = _sig(kernel32, "GetOverlappedResult", wt.BOOL, wt.HANDLE, LPOV,
                           ctypes.POINTER(wt.DWORD), wt.BOOL)
CancelIoEx = _sig(kernel32, "CancelIoEx", wt.BOOL, wt.HANDLE, LPOV)
CreateFileW = _sig(kernel32, "CreateFileW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                   ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE)
PeekNamedPipe = _sig(kernel32, "PeekNamedPipe", wt.BOOL, wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                     ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                     ctypes.POINTER(wt.DWORD))
WaitNamedPipeW = _sig(kernel32, "WaitNamedPipeW", wt.BOOL, wt.LPCWSTR, wt.DWORD)
GetCurrentProcess = _sig(kernel32, "GetCurrentProcess", wt.HANDLE)
LocalFree = _sig(kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
OpenProcessToken = _sig(advapi32, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD,
                        ctypes.POINTER(wt.HANDLE))
GetTokenInformation = _sig(advapi32, "GetTokenInformation", wt.BOOL, wt.HANDLE, ctypes.c_int,
                           ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
ConvertSidToStringSidW = _sig(advapi32, "ConvertSidToStringSidW", wt.BOOL, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_void_p))
ConvertSDDL = _sig(advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL,
                   wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
                   ctypes.POINTER(wt.DWORD))


class PipeError(OSError):
    pass


def _err():
    return ctypes.get_last_error()


def current_user_sid():
    token = wt.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise PipeError(_err(), "OpenProcessToken failed")
    try:
        size = wt.DWORD()
        GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not GetTokenInformation(token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)):
            raise PipeError(_err(), "GetTokenInformation failed")
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = ctypes.c_void_p()
        if not ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise PipeError(_err(), "ConvertSidToStringSid failed")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            LocalFree(text)
    finally:
        CloseHandle(token)


class _SecurityAttributes:
    """Current user only (protected DACL with one entry)."""

    def __init__(self):
        self._sd = ctypes.c_void_p()
        sddl = "D:P(A;;GA;;;%s)" % current_user_sid()
        if not ConvertSDDL(sddl, 1, ctypes.byref(self._sd), None):
            raise PipeError(_err(), "security descriptor failed")
        self.sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self._sd, False)

    def free(self):
        if self._sd:
            LocalFree(self._sd)
            self._sd = ctypes.c_void_p()


def _wait(handles, timeout=INFINITE):
    arr = (wt.HANDLE * len(handles))(*handles)
    return WaitForMultipleObjects(len(handles), arr, False, timeout)


class _Client:
    def __init__(self, cid, handle):
        self.cid = cid
        self.handle = handle
        self.write_lock = threading.Lock()
        self.write_event = CreateEventW(None, True, False, None)
        self.closed = False
        self.thread = None


class PipeServer:
    def __init__(self, name, on_message, on_connect=None, on_disconnect=None, max_clients=4):
        self.name = name
        self.on_message = on_message
        self.on_connect = on_connect or (lambda cid: None)
        self.on_disconnect = on_disconnect or (lambda cid: None)
        self.max_clients = max_clients
        self._stop = CreateEventW(None, True, False, None)
        self._sec = None
        self._clients = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._accept_thread = None
        self._first = None

    # ---- life cycle ----
    def start(self):
        """Create the pipe. Raises PipeError if another process owns the name."""
        self._sec = _SecurityAttributes()
        self._first = self._create(first=True)
        if self._first is None:
            err = _err()
            self._sec.free()
            raise PipeError(err, "pipe name is taken or cannot be created")
        self._accept_thread = threading.Thread(target=self._accept_loop, name="pipe-accept",
                                               daemon=True)
        self._accept_thread.start()

    def close(self):
        SetEvent(self._stop)
        if self._accept_thread:
            self._accept_thread.join(3)
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            if c.thread:
                c.thread.join(3)
        if self._sec:
            self._sec.free()
        CloseHandle(self._stop)
        self._stop = None

    def client_count(self):
        with self._lock:
            return len(self._clients)

    # ---- sending ----
    def send(self, cid, msg):
        with self._lock:
            c = self._clients.get(cid)
        if c is None:
            return False
        data = protocol.encode(msg)
        with c.write_lock:
            if c.closed:
                return False
            ov = OVERLAPPED()
            ov.hEvent = c.write_event
            ResetEvent(c.write_event)
            n = wt.DWORD()
            if not WriteFile(c.handle, data, len(data), ctypes.byref(n), ctypes.byref(ov)):
                if _err() != ERROR_IO_PENDING:
                    return False
                if WaitForSingleObject(c.write_event, 2000) != WAIT_OBJECT_0:
                    CancelIoEx(c.handle, ctypes.byref(ov))
                    GetOverlappedResult(c.handle, ctypes.byref(ov), ctypes.byref(n), True)
                    return False
                if not GetOverlappedResult(c.handle, ctypes.byref(ov), ctypes.byref(n), False):
                    return False
            return n.value == len(data)

    # ---- internals ----
    def _create(self, first=False):
        mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
        if first:
            mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        h = CreateNamedPipeW(self.name, mode, PIPE_TYPE_BYTE | PIPE_REJECT_REMOTE_CLIENTS,
                             self.max_clients, BUF_SIZE, BUF_SIZE, 0,
                             ctypes.byref(self._sec.sa))
        if h in (None, 0, INVALID_HANDLE_VALUE):
            return None
        return h

    def _stopping(self):
        return WaitForSingleObject(self._stop, 0) == WAIT_OBJECT_0

    def _accept_loop(self):
        h = self._first
        ev = CreateEventW(None, True, False, None)
        try:
            while not self._stopping():
                if h is None:
                    h = self._create()
                    if h is None:  # all instances busy (or transient error)
                        if _wait([self._stop], 500) == WAIT_OBJECT_0:
                            break
                        continue
                ov = OVERLAPPED()
                ov.hEvent = ev
                ResetEvent(ev)
                n = wt.DWORD()
                connected = False
                if ConnectNamedPipe(h, ctypes.byref(ov)):
                    connected = True
                else:
                    err = _err()
                    if err == ERROR_PIPE_CONNECTED:
                        connected = True
                    elif err == ERROR_IO_PENDING:
                        if _wait([ev, self._stop]) != WAIT_OBJECT_0:
                            CancelIoEx(h, ctypes.byref(ov))
                            GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), True)
                            break
                        connected = bool(GetOverlappedResult(h, ctypes.byref(ov),
                                                             ctypes.byref(n), False))
                if connected:
                    self._add_client(h)
                else:
                    DisconnectNamedPipe(h)
                    CloseHandle(h)
                h = None
        finally:
            if h is not None:
                CloseHandle(h)
            CloseHandle(ev)

    def _add_client(self, h):
        c = _Client(next(self._ids), h)
        with self._lock:
            self._clients[c.cid] = c
        c.thread = threading.Thread(target=self._client_loop, args=(c,),
                                    name=f"pipe-client-{c.cid}", daemon=True)
        try:
            self.on_connect(c.cid)
        except Exception:
            log.exception("on_connect failed")
        c.thread.start()

    def _client_loop(self, c):
        ev = CreateEventW(None, True, False, None)
        buf = ctypes.create_string_buffer(BUF_SIZE)
        pending = b""
        bad = 0
        try:
            while not self._stopping():
                ov = OVERLAPPED()
                ov.hEvent = ev
                ResetEvent(ev)
                n = wt.DWORD()
                if not ReadFile(c.handle, buf, BUF_SIZE, ctypes.byref(n), ctypes.byref(ov)):
                    err = _err()
                    if err == ERROR_IO_PENDING:
                        if _wait([ev, self._stop]) != WAIT_OBJECT_0:
                            CancelIoEx(c.handle, ctypes.byref(ov))
                            GetOverlappedResult(c.handle, ctypes.byref(ov), ctypes.byref(n), True)
                            break
                        if not GetOverlappedResult(c.handle, ctypes.byref(ov),
                                                   ctypes.byref(n), False):
                            if _err() != ERROR_MORE_DATA:
                                break
                    elif err != ERROR_MORE_DATA:
                        break
                pending += buf.raw[:n.value]
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if not line.strip():
                        continue
                    try:
                        msg = protocol.decode(line)
                    except ValueError:
                        bad += 1
                        log.warning("bad line from host %s", c.cid)
                        if bad >= 3:
                            break
                        continue
                    bad = 0
                    try:
                        self.on_message(c.cid, msg)
                    except Exception:
                        log.exception("message handler failed")
                if bad >= 3:
                    log.warning("dropping host %s after 3 bad lines", c.cid)
                    break
                if len(pending) > protocol.MAX_LINE:
                    pending = b""
                    bad += 1
                    if bad >= 3:
                        break
        finally:
            CloseHandle(ev)
            with c.write_lock:
                c.closed = True
                DisconnectNamedPipe(c.handle)
                CloseHandle(c.handle)
                CloseHandle(c.write_event)
            with self._lock:
                self._clients.pop(c.cid, None)
            try:
                self.on_disconnect(c.cid)
            except Exception:
                log.exception("on_disconnect failed")


class PipeClient:
    """Small blocking client (tests and --self-test)."""

    def __init__(self, name, timeout=2.0):
        deadline = time.monotonic() + timeout
        while True:
            h = CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if h not in (None, 0, INVALID_HANDLE_VALUE):
                break
            err = _err()
            if time.monotonic() > deadline:
                raise PipeError(err, "cannot connect to pipe")
            if err == ERROR_PIPE_BUSY:
                WaitNamedPipeW(name, 200)
            else:
                time.sleep(0.05)
        self.handle = h
        self._buf = b""

    def write_raw(self, data):
        n = wt.DWORD()
        if not WriteFile(self.handle, data, len(data), ctypes.byref(n), None):
            raise PipeError(_err(), "write failed")

    def send(self, msg):
        self.write_raw(protocol.encode(msg))

    def read(self, timeout=2.0):
        """Next message dict, or None on timeout / closed pipe."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            avail = wt.DWORD()
            if not PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(avail), None):
                return None
            if avail.value:
                buf = ctypes.create_string_buffer(avail.value)
                n = wt.DWORD()
                if not ReadFile(self.handle, buf, avail.value, ctypes.byref(n), None):
                    return None
                self._buf += buf.raw[:n.value]
                continue
            if time.monotonic() > deadline:
                return None
            time.sleep(0.01)
        line, self._buf = self._buf.split(b"\n", 1)
        return protocol.decode(line)

    def closed_by_server(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            avail = wt.DWORD()
            if not PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(avail), None):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None
