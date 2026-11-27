"""Local WebSocket server for the Brave extension (see PROTOCOL.md, "Transport").

127.0.0.1 only, SO_EXCLUSIVEADDRUSE, first free port of a short list.
A connection is accepted only when:
- the connecting process passes `peer_check(pid)` (normally: brave.exe of the
  same Windows user, found through the TCP table),
- the HTTP upgrade request is GET <PATH>, Host 127.0.0.1:<port>, the exact
  extension Origin, a valid WebSocket key, within 2 s and 8 KB.
Client frames must be masked, text only, unfragmented, at most 64 KB.
Also a small blocking client used by tests and --self-test. Standard library
and ctypes only."""
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import itertools
import logging
import os
import select
import socket
import struct
import threading
import time

from . import protocol

log = logging.getLogger("knob.ws")

PATH = "/volume-knob/2"
HOST = "127.0.0.1"
MAX_HEAD = 8 * 1024
HANDSHAKE_SECONDS = 2.0
MAX_MESSAGE = 64 * 1024
MAX_BAD = 3
SEND_TIMEOUT = 2.0
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

# ---------------------------------------------------------------- Win32 (read only)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_iph = ctypes.WinDLL("iphlpapi", use_last_error=True)


def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


_OpenProcess = _sig(_k32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_CloseHandle = _sig(_k32, "CloseHandle", wt.BOOL, wt.HANDLE)
_GetCurrentProcess = _sig(_k32, "GetCurrentProcess", wt.HANDLE)
_LocalFree = _sig(_k32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
_QueryImage = _sig(_k32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD,
                   wt.LPWSTR, ctypes.POINTER(wt.DWORD))
_OpenProcessToken = _sig(_adv, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD,
                         ctypes.POINTER(wt.HANDLE))
_GetTokenInformation = _sig(_adv, "GetTokenInformation", wt.BOOL, wt.HANDLE, ctypes.c_int,
                            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
_SidToString = _sig(_adv, "ConvertSidToStringSidW", wt.BOOL, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))
_GetExtendedTcpTable = _sig(_iph, "GetExtendedTcpTable", wt.DWORD, ctypes.c_void_p,
                            ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x8
TOKEN_USER_CLASS = 1
AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
ERROR_INSUFFICIENT_BUFFER = 122
LOOPBACK = 0x0100007F  # 127.0.0.1 as stored in the table (network order in a LE DWORD)


class WsError(OSError):
    pass


def _token_sid(token):
    size = wt.DWORD()
    _GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not _GetTokenInformation(token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)):
        return None
    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
    text = ctypes.c_void_p()
    if not _SidToString(sid, ctypes.byref(text)):
        return None
    try:
        return ctypes.wstring_at(text.value)
    finally:
        _LocalFree(text)


def _process_sid(hproc):
    token = wt.HANDLE()
    if not _OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        return _token_sid(token)
    finally:
        _CloseHandle(token)


def current_user_sid():
    """String SID of the user running this program."""
    sid = _process_sid(_GetCurrentProcess())
    if not sid:
        raise WsError(ctypes.get_last_error(), "cannot read the current user SID")
    return sid


def process_info(pid):
    """(lower-case exe name, user SID string) of a process; None parts if unreadable."""
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        exe = None
        if _QueryImage(h, 0, buf, ctypes.byref(size)):
            exe = os.path.basename(buf.value).lower()
        return exe, _process_sid(h)
    finally:
        _CloseHandle(h)


class _TcpRow(ctypes.Structure):
    _fields_ = [("state", wt.DWORD), ("local_addr", wt.DWORD), ("local_port", wt.DWORD),
                ("remote_addr", wt.DWORD), ("remote_port", wt.DWORD), ("pid", wt.DWORD)]


def _port(dword):
    return socket.ntohs(dword & 0xFFFF)


def tcp_owner_pid(client_port, server_port):
    """PID owning the loopback socket 127.0.0.1:client_port -> 127.0.0.1:server_port."""
    size = wt.DWORD(0)
    buf = None
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value or 16 * 1024)
        size = wt.DWORD(len(buf))
        rc = _GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                  TCP_TABLE_OWNER_PID_ALL, 0)
        if rc == 0:
            break
        if rc != ERROR_INSUFFICIENT_BUFFER:
            return None
    else:
        return None
    count = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD),
                       ctypes.POINTER(_TcpRow * count)).contents
    for r in rows:
        if (r.local_addr == LOOPBACK and r.remote_addr == LOOPBACK
                and _port(r.local_port) == client_port and _port(r.remote_port) == server_port):
            return r.pid
    return None


def process_check(exe_names):
    """peer_check: the process is one of `exe_names` and runs as the same user as us."""
    names = frozenset(n.lower() for n in exe_names)
    me = current_user_sid()

    def check(pid):
        exe, sid = process_info(pid)
        return exe in names and sid == me
    return check


# ---------------------------------------------------------------- frames
def _frame(opcode, payload=b"", mask=False):
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        return head + key + _unmask(payload, key)
    return head + payload


def _unmask(data, key):
    if not data:
        return b""
    k = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "little") ^ int.from_bytes(k, "little")).to_bytes(
        len(data), "little")


def _close_payload(code):
    return struct.pack("!H", code)


class _Closed(Exception):
    pass


class _Conn:
    """A socket with a reusable read buffer that survives timeouts."""

    def __init__(self, sock, stopping):
        self.sock = sock
        self.buf = b""
        self.stopping = stopping

    def read_exact(self, n):
        while len(self.buf) < n:
            if self.stopping():
                raise _Closed()
            try:  # short wait so close() is noticed quickly; send timeout stays 2 s
                if not select.select([self.sock], [], [], 0.25)[0]:
                    continue
                chunk = self.sock.recv(max(4096, n - len(self.buf)))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                raise _Closed()
            if not chunk:
                raise _Closed()
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def _parse_head(head):
    """Request head bytes -> (method, path, {lower name: value}) or None (malformed/duplicate)."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        return None
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        name = name.strip().lower()
        if not sep or not name or name != name.strip() or name in headers:
            return None
        headers[name] = value.strip()
    return parts[0], parts[1], headers


def check_request(head, port, origin):
    """None if the upgrade request is acceptable, else a short reason."""
    parsed = _parse_head(head)
    if parsed is None:
        return "malformed request"
    method, path, h = parsed
    if method != "GET":
        return "method"
    if path != PATH:
        return "path"
    if h.get("host") != f"{HOST}:{port}":
        return "host"
    if h.get("origin") != origin:
        return "origin"
    if h.get("upgrade", "").lower() != "websocket":
        return "upgrade"
    if "upgrade" not in [t.strip().lower() for t in h.get("connection", "").split(",")]:
        return "connection"
    if h.get("sec-websocket-version") != "13":
        return "version"
    key = h.get("sec-websocket-key", "")
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return "key"
    except (ValueError, TypeError):
        return "key"
    return None


class _Client:
    def __init__(self, cid, sock):
        self.cid = cid
        self.sock = sock
        self.write_lock = threading.Lock()
        self.closed = False
        self.thread = None


class WsServer:
    def __init__(self, ports, on_message, on_connect=None, on_disconnect=None,
                 origin=protocol.EXTENSION_ORIGIN, peer_check=None, max_clients=4):
        if peer_check is None:
            raise ValueError("peer_check is required")
        self.ports = list(ports)
        self.on_message = on_message
        self.on_connect = on_connect or (lambda cid: None)
        self.on_disconnect = on_disconnect or (lambda cid: None)
        self.origin = origin
        self.peer_check = peer_check
        self.max_clients = max_clients
        self.port = None
        self._sock = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = {}      # cid -> _Client (handshake done)
        self._pending = set()   # sockets still in the handshake
        self._threads = []
        self._ids = itertools.count(1)
        self._accept_thread = None

    # ---- life cycle ----
    def start(self):
        """Bind the first free port. Returns it. Raises WsError if none is free."""
        last = None
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind((HOST, port))
                s.listen(8)
            except OSError as e:
                s.close()
                last = e
                continue
            s.settimeout(0.5)
            self._sock = s
            self.port = s.getsockname()[1]
            break
        if self._sock is None:
            raise WsError(getattr(last, "errno", None), "no free port in %s" % (self.ports,))
        self._accept_thread = threading.Thread(target=self._accept_loop, name="ws-accept",
                                               daemon=True)
        self._accept_thread.start()
        return self.port

    def close(self):
        self._stop.set()
        if self._accept_thread:
            if self.port and self._accept_thread.is_alive():
                try:  # wake accept() now instead of after its 0.5 s timeout
                    socket.create_connection((HOST, self.port), timeout=0.5).close()
                except OSError:
                    pass
            self._accept_thread.join(3)
        if self._sock:
            self._sock.close()
        with self._lock:
            socks = [c.sock for c in self._clients.values()] + list(self._pending)
            threads = list(self._threads)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in threads:
            t.join(3)

    def client_count(self):
        with self._lock:
            return len(self._clients)

    # ---- sending ----
    def send(self, cid, msg):
        with self._lock:
            c = self._clients.get(cid)
        if c is None:
            return False
        data = _frame(OP_TEXT, protocol.encode(msg).rstrip(b"\n"))
        return self._send_raw(c, data)

    def _send_raw(self, c, data):
        with c.write_lock:
            if c.closed:
                return False
            try:
                c.sock.sendall(data)
                return True
            except OSError:  # incl. timeout: the stream is now broken
                c.closed = True
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return False

    # ---- internals ----
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.1)
                continue
            if self._stop.is_set():  # close()'s wake-up connection (or one just too late)
                sock.close()
                break
            if not self._admit(sock, addr):
                sock.close()

    def _admit(self, sock, addr):
        if addr[0] != HOST:
            return False
        with self._lock:
            if len(self._clients) + len(self._pending) >= self.max_clients:
                log.warning("browser connection refused: too many clients")
                return False
        pid = None
        try:
            pid = tcp_owner_pid(addr[1], self.port)
            ok = pid is not None and self.peer_check(pid)
        except Exception:
            log.exception("peer check failed")
            ok = False
        if not ok:
            log.warning("browser connection refused: process %s not allowed", pid)
            return False
        with self._lock:
            self._pending.add(sock)
            t = threading.Thread(target=self._client_main, args=(sock,), daemon=True,
                                 name="ws-client")
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()
        return True

    def _handshake(self, sock):
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        head = b""
        while b"\r\n\r\n" not in head:
            left = deadline - time.monotonic()
            if left <= 0 or self._stop.is_set() or len(head) > MAX_HEAD:
                return None, "timeout or too long"
            sock.settimeout(min(left, 0.5))
            try:
                chunk = sock.recv(MAX_HEAD + 1 - len(head))
            except socket.timeout:
                continue
            except OSError:
                return None, "closed"
            if not chunk:
                return None, "closed"
            head += chunk
        end = head.index(b"\r\n\r\n")
        if end > MAX_HEAD or len(head) > end + 4:  # no data may follow the head yet
            return None, "too long"
        why = check_request(head[:end], self.port, self.origin)
        if why:
            return None, why
        h = _parse_head(head[:end])[2]
        return _accept_key(h["sec-websocket-key"]), None

    def _client_main(self, sock):
        c = None
        try:
            accept, why = self._handshake(sock)
            if accept is None:
                log.warning("browser connection refused: %s", why)
                if why not in ("closed", "timeout or too long"):
                    try:
                        sock.settimeout(1)
                        sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                                     b"Connection: close\r\n\r\n")
                    except OSError:
                        pass
                return
            sock.settimeout(SEND_TIMEOUT)
            try:
                sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                              % accept).encode("ascii"))
            except OSError:
                return
            c = _Client(next(self._ids), sock)
            with self._lock:
                self._pending.discard(sock)
                if self._stop.is_set():
                    return
                self._clients[c.cid] = c
            try:
                self.on_connect(c.cid)
            except Exception:
                log.exception("on_connect failed")
            self._read_loop(c)
        finally:
            with self._lock:
                self._pending.discard(sock)
                if c is not None:
                    self._clients.pop(c.cid, None)
            if c is not None:
                with c.write_lock:
                    c.closed = True
            try:
                sock.close()
            except OSError:
                pass
            if c is not None:
                try:
                    self.on_disconnect(c.cid)
                except Exception:
                    log.exception("on_disconnect failed")

    def _fail(self, c, code, why):
        log.warning("dropping browser client %s: %s", c.cid, why)
        self._send_raw(c, _frame(OP_CLOSE, _close_payload(code)))

    def _read_loop(self, c):
        conn = _Conn(c.sock, self._stop.is_set)
        bad = 0
        try:
            while True:
                b0, b1 = conn.read_exact(2)
                fin, rsv, op = b0 & 0x80, b0 & 0x70, b0 & 0x0F
                masked, n = b1 & 0x80, b1 & 0x7F
                if rsv or not masked:
                    return self._fail(c, 1002, "unmasked or reserved bits")
                if n == 126:
                    n = struct.unpack("!H", conn.read_exact(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", conn.read_exact(8))[0]
                if op >= 0x8:  # control frame
                    if not fin or n > 125:
                        return self._fail(c, 1002, "bad control frame")
                elif not fin or op == OP_CONT:
                    return self._fail(c, 1002, "fragmented message")
                elif op != OP_TEXT:
                    return self._fail(c, 1003, "not a text message")
                if n > MAX_MESSAGE:
                    return self._fail(c, 1009, "message too big")
                key = conn.read_exact(4)
                payload = _unmask(conn.read_exact(n), key)
                if op == OP_PING:
                    self._send_raw(c, _frame(OP_PONG, payload))
                    continue
                if op == OP_PONG:
                    continue
                if op == OP_CLOSE:
                    self._send_raw(c, _frame(OP_CLOSE, _close_payload(1000)))
                    return
                if op not in (OP_TEXT,):
                    return self._fail(c, 1002, "unknown opcode")
                try:
                    msg = protocol.decode(payload)
                except ValueError:
                    bad += 1
                    log.warning("bad message from browser client %s", c.cid)
                    if bad >= MAX_BAD:
                        return self._fail(c, 1008, "3 bad messages")
                    continue
                bad = 0
                try:
                    self.on_message(c.cid, msg)
                except Exception:
                    log.exception("message handler failed")
        except _Closed:
            return


class WsClient:
    """Small blocking client (tests and --self-test)."""

    def __init__(self, port, origin=protocol.EXTENSION_ORIGIN, path=PATH, host=None,
                 timeout=2.0, extra_head=""):
        self.sock = socket.create_connection((HOST, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        head = (f"GET {path} HTTP/1.1\r\nHost: {host or f'{HOST}:{port}'}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n")
        if origin is not None:
            head += f"Origin: {origin}\r\n"
        head += extra_head + "\r\n"
        self._buf = b""
        self.status = None
        try:
            self.sock.sendall(head.encode("ascii"))
            resp = self._read_head()
        except OSError:
            resp = b""
        if resp:
            self.status = int(resp.split(b" ", 2)[1])
            if self.status == 101 and _accept_key(key).encode() not in resp:
                self.status = -1

    def _read_head(self):
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return b""
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        return head

    @property
    def ok(self):
        return self.status == 101

    def write_raw(self, data):
        self.sock.sendall(data)

    def send_frame(self, opcode, payload, fin=True, mask=True):
        data = _frame(opcode, payload, mask=mask)
        if not fin:
            data = bytes([data[0] & 0x7F]) + data[1:]
        self.write_raw(data)

    def send(self, msg):
        self.send_frame(OP_TEXT, protocol.encode(msg).rstrip(b"\n"))

    def _recv(self, n, deadline):
        while len(self._buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout()
            self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame_or_raise(self, deadline):
        b0, b1 = self._recv(2, deadline)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv(2, deadline))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv(8, deadline))[0]
        return b0 & 0x0F, self._recv(n, deadline)

    def read_frame(self, timeout=2.0):
        """(opcode, payload) or None on timeout / closed."""
        try:
            return self._frame_or_raise(time.monotonic() + timeout)
        except OSError:
            return None

    def read(self, timeout=2.0):
        """Next message dict (pings/pongs skipped), or None."""
        deadline = time.monotonic() + timeout
        while True:
            f = self.read_frame(max(0.0, deadline - time.monotonic()))
            if f is None or f[0] == OP_CLOSE:
                return None
            if f[0] == OP_TEXT:
                return protocol.decode(f[1])

    def closed_by_server(self, timeout=2.0):
        """True if the server sent a close frame or dropped the connection."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                op, _ = self._frame_or_raise(deadline)
            except socket.timeout:
                return False
            except OSError:  # EOF (ConnectionError) or reset
                return True
            if op == OP_CLOSE:
                return True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
