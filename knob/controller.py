"""Audio thread: polls sound levels, and applies knob keys to apps and tabs."""
import logging
import queue
import time

from . import display as view
from . import protocol, router
from .activity import ActivityTracker
from .failsafe import Failsafe

log = logging.getLogger("knob.controller")

BRAVE = "brave.exe"
POLL_INTERVAL = 0.2
REFRESH_INTERVAL = 2.0
GUESS_SECONDS = 1.0  # a tab's predicted volume wins over its (older) reported state this long


def _running_exes(names):
    from . import coreaudio  # looked up at call time (tests replace it)
    return coreaudio.running_exes(names)


class Controller:
    """`audio_factory()` must return an object with sessions(apps) and close();
    it is called on the controller's own thread (COM is per thread)."""

    def __init__(self, settings, audio_factory, bridge, clock=time.monotonic,
                 process_lister=_running_exes, display=None):
        self.settings = settings
        # display(rows, popup): pop-up / tray icon sink (ui.Ui.show), called on this thread
        self.display = display
        self._shown = None      # sources the knob changed last: ("app", exe) / ("tab", client, id)
        self._guess = {}        # (client, tab id) -> (level, muted, time) predicted after a step
        self._last_rows = None
        self.audio_factory = audio_factory
        self.bridge = bridge
        self.clock = clock
        self.process_lister = process_lister
        self.activity = ActivityTracker(settings["peak_threshold"], settings["hold_seconds"])
        self.failsafe = Failsafe(clock(), settings["failsafe_level"],
                                 settings["failsafe_start_window"], enabled=settings["failsafe"])
        self.keys = queue.Queue()
        self.audio = None
        self._sessions = []
        self._refreshed = None
        self._warned = set()
        bridge.events = self.keys.put  # tab states / host gone -> this thread

    # --- called from other threads ---
    def press(self, action):
        self.keys.put(action)

    def stop(self):
        self.keys.put(None)

    # --- controller thread ---
    def run(self):
        self.audio = self.audio_factory()
        try:
            while True:
                try:
                    action = self.keys.get(timeout=POLL_INTERVAL)
                except queue.Empty:
                    action = ""
                if action is None:
                    break
                try:
                    self.poll()
                    if isinstance(action, tuple):
                        self.on_event(action)
                    elif action:
                        self.handle(action)
                except Exception:  # keep running whatever happens
                    log.exception("error in audio thread")
                    self._drop_sessions()
        finally:
            self._drop_sessions()
            self.audio.close()

    def _wanted_apps(self):
        apps = set(self.settings["apps"])
        if self.settings["brave_fallback"]:
            apps.add(BRAVE)
        return apps

    def _drop_sessions(self):
        for s in self._sessions:
            try:
                s.release()
            except OSError:
                pass
        self._sessions = []
        self._refreshed = None

    def refresh(self, force=False):
        """Rebuild the session list every REFRESH_INTERVAL s. True if it did."""
        now = self.clock()
        if not force and self._refreshed is not None and now - self._refreshed < REFRESH_INTERVAL:
            return False
        self._drop_sessions()
        self._sessions = self.audio.sessions(self._wanted_apps())
        self._refreshed = now
        self.activity.forget_except({s.exe for s in self._sessions})
        return True

    def poll(self):
        if self.refresh():
            self.update_display()  # volume changed elsewhere (Spotify's own slider...)
        now = self.clock()
        for s in self._sessions:
            try:
                self.activity.update(s.exe, s.peak(), now)
            except OSError:
                self._refreshed = None  # session gone: rebuild next time

    def _active_sessions(self, now):
        """Sessions the knob may change right now."""
        brave_by_extension = self.bridge.state.connected(now)
        allowed = set(self.settings["apps"])
        if self.settings["brave_fallback"] and not brave_by_extension:
            allowed.add(BRAVE)
        if brave_by_extension:
            allowed.discard(BRAVE)  # never touch Brave's sound while the extension does it
        return [s for s in self._sessions if s.exe in allowed]

    def handle(self, action):
        now = self.clock()
        sessions = self._active_sessions(now)
        by_exe = {}
        for s in sessions:
            by_exe.setdefault(s.exe, []).append(s)
        apps = []
        for exe, group in by_exe.items():
            try:
                muted = all(s.muted() for s in group)
            except OSError:
                muted = False
            apps.append(router.AppSource(exe, self.activity.is_playing(exe, now), muted))
        tabs = self.bridge.state.tabs(now)
        d = router.decide(action, apps, tabs, self.settings["step"])
        log.debug("key %s -> %r", action, d)
        for exe in d.apps:
            for s in by_exe[exe]:
                try:
                    if d.muted is not None:
                        s.set_mute(d.muted)
                    else:
                        if s.muted():
                            s.set_mute(False)
                        s.set_volume(min(1.0, max(0.0, s.volume() + d.delta)))
                except OSError:
                    self._refreshed = None
        by_key = {(t.client, t.tab): t for t in tabs}
        for client, ids in d.tabs.items():
            self._guess_tabs(client, ids, by_key, d, now)
            if d.muted is not None:
                self.bridge.command(client, protocol.mute(d.muted, tabs=ids))
                continue
            groups = {}
            for tid in ids:
                groups.setdefault(self._tab_max(client, by_key[(client, tid)]), []).append(tid)
            for mx, group in sorted(groups.items()):
                self.bridge.command(client, protocol.volume(d.delta, tabs=group, max_volume=mx))
        if not d.empty():
            self._shown = self._sources(d, by_key)
            self.update_display(popup=True)
        return d

    # --- what the pop-up and the tray icon show ---
    def _guess_tabs(self, client, ids, by_key, d, now):
        """Tab states arrive after the extension applied a step, so the shown
        volume is predicted the way the extension computes it."""
        self._guess = {k: g for k, g in self._guess.items() if now - g[2] < GUESS_SECONDS}
        for tid in ids:
            tab = by_key[(client, tid)]
            base = self._tab_level(client, tab, now)
            if base is None:
                continue
            level, muted = base
            if d.muted is not None:
                self._guess[(client, tid)] = (level, d.muted, now)
            else:
                new = view.predict_tab(level, d.delta, self._tab_max(client, tab))
                self._guess[(client, tid)] = (new, False, now)  # a volume step unmutes

    @staticmethod
    def _sources(d, by_key):
        """Apps first, then one tab per site (YouTube, YouTube Music)."""
        out = [("app", exe) for exe in d.apps]
        sites = set()
        for client in sorted(d.tabs):
            for tid in d.tabs[client]:
                site = by_key[(client, tid)].site
                if site not in sites:
                    sites.add(site)
                    out.append(("tab", client, tid))
        return out

    def _tab_level(self, client, tab, now):
        """(level, muted) of a tab: the recent prediction, else its reported state."""
        g = self._guess.get((client, tab.tab))
        if g and now - g[2] < GUESS_SECONDS:
            return g[0], g[1]
        if tab.volume is None:
            return None
        return tab.volume, bool(tab.muted)

    # One small reader per source, so a different way of setting a source's
    # volume only needs its reader changed.
    @staticmethod
    def _read_app(exe, group):
        """Row for a Windows app from its audio sessions (Core Audio volume)."""
        if not group:
            return None
        try:
            return view.Row(view.name_for(exe), group[0].volume(),
                            all(s.muted() for s in group), 1.0)
        except OSError:
            return None

    def _read_tab(self, client, tid, tab, now):
        """Row for a YouTube / YouTube Music tab (prediction or reported state)."""
        if tab is None:
            return None
        lv = self._tab_level(client, tab, now)
        if lv is None:
            return None
        return view.Row(view.name_for(tab.site), lv[0], lv[1], self._tab_max(client, tab))

    def rows(self, now=None):
        """What to show now: the sources the knob changed last, or before the
        first turn the Windows apps that are open (Spotify)."""
        now = self.clock() if now is None else now
        groups = {}
        for s in self._active_sessions(now):
            groups.setdefault(s.exe, []).append(s)
        sources = self._shown
        if sources is None:
            sources = [("app", exe) for exe in sorted(groups) if exe != BRAVE]
        tabs = {(t.client, t.tab): t for t in self.bridge.state.tabs(now)}
        out = []
        for src in sources:
            if src[0] == "app":
                row = self._read_app(src[1], groups.get(src[1]))
            else:
                row = self._read_tab(src[1], src[2], tabs.get((src[1], src[2])), now)
            if row is not None:
                out.append(row)
        if not out and self._shown is not None:
            self._shown = None  # what the knob changed last is gone: back to the open apps
            return self.rows(now)
        return out

    def update_display(self, popup=False):
        """Send the rows to the display: always for a knob turn (popup), else
        only when something changed (tray icon / open pop-up follow along)."""
        if not self.display:
            return
        rows = self.rows()
        if not popup and rows == self._last_rows:
            return
        self._last_rows = rows
        try:
            self.display(rows, popup)
        except Exception:  # the display never stops the knob
            log.exception("display update failed")

    def _tab_max(self, client, tab):
        """Highest volume we allow this tab: boost_max for boost-capable hosts,
        1.0 when the host can't boost or the tab's boost is impossible.
        ("blocked" keeps boost_max: the extension retries on the next step.)"""
        features = self.bridge.features(client) or ()
        if "boost" not in features or tab.boost == "unavailable":
            return 1.0
        return self.settings["boost_max"]

    # --- failsafe ---
    def _apps_open(self):
        apps = set(self.settings["apps"]) - {BRAVE}
        if any(s.exe in apps for s in self._sessions):
            return True
        try:
            return bool(self.process_lister(apps))
        except OSError:
            return False

    def on_event(self, event):
        now = self.clock()
        if event[0] == "gone":
            self.failsafe.host_gone(event[1])
            self.update_display()
            return
        _, client, tabs = event
        self.update_display()  # a tab's volume may have changed in the page itself
        for t in tabs:
            key = (client, t["tab"])
            if t["boost"] == "error" and key not in self._warned:
                self._warned.add(key)
                log.warning("boost stopped working in a browser tab (host %s); "
                            "the tab may need a reload", client)
        ids = self.failsafe.on_tabs(client, tabs, now, self._apps_open)
        if not ids:
            return
        features = self.bridge.features(client) or ()
        if "set_volume" not in features:
            log.warning("failsafe: host %s cannot set volume; %d tab(s) left loud",
                        client, len(ids))
            return
        log.info("failsafe: %d tab(s) over 100%% set to %d%%", len(ids),
                 round(self.failsafe.level * 100))
        self.bridge.command(client, protocol.set_volume(self.failsafe.level, tabs=ids))
