"""Real named pipe on a random test name (never the program's own pipe)."""
import time
import unittest
import uuid

from knob import pipe_server, protocol
from knob.browser import BrowserBridge, BrowserState
from knob.pipe_server import PipeClient, PipeError, PipeServer


def wait_for(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


HELLO = {"type": "hello", "protocol": 2, "host_version": "test", "features": ["boost", "set_volume"]}


class PipeTests(unittest.TestCase):
    def setUp(self):
        self.name = r"\\.\pipe\8bitdo-volume-knob-test-" + uuid.uuid4().hex
        self.state = BrowserState()
        self.bridge = BrowserBridge(self.state, time.monotonic)
        self.disconnects = []
        self.server = PipeServer(self.name, self.bridge.on_message, self.bridge.on_connect,
                                 self._on_disconnect)
        self.bridge.send = self.server.send
        self.server.start()
        self.clients = []

    def _on_disconnect(self, cid):
        self.disconnects.append(cid)
        self.bridge.on_disconnect(cid)

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.close()

    def client(self):
        c = PipeClient(self.name)
        self.clients.append(c)
        return c

    def test_hello_state_and_command(self):
        c = self.client()
        c.send(HELLO)
        reply = c.read()
        self.assertEqual((reply["type"], reply["protocol"]), ("hello", 2))
        c.send({"type": "state", "tabs": [
            {"tab": 3, "site": "ytmusic", "playing": True, "volume": 0.2, "muted": False}]})
        self.assertTrue(wait_for(lambda: self.state.tabs(time.monotonic())))
        tab = self.state.tabs(time.monotonic())[0]
        self.bridge.command(tab.client, protocol.mute(True, tabs=[3]))
        self.assertEqual(c.read(), {"type": "mute", "muted": True, "tabs": [3]})

    def test_split_and_joined_lines(self):
        c = self.client()
        data = protocol.encode(HELLO) + protocol.encode(
            {"type": "state", "tabs": [{"tab": 1, "site": "youtube"}]})
        c.write_raw(data[:10])
        time.sleep(0.05)
        c.write_raw(data[10:])
        self.assertEqual(c.read()["type"], "hello")
        self.assertTrue(wait_for(lambda: self.state.tabs(time.monotonic())))

    def test_three_bad_lines_drop_client(self):
        c = self.client()
        c.write_raw(b"bad\n{]\nnot json\n")
        self.assertTrue(c.closed_by_server())
        self.assertTrue(wait_for(lambda: self.disconnects))

    def test_good_line_resets_bad_count(self):
        c = self.client()
        c.write_raw(b"bad\nbad\n" + protocol.encode(HELLO) + b"bad\nbad\n")
        self.assertEqual(c.read()["type"], "hello")
        self.assertFalse(c.closed_by_server(0.3))

    def test_oversize_line_does_not_crash(self):
        c = self.client()
        c.write_raw(b"x" * (protocol.MAX_LINE + 10) + b"\n")
        c.send(HELLO)
        self.assertEqual(c.read()["type"], "hello")

    def test_disconnect_clears_tabs(self):
        c = self.client()
        c.send(HELLO)
        c.read()
        c.send({"type": "state", "tabs": [{"tab": 1, "site": "youtube"}]})
        self.assertTrue(wait_for(lambda: self.state.tabs(time.monotonic())))
        c.close()
        self.assertTrue(wait_for(lambda: not self.state.tabs(time.monotonic())))
        self.assertTrue(wait_for(lambda: self.server.client_count() == 0))

    def test_several_clients(self):
        cs = [self.client() for _ in range(3)]
        for c in cs:
            c.send(HELLO)
        for c in cs:
            self.assertEqual(c.read()["type"], "hello")
        self.assertEqual(self.server.client_count(), 3)

    def test_first_instance_only(self):
        other = PipeServer(self.name, lambda c, m: None)
        with self.assertRaises(PipeError):
            other.start()

    def test_send_to_unknown_client(self):
        self.assertFalse(self.server.send(999, {"type": "x"}))

    def test_current_user_sid(self):
        self.assertTrue(pipe_server.current_user_sid().startswith("S-1-"))


class CloseTests(unittest.TestCase):
    def test_close_with_connected_client_is_quick(self):
        name = r"\\.\pipe\8bitdo-volume-knob-test-" + uuid.uuid4().hex
        server = PipeServer(name, lambda c, m: None)
        server.start()
        c = PipeClient(name)
        self.assertTrue(wait_for(lambda: server.client_count() == 1))
        t = time.monotonic()
        server.close()
        self.assertLess(time.monotonic() - t, 2)
        self.assertTrue(c.closed_by_server())
        c.close()
        # name is free again
        again = PipeServer(name, lambda c, m: None)
        again.start()
        again.close()


if __name__ == "__main__":
    unittest.main()
