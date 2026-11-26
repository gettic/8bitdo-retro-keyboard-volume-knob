"""Failsafe: a YouTube / YouTube Music tab louder than 100 % goes down to 50 %.

Checked only
  a) for tabs first seen within `start_window` seconds after program start, and
  b) for a tab that opens while no other target is open (no Spotify, no other tab).
Windows apps (Spotify) can never be above 100 %, so they are only used to
decide "another app is open". Pure logic, no Windows calls."""

TRIGGER = 1.0
EPS = 1e-6


class Failsafe:
    def __init__(self, start, level=0.5, start_window=20.0, memory=30.0, enabled=True):
        self.start = start
        self.level = level
        self.start_window = start_window
        self.memory = memory
        self.enabled = enabled
        self._known = {}     # (site, tab) -> time last seen
        self._current = {}   # client -> set of (site, tab) in its latest state
        self._pending = set()  # (site, tab) to check once its volume is known

    def host_gone(self, client):
        self._current.pop(client, None)

    def on_tabs(self, client, tabs, now, apps_open):
        """tabs: list of tab dicts from one host's state. apps_open: callable
        -> bool (is any Windows target app open). Returns tab ids to set to
        `level`."""
        for k, t in list(self._known.items()):
            if now - t > self.memory:
                del self._known[k]
        keys = [(t["site"], t["tab"]) for t in tabs]
        new = {k for k in keys if k not in self._known}
        reset = []
        if self.enabled and tabs:
            in_window = now - self.start <= self.start_window
            others = None  # computed lazily
            for t, k in zip(tabs, keys):
                check = False
                if k in new:
                    if in_window:
                        check = True
                    else:
                        if others is None:
                            others = self._others_open(client, keys, new, apps_open)
                        check = not others
                    if check and t["volume"] is None:
                        self._pending.add(k)
                        check = False
                elif k in self._pending and t["volume"] is not None:
                    self._pending.discard(k)
                    check = True
                if check and t["volume"] > TRIGGER + EPS:
                    reset.append(t["tab"])
        for k in keys:
            self._known[k] = now
        self._current[client] = set(keys)
        self._pending &= {k for ks in self._current.values() for k in ks}
        return reset

    def _others_open(self, client, keys, new, apps_open):
        for c, ks in self._current.items():
            if c != client and ks:
                return True
        if any(k not in new for k in keys):  # already-known tabs of this host
            return True
        return bool(apps_open())
