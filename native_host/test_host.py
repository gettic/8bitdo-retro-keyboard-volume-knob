"""Tests for the native-messaging host (built-in unittest, no extra packages).

Runs the real host as a child process (no window), talks to it like Brave does
on stdin/stdout, and plays the program on a random test pipe. Never touches the
real pipe, the registry or the user's settings.

    python -m unittest native_host.test_host        (from code\\)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import volume_knob_host as vh  # noqa: E402

k32 = vh.kernel32
k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                 wt.DWORD, ctypes.c_void_p]
k32.CreateNamedPipeW.restype = wt.HANDLE
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.ConnectNamedPipe.restype = wt.BOOL
CREATE_NO_WINDOW = 0x08000000


class FakeProgram:
    """A pipe server playing the volume knob program. Accepts clients one after another."""

    def __init__(self, name):
        self.name = name
        self.lines = queue.Queue()
        self.conn = None
        self.connections = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            h = k32.CreateNamedPipeW(self.name, 3 | vh.FILE_FLAG_OVERLAPPED, 0, 4, 65536, 65536,
                                     0, None)
            if h == vh.INVALID_HANDLE_VALUE:
                time.sleep(0.2)
                continue
            pipe = vh.Pipe(h)
            ov = vh.OVERLAPPED()
            ev = k32.CreateEventW(None, True, False, None)
            ov.hEvent = ev
            connected = False
            if not k32.ConnectNamedPipe(h, ctypes.byref(ov)):
                err = ctypes.get_last_error()
                if err == 535:  # ERROR_PIPE_CONNECTED
                    connected = True
                elif err == vh.ERROR_IO_PENDING:
                    while not self.stop.is_set():
                        if k32.WaitForSingleObject(ev, 200) == vh.WAIT_OBJECT_0:
                            got = wt.DWORD()
                            connected = bool(k32.GetOverlappedResult(h, ctypes.byref(ov),
                                                                     ctypes.byref(got), False))
                            break
                    else:
                        k32.CancelIoEx(h, ctypes.byref(ov))
            k32.CloseHandle(ev)
            if not connected:
                pipe.close()
                continue
            self.connections += 1
            self.conn = pipe
            pending = b""
            while not self.stop.is_set():
                data = pipe.read_some(0.2)
                if data is None:
                    break
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.lines.put(json.loads(line))
            self.conn = None
            pipe.close()

    def send(self, msg):
        deadline = time.time() + 5
        while self.conn is None and time.time() < deadline:
            time.sleep(0.05)
        return self.conn.write_line(msg)

    def drop_client(self):
        c = self.conn
        if c:
            c.fail()

    def next(self, kind, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.lines.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("type") == kind:
                return msg
        raise AssertionError(f"no {kind!r} message from the host within {timeout} s")

    def close(self):
        self.stop.set()
        self.drop_client()
        self.thread.join(3)


class HostProcess:
    """The real host, started the way Brave starts it."""

    def __init__(self, pipe_name, data_dir):
        env = dict(os.environ, VOLUME_KNOB_PIPE=pipe_name, VOLUME_KNOB_DATA=data_dir)
        self.p = subprocess.Popen(
            [sys.executable, "-I", os.path.join(HERE, "volume_knob_host.py"),
             "chrome-extension://ocheindfkmmnaekoolhggndpapplccpd/", "--parent-window=0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
            creationflags=CREATE_NO_WINDOW)
        self.out = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            head = self.p.stdout.read(4)
            if len(head) < 4:
                return
            size = struct.unpack("<I", head)[0]
            self.out.put(json.loads(self.p.stdout.read(size)))

    def send(self, msg):
        data = json.dumps(msg).encode()
        self.p.stdin.write(struct.pack("<I", len(data)) + data)
        self.p.stdin.flush()

    def send_raw(self, data):
        self.p.stdin.write(struct.pack("<I", len(data)) + data)
        self.p.stdin.flush()

    def next(self, timeout=5.0):
        return self.out.get(timeout=timeout)

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            return self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return None
        finally:
            self.p.stdout.close()


TAB = {"tab": 7, "site": "youtube", "playing": True, "volume": 0.5, "muted": False}


class CheckTests(unittest.TestCase):
    def test_state_is_cleaned(self):
        st = vh.clean_state({"type": "state", "tabs": [
            TAB, {"tab": "x", "site": "youtube", "volume": 1}, {"tab": 2, "site": "evil", "volume": 1},
            {"tab": 3, "site": "ytmusic", "volume": 7, "playing": 1, "title": "secret"}]})
        self.assertEqual(st["tabs"], [TAB, {"tab": 3, "site": "ytmusic", "playing": False,
                                            "volume": 1.5, "muted": False}])
        self.assertIsNone(vh.clean_state({"type": "state"}))

    def test_state_v2_fields(self):
        st = vh.clean_state({"type": "state", "tabs": [
            {"tab": 1, "site": "youtube", "volume": None},
            {"tab": 2, "site": "youtube", "volume": 1.2, "max_volume": 1.5, "boost": "on"},
            {"tab": 3, "site": "youtube", "volume": 1.0, "max_volume": 9, "boost": "weird"},
            {"tab": 4, "site": "youtube", "volume": "loud"}]})
        self.assertEqual(st["tabs"], [
            {"tab": 1, "site": "youtube", "playing": False, "volume": None, "muted": False},
            {"tab": 2, "site": "youtube", "playing": False, "volume": 1.2, "muted": False,
             "max_volume": 1.5, "boost": "on"},
            {"tab": 3, "site": "youtube", "playing": False, "volume": 1.0, "muted": False,
             "max_volume": 1.5}])

    def test_commands_are_cleaned(self):
        self.assertEqual(vh.clean_command({"type": "volume", "site": "all", "delta": 0.04}),
                         {"type": "volume", "site": "all", "delta": 0.04, "max": 1.0})
        self.assertEqual(vh.clean_command({"type": "volume", "tabs": [1, "x", True], "delta": 9, "max": 7}),
                         {"type": "volume", "tabs": [1], "delta": 1.5, "max": 1.5})
        self.assertEqual(vh.clean_command({"type": "volume", "tabs": [1], "delta": 0.04, "max": 1.3}),
                         {"type": "volume", "tabs": [1], "delta": 0.04, "max": 1.3})
        self.assertEqual(vh.clean_command({"type": "set_volume", "tabs": [1], "volume": 0.5, "muted": True}),
                         {"type": "set_volume", "tabs": [1], "volume": 0.5})
        self.assertEqual(vh.clean_command({"type": "set_volume", "site": "all", "volume": 3}),
                         {"type": "set_volume", "site": "all", "volume": 1.5})
        self.assertIsNone(vh.clean_command({"type": "set_volume", "tabs": [1], "volume": None}))
        self.assertEqual(vh.clean_command({"type": "mute", "site": "ytmusic", "muted": True}),
                         {"type": "mute", "site": "ytmusic", "muted": True})
        self.assertIsNone(vh.clean_command({"type": "volume", "site": "all", "delta": float("nan")}))
        self.assertIsNone(vh.clean_command({"type": "volume", "site": "spotify", "delta": 0.1}))
        self.assertIsNone(vh.clean_command({"type": "hello", "protocol": 1}))
        self.assertIsNone(vh.clean_command({"type": "shutdown"}))
        self.assertEqual(vh.clean_command({"type": "probe", "tabs": [3]}),
                         {"type": "probe", "tabs": [3]})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.pipe_name = r"\\.\pipe\8bitdo-volume-knob-test-" + uuid.uuid4().hex
        self.tmp = tempfile.TemporaryDirectory(prefix="vk_host_test_")
        self.prog = None
        self.host = None

    def tearDown(self):
        if self.host:
            self.host.close()
        if self.prog:
            self.prog.close()
        self.tmp.cleanup()

    def test_full_bridge(self):
        # Host first, program not running yet: it must keep retrying calmly.
        self.host = HostProcess(self.pipe_name, self.tmp.name)
        self.host.send({"type": "hello", "ext": "1.0.0", "features": ["boost", "set_volume", "x"]})
        self.host.send({"type": "state", "tabs": [TAB]})
        time.sleep(4.5)
        self.assertIsNone(self.host.p.poll(), "host quit while the program was away")

        # Program appears: the host must find it within 2 s.
        t0 = time.time()
        self.prog = FakeProgram(self.pipe_name)
        hello = self.prog.next("hello", 6)
        self.assertLess(time.time() - t0, 2.6)
        self.assertEqual((hello["protocol"], hello["host_version"], hello["ext_version"], hello["features"]),
                         (2, vh.HOST_VERSION, "1.0.0", ["boost", "set_volume"]))
        self.assertEqual(self.prog.next("state")["tabs"], [TAB])
        self.prog.send({"type": "hello", "protocol": 2, "version": "1.0.0"})

        # Heartbeat: the full state again within ~2 s without any change.
        t0 = time.time()
        self.prog.next("state", 3.5)
        self.assertLess(time.time() - t0, 3.0)

        # Changes go through right away.
        changed = dict(TAB, volume=0.54)
        self.host.send({"type": "state", "tabs": [changed]})
        self.assertEqual(self.prog.next("state", 1.5)["tabs"], [changed])

        # Commands: forwarded cleaned, junk dropped, hello not forwarded.
        self.prog.send({"type": "volume", "site": "youtube", "delta": 0.04, "extra": 1})
        self.assertEqual(self.host.next(), {"type": "volume", "site": "youtube", "delta": 0.04, "max": 1.0})
        self.prog.send({"type": "set_volume", "tabs": [7], "volume": 0.5})
        self.assertEqual(self.host.next(), {"type": "set_volume", "tabs": [7], "volume": 0.5})
        self.prog.conn.write_line({"type": "weird"})
        self.prog.send({"type": "mute", "tabs": [7], "muted": True})
        self.assertEqual(self.host.next(), {"type": "mute", "tabs": [7], "muted": True})

        # Bad input from the browser side is ignored.
        self.host.send_raw(b"{not json")
        self.host.send({"type": "state", "tabs": "nope"})
        self.assertIsNone(self.host.p.poll())

        # Program drops the connection: the host reconnects (within 2 s) and says hello again.
        t0 = time.time()
        self.prog.drop_client()
        self.prog.next("hello", 8)
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.prog.next("state")["tabs"], [changed])
        self.assertGreaterEqual(self.prog.connections, 2)

        # Brave closes the port: the host ends cleanly.
        code = self.host.close()
        self.host = None
        self.assertEqual(code, 0)

    def test_exit_while_program_missing(self):
        self.host = HostProcess(self.pipe_name, self.tmp.name)
        time.sleep(1.0)
        code = self.host.close()
        self.host = None
        self.assertEqual(code, 0)

    def test_not_started_by_browser(self):
        self.assertEqual(vh.run(["volume_knob_host.py"]), 2)
        self.assertEqual(vh.run([]), 2)

    def test_through_the_program_entry(self):
        """The same path the .exe takes: volume_knob.py main() with sys.argv[1:]."""
        code_dir = os.path.dirname(HERE)
        env = dict(os.environ, VOLUME_KNOB_PIPE=self.pipe_name, VOLUME_KNOB_DATA=self.tmp.name)
        p = subprocess.Popen([sys.executable, "-I", os.path.join(code_dir, "volume_knob.py"),
                              "chrome-extension://ocheindfkmmnaekoolhggndpapplccpd/",
                              "--parent-window=0"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                             creationflags=CREATE_NO_WINDOW)
        self.prog = FakeProgram(self.pipe_name)
        try:
            self.assertEqual(self.prog.next("hello", 8)["protocol"], vh.PROTOCOL)
        finally:
            p.stdin.close()
            code = p.wait(5)
            p.stdout.close()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
