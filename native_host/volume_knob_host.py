"""Native-messaging host: Brave extension <-> volume knob program.

Brave starts this when the extension connects (one copy per Brave profile) and
ends it by closing stdin. It forwards messages between Brave (stdio, 4-byte
length + JSON) and the program's named pipe (JSON lines, see PROTOCOL.md).
If the program isn't running, it keeps retrying calmly.

Standard library + ctypes only. Stdio is used through the raw Windows handles,
so it works under python.exe, pythonw.exe or a windowed .exe alike.

Entry points:  run(argv) -> int   (the program's .exe calls this when
               argv[1] starts with "chrome-extension://")
               python volume_knob_host.py chrome-extension://<id>/
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import struct
import sys
import threading
import time

HOST_VERSION = "1.0.0"
PROTOCOL = 2
MAX_VOLUME = 1.5
BOOST_STATES = ("off", "on", "blocked", "unavailable", "error")
KNOWN_FEATURES = ("boost", "set_volume")
RETRY = 2.0  # PROTOCOL.md v2: reconnect within 2 s
DEFAULT_PIPE = r"\\.\pipe\8bitdo-volume-knob"
APP_FOLDER = "8BitDo Volume Knob"
LOG_NAME = "native_host.log"
LOG_MAX = 64 * 1024
MAX_LINE = 64 * 1024
MAX_NATIVE_IN = 1024 * 1024
HEARTBEAT = 2.0
SITES = ("youtube", "ytmusic")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
ERROR_IO_PENDING = 997
ERROR_PIPE_BUSY = 231
ERROR_MORE_DATA = 234
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                              ctypes.c_void_p]
kernel32.ReadFile.restype = wt.BOOL
kernel32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                               ctypes.c_void_p]
kernel32.WriteFile.restype = wt.BOOL
kernel32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
                                         wt.BOOL]
kernel32.GetOverlappedResult.restype = wt.BOOL
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.ResetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetStdHandle.argtypes = [wt.DWORD]
kernel32.GetStdHandle.restype = wt.HANDLE


# ---- log (small, capped; one line per connect / disconnect) -----------------

def _log_path():
    folder = os.environ.get("VOLUME_KNOB_DATA") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_FOLDER)
    return os.path.join(folder, LOG_NAME)


def log(text):
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" [{os.getpid()}] {text}\n")
    except OSError:
        pass


# ---- message checks ----------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean_state(msg):
    """Extension -> program state message, checked and rebuilt. None if bad."""
    tabs = msg.get("tabs")
    if not isinstance(tabs, list):
        return None
    out = []
    for t in tabs[:200]:
        if not isinstance(t, dict):
            continue
        tab, site, vol = t.get("tab"), t.get("site"), t.get("volume")
        if not isinstance(tab, int) or isinstance(tab, bool) or site not in SITES:
            continue
        if vol is not None and not _is_num(vol):
            continue
        entry = {"tab": tab, "site": site, "playing": t.get("playing") is True,
                 # None = player not loaded yet (PROTOCOL.md v2)
                 "volume": None if vol is None else min(MAX_VOLUME, max(0.0, float(vol))),
                 "muted": t.get("muted") is True}
        mv = t.get("max_volume")
        if _is_num(mv):
            entry["max_volume"] = min(MAX_VOLUME, max(0.0, float(mv)))
        if t.get("boost") in BOOST_STATES:
            entry["boost"] = t["boost"]
        out.append(entry)
    return {"type": "state", "tabs": out}


def clean_command(msg):
    """Program -> extension command, checked and rebuilt. None if not for Brave."""
    kind = msg.get("type")
    # "probe" is for tests only: the extension answers with what the page's
    # own volume controls show. The program never sends it.
    if kind not in ("volume", "set_volume", "mute", "probe"):
        return None
    cmd = {"type": kind}
    tabs = msg.get("tabs")
    if isinstance(tabs, list):
        cmd["tabs"] = [t for t in tabs[:200] if isinstance(t, int) and not isinstance(t, bool)]
    elif msg.get("site") in SITES + ("all",):
        cmd["site"] = msg["site"]
    else:
        return None
    if kind == "volume":
        d = msg.get("delta")
        if not _is_num(d):
            return None
        cmd["delta"] = max(-MAX_VOLUME, min(MAX_VOLUME, float(d)))
        m = msg.get("max")
        # Missing max = 1.0 (no boost). The crossing-100 % rule and the clamp to
        # [0, max] are applied in the tab (page.js), where the current value is known.
        cmd["max"] = min(MAX_VOLUME, max(0.0, float(m))) if _is_num(m) else 1.0
    elif kind == "set_volume":
        v = msg.get("volume")
        if not _is_num(v):
            return None
        cmd["volume"] = min(MAX_VOLUME, max(0.0, float(v)))
    else:
        if isinstance(msg.get("muted"), bool):
            cmd["muted"] = msg["muted"]
    return cmd


# ---- Brave side: stdio --------------------------------------------------------

class Stdio:
    def __init__(self):
        self.h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        self.h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        self.lock = threading.Lock()

    def _read_exact(self, n):
        buf = bytearray()
        chunk = ctypes.create_string_buffer(min(max(n, 1), 65536))
        while len(buf) < n:
            got = wt.DWORD(0)
            want = min(n - len(buf), len(chunk))
            ok = kernel32.ReadFile(self.h_in, chunk, want, ctypes.byref(got), None)
            if not ok or got.value == 0:
                return None
            buf += chunk.raw[:got.value]
        return bytes(buf)

    def read(self):
        """Next message from Brave, {} for a bad one, None when Brave closed stdin."""
        head = self._read_exact(4)
        if head is None:
            return None
        size = struct.unpack("<I", head)[0]
        if size > MAX_NATIVE_IN:
            return None
        body = self._read_exact(size) if size else b""
        if body is None:
            return None
        try:
            msg = json.loads(body.decode("utf-8"))
        except ValueError:
            return {}
        return msg if isinstance(msg, dict) else {}

    def write(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        data = struct.pack("<I", len(data)) + data
        with self.lock:
            done = 0
            while done < len(data):
                got = wt.DWORD(0)
                part = data[done:]
                if not kernel32.WriteFile(self.h_out, part, len(part), ctypes.byref(got), None):
                    return False
                done += got.value
        return True


# ---- program side: the named pipe (overlapped, so reads and writes never block each other)

class Pipe:
    def __init__(self, handle):
        self.h = handle
        self.write_lock = threading.Lock()
        self.read_event = kernel32.CreateEventW(None, True, False, None)
        self.write_event = kernel32.CreateEventW(None, True, False, None)
        self.read_ov = OVERLAPPED()
        self.read_ov.hEvent = self.read_event
        self.read_buf = ctypes.create_string_buffer(65536)
        self.read_pending = False
        self.broken = False

    @staticmethod
    def open(name):
        h = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING,
                                 FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT
                                 | SECURITY_IDENTIFICATION, None)
        if h == INVALID_HANDLE_VALUE or not h:
            return None, ctypes.get_last_error()
        return Pipe(h), 0

    def write_line(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_LINE:
            return True
        with self.write_lock:
            if self.broken:
                return False
            ov = OVERLAPPED()
            ov.hEvent = self.write_event
            kernel32.ResetEvent(self.write_event)
            got = wt.DWORD(0)
            ok = kernel32.WriteFile(self.h, data, len(data), ctypes.byref(got), ctypes.byref(ov))
            if not ok:
                if ctypes.get_last_error() != ERROR_IO_PENDING:
                    self.fail()
                    return False
                if kernel32.WaitForSingleObject(self.write_event, 5000) != WAIT_OBJECT_0:
                    kernel32.CancelIoEx(self.h, ctypes.byref(ov))
                    kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), True)
                    self.fail()
                    return False
                if not kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got),
                                                    False):
                    self.fail()
                    return False
            return True

    def read_some(self, timeout):
        """Bytes read, b"" on timeout, None if the pipe closed."""
        got = wt.DWORD(0)
        if not self.read_pending:
            kernel32.ResetEvent(self.read_event)
            ok = kernel32.ReadFile(self.h, self.read_buf, len(self.read_buf), ctypes.byref(got),
                                   ctypes.byref(self.read_ov))
            if ok:
                return self.read_buf.raw[:got.value] if got.value else b""
            err = ctypes.get_last_error()
            if err == ERROR_MORE_DATA:
                return self.read_buf.raw[:len(self.read_buf)]
            if err != ERROR_IO_PENDING:
                return None
            self.read_pending = True
        wait = kernel32.WaitForSingleObject(self.read_event, max(0, int(timeout * 1000)))
        if wait == WAIT_TIMEOUT:
            return b""
        self.read_pending = False
        if not kernel32.GetOverlappedResult(self.h, ctypes.byref(self.read_ov), ctypes.byref(got),
                                            False):
            if ctypes.get_last_error() == ERROR_MORE_DATA:
                return self.read_buf.raw[:got.value]
            return None
        return self.read_buf.raw[:got.value]

    def fail(self):
        self.broken = True
        kernel32.CancelIoEx(self.h, None)

    def close(self):
        with self.write_lock:
            self.broken = True
        if self.read_pending:
            kernel32.CancelIoEx(self.h, ctypes.byref(self.read_ov))
            got = wt.DWORD(0)
            kernel32.GetOverlappedResult(self.h, ctypes.byref(self.read_ov), ctypes.byref(got), True)
            self.read_pending = False
        kernel32.CloseHandle(self.h)
        kernel32.CloseHandle(self.read_event)
        kernel32.CloseHandle(self.write_event)


# ---- the bridge ---------------------------------------------------------------------

class Host:
    def __init__(self, stdio, pipe_name):
        self.stdio = stdio
        self.pipe_name = pipe_name
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = {"type": "state", "tabs": []}
        self.ext_version = None
        self.features = []
        self.got_hello = threading.Event()
        self.pipe = None
        self.last_sent = 0.0

    # Brave -> program
    def stdin_loop(self):
        try:
            while not self.stop.is_set():
                msg = self.stdio.read()
                if msg is None:
                    break
                kind = msg.get("type")
                if kind == "hello":
                    v = msg.get("ext")
                    self.ext_version = v[:32] if isinstance(v, str) else None
                    f = msg.get("features")
                    self.features = [x for x in KNOWN_FEATURES if isinstance(f, list) and x in f]
                    self.got_hello.set()
                elif kind == "state":
                    st = clean_state(msg)
                    if st is not None:
                        with self.lock:
                            self.state = st
                        self.send_state()
                elif kind == "probe":
                    # Test diagnostics; the program ignores unknown types.
                    p = self.pipe
                    if p:
                        p.write_line({k: msg.get(k) for k in
                                      ("type", "tab", "ytSlider", "ytmSlider", "saved")})
        finally:
            self.stop.set()
            p = self.pipe
            if p:
                p.fail()  # wake the pipe loop so it can end

    def send_state(self):
        p = self.pipe
        if not p:
            return
        with self.lock:
            st = self.state
        if p.write_line(st):
            self.last_sent = time.monotonic()

    # program -> Brave, plus connecting and the heartbeat
    def pipe_loop(self):
        # The extension says hello first (with its features); wait for it briefly
        # so the hello to the program is complete.
        self.got_hello.wait(1.0)
        was_connected = None
        while not self.stop.is_set():
            pipe, err = Pipe.open(self.pipe_name)
            if pipe is None:
                if was_connected is not False:
                    log(f"program not reachable (error {err}), retrying")
                    was_connected = False
                # Cheap check (no pipe = instant failure), every 2 s as PROTOCOL.md asks.
                self.stop.wait(1.0 if err == ERROR_PIPE_BUSY else RETRY)
                continue
            log("connected to the program")
            was_connected = True
            try:
                hello = {"type": "hello", "protocol": PROTOCOL, "host_version": HOST_VERSION,
                         "features": list(self.features)}
                if self.ext_version:
                    hello["ext_version"] = self.ext_version
                if pipe.write_line(hello):
                    self.pipe = pipe  # only now: the hello must be the first line
                    self.send_state()
                    self._serve(pipe)
            finally:
                self.pipe = None
                pipe.close()
            if not self.stop.is_set():
                log("program closed the connection, retrying")
                was_connected = False
                self.stop.wait(0.5)

    def _serve(self, pipe):
        pending = b""
        while not self.stop.is_set() and not pipe.broken:
            wait = HEARTBEAT - (time.monotonic() - self.last_sent)
            if wait <= 0:
                self.send_state()
                continue
            data = pipe.read_some(min(wait, 0.5))
            if data is None:
                return
            if not data:
                continue
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._from_program(line)
            if len(pending) > MAX_LINE:
                pending = b""

    def _from_program(self, line):
        line = line.strip()
        if not line or len(line) > MAX_LINE:
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "hello":
            if msg.get("protocol") != PROTOCOL:
                log(f"program speaks protocol {msg.get('protocol')!r}, host speaks {PROTOCOL}")
            return
        cmd = clean_command(msg)
        if cmd is not None:
            self.stdio.write(cmd)


def run(argv=None):
    """Run the host until Brave closes the connection. Returns the exit code."""
    # Accepts sys.argv or sys.argv[1:] (the program's main() passes the latter).
    argv = list(sys.argv if argv is None else argv)
    if not any(isinstance(a, str) and a.startswith("chrome-extension://") for a in argv):
        # Not started by the browser: nothing to do.
        return 2
    pipe_name = os.environ.get("VOLUME_KNOB_PIPE") or DEFAULT_PIPE
    host = Host(Stdio(), pipe_name)
    reader = threading.Thread(target=host.stdin_loop, name="stdin", daemon=True)
    reader.start()
    try:
        host.pipe_loop()
    finally:
        host.stop.set()
    log("browser closed the connection, host ends")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv))
