"""What the Brave extension hosts report: their YouTube / YouTube Music tabs."""
import logging
import threading

from . import protocol

log = logging.getLogger("knob.browser")

STALE_AFTER = 5.0


class Tab:
    __slots__ = ("client", "tab", "site", "playing", "volume", "muted", "max_volume", "boost")

    def __init__(self, client, tab, site, playing, volume, muted, max_volume=1.0, boost="off"):
        self.client, self.tab, self.site = client, tab, site
        self.playing, self.volume, self.muted = playing, volume, muted
        self.max_volume, self.boost = max_volume, boost

    def __repr__(self):
        return f"Tab({self.client}:{self.tab} {self.site} playing={self.playing})"


class BrowserState:
    """Thread-safe store of tab states per connected host."""

    def __init__(self, stale_after=STALE_AFTER):
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._hosts = {}  # client id -> (time of last state, [tab dicts])

    def update(self, client, tabs, now):
        with self._lock:
            self._hosts[client] = (now, tabs)

    def drop(self, client):
        with self._lock:
            self._hosts.pop(client, None)

    def connected(self, now):
        """True if any host sent a state within the last `stale_after` s."""
        with self._lock:
            return any(now - t <= self.stale_after for t, _ in self._hosts.values())

    def tabs(self, now):
        with self._lock:
            out = []
            for client, (t, tabs) in self._hosts.items():
                if now - t > self.stale_after:
                    continue
                for d in tabs:
                    out.append(Tab(client, d["tab"], d["site"], d["playing"], d["volume"],
                                   d["muted"], d.get("max_volume", 1.0), d.get("boost", "off")))
            return out


class BrowserBridge:
    """Handles messages from hosts and sends commands back.

    `send(client, msg_dict)` is supplied by the pipe server. `events(tuple)`,
    if set, gets ("tabs", client, tabs) and ("gone", client) for the
    controller thread (failsafe)."""

    def __init__(self, state, clock, send=None, events=None):
        self.state = state
        self.clock = clock
        self.send = send
        self.events = events
        self._features = {}  # clients that said a matching hello -> their features
        self._lock = threading.Lock()

    def on_connect(self, client):
        log.info("browser host %s connected", client)

    def on_disconnect(self, client):
        with self._lock:
            self._features.pop(client, None)
        self.state.drop(client)
        if self.events:
            self.events(("gone", client))
        log.info("browser host %s disconnected", client)

    def on_message(self, client, msg):
        kind = msg.get("type")
        if kind == "hello":
            if protocol.parse_hello(msg):
                with self._lock:
                    self._features[client] = protocol.parse_features(msg)
                if self.send:
                    self.send(client, protocol.hello())
            else:
                log.warning("browser host %s speaks an unknown protocol; ignored", client)
            return
        with self._lock:
            ok = client in self._features
        if not ok:
            return
        if kind == "state":
            tabs = protocol.parse_state(msg)
            if tabs is not None:
                self.state.update(client, tabs, self.clock())
                if self.events:
                    self.events(("tabs", client, tabs))
        # anything else: ignored (forward compatible)

    def features(self, client):
        with self._lock:
            return self._features.get(client)

    def command(self, client, msg):
        with self._lock:
            ok = client in self._features
        if ok and self.send:
            self.send(client, msg)
