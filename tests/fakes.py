"""In-memory stand-ins for Core Audio, the clock and the browser link."""
from knob import config
from knob.browser import BrowserBridge, BrowserState


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeSession:
    def __init__(self, exe, volume=0.5, muted=False, peak=0.0, pid=100):
        self.exe, self._volume, self._muted, self._peak, self.pid = exe, volume, muted, peak, pid
        self.released = False
        self.fail = False

    def _check(self):
        if self.fail:
            raise OSError("session gone")

    def volume(self):
        self._check()
        return self._volume

    def set_volume(self, v):
        self._check()
        self._volume = min(1.0, max(0.0, v))

    def muted(self):
        self._check()
        return self._muted

    def set_mute(self, m):
        self._check()
        self._muted = bool(m)

    def peak(self):
        self._check()
        return self._peak

    def release(self):
        self.released = True


class FakeAudio:
    """sessions() hands out the same FakeSession objects every time; like
    coreaudio.CoreAudio it owns them (released by reset() / close())."""

    def __init__(self, sessions):
        self.all = sessions
        self.closed = False
        self.calls = 0
        self.resets = 0

    def sessions(self, apps=None):
        self.calls += 1
        for s in self.all:
            s.released = False
        return [s for s in self.all if apps is None or s.exe in apps]

    def reset(self):
        self.resets += 1
        for s in self.all:
            s.release()

    def close(self):
        self.reset()
        self.closed = True


class Recorder:
    """Records what the program would send to hosts."""

    def __init__(self):
        self.sent = []

    def __call__(self, client, msg):
        self.sent.append((client, msg))
        return True


def settings(**over):
    s = dict(config.DEFAULTS)
    s.update(over)
    return s


def make_bridge(clock):
    rec = Recorder()
    bridge = BrowserBridge(BrowserState(), clock, rec)
    return bridge, rec


FEATURES = ["boost", "set_volume"]


def connect_host(bridge, client, tabs, features=FEATURES):
    bridge.on_message(client, {"type": "hello", "protocol": 2, "features": features})
    bridge.on_message(client, {"type": "state", "tabs": tabs})


class ProcessList:
    """Fake running-process list."""

    def __init__(self, running=()):
        self.running = set(running)

    def __call__(self, names):
        return self.running & set(names)
