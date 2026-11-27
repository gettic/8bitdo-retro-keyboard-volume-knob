"""Real loopback WebSocket server on random free ports (never the program's own ports)."""
import os
import socket
import struct
import sys
import time
import unittest

from knob import protocol, ws_server
from knob.browser import BrowserBridge, BrowserState
from knob.ws_server import (OP_BIN, OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT, WsClient,
                            WsError, WsServer, process_check, process_info)

ALLOW = lambda pid: True  # noqa: E731  (peer check tested separately)
HELLO = {"type": "hello", "protocol": 2, "host_version": "test",
         "features": ["boost", "set_volume"]}


def wait_for(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def raw_connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    return s


def recv_all(s, timeout=3.0):
    s.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    return data


class Base(unittest.TestCase):
    peer_check = staticmethod(ALLOW)

    def setUp(self):
        self.state = BrowserState()
        self.bridge = BrowserBridge(self.state, time.monotonic)
        self.disconnects = []
        self.server = WsServer([0], self.bridge.on_message, self.bridge.on_connect,
                               self._on_disconnect, peer_check=self.peer_check)
        self.bridge.send = self.server.send
        self.port = self.server.start()
        self.clients = []

    def _on_disconnect(self, cid):
        self.disconnects.append(cid)
        self.bridge.on_disconnect(cid)

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.close()

    def client(self, **kw):
        c = WsClient(self.port, **kw)
        self.clients.append(c)
        return c


class HandshakeTests(Base):
    def test_good_handshake(self):
        c = self.client()
        self.assertEqual(c.status, 101)
        self.assertTrue(wait_for(lambda: self.server.client_count() == 1))

    def test_permessage_deflate_not_accepted(self):
        c = self.client(extra_head="Sec-WebSocket-Extensions: permessage-deflate\r\n")
        self.assertTrue(c.ok)
        # the 101 response was already consumed; check by a fresh raw request
        s = raw_connect(self.port)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        s.sendall((f"GET {ws_server.PATH} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
                   f"Origin: {protocol.EXTENSION_ORIGIN}\r\n\r\n").encode())
        s.settimeout(2)
        head = s.recv(4096)
        s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 101"))
        self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", head)  # RFC 6455 example
        self.assertNotIn(b"extensions", head.lower())

    def test_rejections(self):
        cases = {
            "wrong origin": dict(origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            "web origin": dict(origin="https://www.youtube.com"),
            "no origin": dict(origin=None),
            "wrong path": dict(path="/volume-knob/1"),
            "wrong host": dict(host="localhost:%d" % self.port),
            "rebinding host": dict(host="evil.example:%d" % self.port),
            "duplicate origin": dict(extra_head="Origin: https://evil.example\r\n"),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                c = self.client(**kw)
                self.assertEqual(c.status, 403, name)
        self.assertEqual(self.server.client_count(), 0)

    def test_head_too_long(self):
        s = raw_connect(self.port)
        s.sendall(b"GET " + ws_server.PATH.encode() + b" HTTP/1.1\r\nX: " + b"a" * 9000)
        self.assertEqual(recv_all(s), b"")  # dropped without upgrade
        s.close()

    def test_slow_handshake_dropped(self):
        s = raw_connect(self.port)
        t = time.monotonic()
        s.sendall(b"GET ")
        self.assertEqual(recv_all(s, 4), b"")
        self.assertLess(time.monotonic() - t, 3.5)
        s.close()

    def test_bad_key_and_version(self):
        for extra in ("Sec-WebSocket-Key: c2hvcnQ=", "Sec-WebSocket-Version: 8"):
            s = raw_connect(self.port)
            key = "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" if "Key" not in extra else extra
            ver = "Sec-WebSocket-Version: 13" if "Version" not in extra else extra
            s.sendall((f"GET {ws_server.PATH} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                       f"Upgrade: websocket\r\nConnection: Upgrade\r\n{key}\r\n{ver}\r\n"
                       f"Origin: {protocol.EXTENSION_ORIGIN}\r\n\r\n").encode())
            self.assertTrue(recv_all(s).startswith(b"HTTP/1.1 403"), extra)
            s.close()

    def test_max_clients(self):
        cs = [self.client() for _ in range(4)]
        self.assertTrue(all(c.ok for c in cs))
        self.assertTrue(wait_for(lambda: self.server.client_count() == 4))
        extra = self.client()
        self.assertFalse(extra.ok)
        cs[0].close()
        self.assertTrue(wait_for(lambda: self.server.client_count() == 3))
        self.assertTrue(self.client().ok)


class MessageTests(Base):
    def hello_client(self):
        c = self.client()
        self.assertTrue(c.ok)
        c.send(HELLO)
        reply = c.read()
        self.assertEqual((reply["type"], reply["protocol"]), ("hello", 2))
        return c

    def test_round_trip(self):
        c = self.hello_client()
        c.send({"type": "state", "tabs": [
            {"tab": 3, "site": "ytmusic", "playing": True, "volume": 0.2, "muted": False}]})
        self.assertTrue(wait_for(lambda: self.state.tabs(time.monotonic())))
        tab = self.state.tabs(time.monotonic())[0]
        self.bridge.command(tab.client, protocol.mute(True, tabs=[3]))
        self.assertEqual(c.read(), {"type": "mute", "muted": True, "tabs": [3]})

    def test_server_frames_unmasked_text_one_message(self):
        c = self.client()
        c.send(HELLO)
        op, payload = c.read_frame()
        self.assertEqual(op, OP_TEXT)
        self.assertFalse(payload.endswith(b"\n"))
        self.assertEqual(protocol.decode(payload)["type"], "hello")

    def test_large_message_16_bit_length(self):
        c = self.hello_client()
        tabs = [{"tab": i, "site": "youtube", "playing": False, "volume": 0.5, "muted": False}
                for i in range(600)]
        c.send({"type": "state", "tabs": tabs})
        self.assertTrue(wait_for(lambda: len(self.state.tabs(time.monotonic())) == 600))

    def test_ping_answered(self):
        c = self.client()
        c.send_frame(OP_PING, b"abc")
        self.assertEqual(c.read_frame(), (OP_PONG, b"abc"))

    def test_close_frame(self):
        c = self.client()
        c.send_frame(OP_CLOSE, struct.pack("!H", 1000))
        self.assertTrue(c.closed_by_server())
        self.assertTrue(wait_for(lambda: self.disconnects))

    def test_unmasked_frame_drops(self):
        c = self.client()
        c.send_frame(OP_TEXT, protocol.encode(HELLO).strip(), mask=False)
        self.assertTrue(c.closed_by_server())
        self.assertTrue(wait_for(lambda: self.server.client_count() == 0))

    def test_binary_drops(self):
        c = self.client()
        c.send_frame(OP_BIN, b"\x00\x01")
        self.assertTrue(c.closed_by_server())

    def test_fragments_drop(self):
        c = self.client()
        c.send_frame(OP_TEXT, b'{"type":', fin=False)
        self.assertTrue(c.closed_by_server())
        c2 = self.client()
        c2.send_frame(OP_CONT, b'"x"}')
        self.assertTrue(c2.closed_by_server())

    def test_reserved_bits_drop(self):
        c = self.client()
        data = bytearray(ws_server._frame(OP_TEXT, b'{"type":"x"}', mask=True))
        data[0] |= 0x40  # RSV1 = compressed
        c.write_raw(bytes(data))
        self.assertTrue(c.closed_by_server())

    def test_oversize_drops_without_reading(self):
        c = self.client()
        c.write_raw(bytes([0x81, 0x80 | 127]) + struct.pack("!Q", ws_server.MAX_MESSAGE + 1)
                    + b"\0\0\0\0")
        self.assertTrue(c.closed_by_server())

    def test_max_size_message_ok(self):
        c = self.hello_client()
        pad = ws_server.MAX_MESSAGE - len(b'{"type":"x","p":""}')
        c.send_frame(OP_TEXT, b'{"type":"x","p":"' + b"a" * pad + b'"}')
        c.send_frame(OP_PING, b"")
        self.assertEqual(c.read_frame(), (OP_PONG, b""))

    def test_three_bad_messages_drop(self):
        c = self.client()
        for bad in (b"bad", b"{]", b"\xff\xfe"):
            c.send_frame(OP_TEXT, bad)
        self.assertTrue(c.closed_by_server())
        self.assertTrue(wait_for(lambda: self.disconnects))

    def test_good_message_resets_bad_count(self):
        c = self.client()
        for p in (b"bad", b"bad", protocol.encode(HELLO).strip(), b"bad", b"bad"):
            c.send_frame(OP_TEXT, p)
        self.assertEqual(c.read()["type"], "hello")
        self.assertFalse(c.closed_by_server(0.3))

    def test_split_frames_over_tcp(self):
        c = self.client()
        data = ws_server._frame(OP_TEXT, protocol.encode(HELLO).strip(), mask=True)
        for i in range(0, len(data), 7):
            c.write_raw(data[i:i + 7])
            time.sleep(0.01)
        self.assertEqual(c.read()["type"], "hello")

    def test_disconnect_clears_tabs(self):
        c = self.hello_client()
        c.send({"type": "state", "tabs": [{"tab": 1, "site": "youtube"}]})
        self.assertTrue(wait_for(lambda: self.state.tabs(time.monotonic())))
        c.close()
        self.assertTrue(wait_for(lambda: not self.state.tabs(time.monotonic())))
        self.assertTrue(wait_for(lambda: self.server.client_count() == 0))

    def test_send_to_unknown_client(self):
        self.assertFalse(self.server.send(999, {"type": "x"}))


class PeerCheckTests(unittest.TestCase):
    """The real check: TCP table -> owning process -> exe name + user SID."""

    def run_with(self, check):
        server = WsServer([0], lambda c, m: None, peer_check=check)
        port = server.start()
        try:
            c = WsClient(port)
            ok = c.ok
            c.close()
            return ok
        finally:
            server.close()

    def test_own_exe_allowed(self):
        own = os.path.basename(sys.executable).lower()
        self.assertEqual(process_info(os.getpid())[0], own)
        self.assertTrue(self.run_with(process_check([own])))

    def test_brave_only_rejects_us(self):
        self.assertFalse(self.run_with(process_check(["brave.exe"])))

    def test_owner_pid_is_us(self):
        seen = []

        def check(pid):
            seen.append(pid)
            return True
        self.assertTrue(self.run_with(check))
        self.assertEqual(seen, [os.getpid()])

    def test_current_user_sid(self):
        sid = ws_server.current_user_sid()
        self.assertTrue(sid.startswith("S-1-"))
        self.assertEqual(process_info(os.getpid())[1], sid)

    def test_peer_check_required(self):
        with self.assertRaises(ValueError):
            WsServer([0], lambda c, m: None)


class BindTests(unittest.TestCase):
    def test_loopback_only_and_exclusive(self):
        a = WsServer([0], lambda c, m: None, peer_check=ALLOW)
        port = a.start()
        try:
            self.assertEqual(a._sock.getsockname()[0], "127.0.0.1")
            b = WsServer([port], lambda c, m: None, peer_check=ALLOW)
            with self.assertRaises(WsError):
                b.start()
            # a plain socket can't share it either (SO_EXCLUSIVEADDRUSE)
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    s.bind(("127.0.0.1", port))
            finally:
                s.close()
        finally:
            a.close()

    def test_falls_back_to_next_port(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        try:
            s = WsServer([taken, 0], lambda c, m: None, peer_check=ALLOW)
            port = s.start()
            self.assertNotEqual(port, taken)
            s.close()
        finally:
            blocker.close()

    def test_all_taken_raises(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        try:
            with self.assertRaises(WsError):
                WsServer([taken, taken], lambda c, m: None, peer_check=ALLOW).start()
        finally:
            blocker.close()

    def test_close_wakes_accept_at_once(self):
        s = WsServer([0], lambda c, m: None, peer_check=ALLOW)
        s.start()
        time.sleep(0.05)
        t = time.monotonic()
        s.close()
        self.assertLess(time.monotonic() - t, 0.3)   # not the 0.5 s accept timeout
        self.assertFalse(s._accept_thread.is_alive())

    def test_close_with_client_is_quick_and_frees_port(self):
        s = WsServer([0], lambda c, m: None, peer_check=ALLOW)
        port = s.start()
        c = WsClient(port)
        self.assertTrue(c.ok)
        self.assertTrue(wait_for(lambda: s.client_count() == 1))
        t = time.monotonic()
        s.close()
        self.assertLess(time.monotonic() - t, 2)
        self.assertTrue(c.closed_by_server())
        c.close()
        again = WsServer([port], lambda c, m: None, peer_check=ALLOW)
        self.assertEqual(again.start(), port)
        again.close()


class FrameTests(unittest.TestCase):
    def test_unmask_round_trip(self):
        for n in (0, 1, 3, 4, 5, 125, 126, 70000):
            data = os.urandom(n)
            key = os.urandom(4)
            self.assertEqual(ws_server._unmask(ws_server._unmask(data, key), key), data)

    def test_check_request_ok(self):
        head = (f"GET {ws_server.PATH} HTTP/1.1\r\nHost: 127.0.0.1:5\r\nUpgrade: WebSocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\nOrigin: {protocol.EXTENSION_ORIGIN}").encode()
        self.assertIsNone(ws_server.check_request(head, 5, protocol.EXTENSION_ORIGIN))
        self.assertEqual(ws_server.check_request(head.replace(b"GET", b"POST"), 5,
                                                 protocol.EXTENSION_ORIGIN), "method")
        self.assertEqual(ws_server.check_request(head, 6, protocol.EXTENSION_ORIGIN), "host")


if __name__ == "__main__":
    unittest.main()
