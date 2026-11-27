"""Audio thread: polls sound levels, and applies knob keys to apps and tabs."""
import logging
import queue
import threading
import time

from . import display as view
from . import protocol, router
from .activity import ActivityTracker
from .failsafe import Failsafe

log = logging.getLogger("knob.controller")

BRAVE = "brave.exe"
SPOTIFY = "spotify.exe"
POLL_INTERVAL = 0.2
REFRESH_INTERVAL = 2.0
GUESS_SECONDS = 1.0  # a tab's predicted volume wins over its (older) reported state this long
MAX_SECONDS = 1.5    # an app turned up at 100 % shows "Max" on the pop-up this long


def _running_exes(names):
    from . import coreaudio  # looked up at call time (tests replace it)
    return coreaudio.running_exes(names)


class Controller:
    """`audio_factory()` must return an object with sessions(apps), close()
    and optionally reset(); it is called on the controller's own thread (COM
    is per thread). The audio object owns the sessions it hands out (it
    releases them); the controller never releases them itself."""

    def __init__(self, settings, audio_factory, bridge, clock=time.monotonic,
                 process_lister=_running_exes, display=None, spotify=None,
                 spotify_factory=None):
        self.settings = settings
        # spotify: spotify_api.SpotifyVolume (Spotify's own bar) or None (mixer only);
        # spotify_factory() makes a new one when the settings window switches to "web"
        self.spotify = spotify
        self.spotify_factory = spotify_factory
        if spotify is not None:
            spotify.on_change = self._spotify_changed
        # display(rows, popup): pop-up / tray icon sink (ui.Ui.show), called on this thread
        self.display = display
        self._shown = None      # sources the knob changed last: ("app", exe) / ("tab", client, id)
        self._guess = {}        # (client, tab id) -> (level, muted, time) predicted after a step
        self._at_max = {}       # exe -> time it was turned up while at 100 % ("Max" on the card)
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
        self._audio_failing = False
        self._rebuild = False   # next refresh: audio.reset() first (a kept object failed)
        self._brave_orig = {}   # Brave pid -> (volume, muted) before the fallback changed it
        self._quiet = set()     # exes the knob muted / turned to 0 % (stay the target, see _targets)
        bridge.events = self.keys.put  # tab states / host gone -> this thread

    def _spotify_changed(self):
        self.keys.put(("spotify",))

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
                except Exception:  # keep running whatever happens
                    log.exception("error in audio thread")
                    self._drop_sessions()
                if not action:
                    continue
                try:  # the key still counts when the poll failed (tabs need no Core Audio)
                    if isinstance(action, tuple):
                        self.on_event(action)
                    else:
                        self.handle(action)
                except Exception:
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
        """Forget the session list; the audio object (which owns the sessions)
        starts clean on the next refresh."""
        self._sessions = []
        self._session_error()

    def _session_error(self):
        """A session call failed (app closed, device unplugged...): list again
        on the next poll, with every kept Core Audio object made new."""
        self._refreshed = None
        self._rebuild = True

    def refresh(self, force=False):
        """Re-list the sessions every REFRESH_INTERVAL s. True if it did."""
        now = self.clock()
        if not force and self._refreshed is not None and now - self._refreshed < REFRESH_INTERVAL:
            return False
        if self._rebuild:
            self._rebuild = False
            reset = getattr(self.audio, "reset", None)
            if reset:
                try:
                    reset()
                except OSError:
                    pass
        try:
            self._sessions = self.audio.sessions(self._wanted_apps())
        except OSError as e:
            # Windows audio not answering (service restart, driver update, resume):
            # no apps for now, tabs still work; retried on the normal 2 s refresh
            if not self._audio_failing:
                log.warning("Windows audio not available (%s); retrying every %g s",
                            e, REFRESH_INTERVAL)
            self._audio_failing = True
            self._sessions = []
        else:
            if self._audio_failing:
                log.info("Windows audio available again")
            self._audio_failing = False
        self._refreshed = now
        self._forget_gone()
        self.activity.forget_except({s.exe for s in self._sessions})
        if self.spotify is not None:
            self.spotify.set_present(any(s.exe == SPOTIFY for s in self._sessions))
        return True

    def _spotify_web(self, exe):
        return exe == SPOTIFY and self.spotify is not None and self.spotify.usable()

    def _forget_gone(self):
        """Drop remembered Brave sessions whose process is gone."""
        if self._audio_failing or not self._brave_orig:
            return
        pids = {s.pid for s in self._sessions if s.exe == BRAVE}
        self._brave_orig = {p: v for p, v in self._brave_orig.items() if p in pids}

    def _remember_brave(self, s):
        """Before the Brave fallback changes Brave's whole sound: keep how it was,
        so it can be put back once the extension takes over (_restore_brave)."""
        if s.exe == BRAVE and s.pid not in self._brave_orig:
            self._brave_orig[s.pid] = (s.volume(), s.muted())

    def _restore_brave(self, now):
        """The extension is connected again: undo what the fallback did to
        Brave's whole sound (the knob can't reach it while the extension works)."""
        if not self._brave_orig or not self.bridge.state.connected(now):
            return
        orig, self._brave_orig = self._brave_orig, {}
        n = 0
        for s in self._sessions:
            if s.exe == BRAVE and s.pid in orig:
                volume, muted = orig[s.pid]
                try:
                    s.set_volume(volume)
                    s.set_mute(muted)
                    n += 1
                except OSError:
                    self._session_error()
        if n:
            log.info("extension connected: Brave's own sound put back as it was")

    def poll(self):
        if self.refresh():
            self.update_display()  # volume changed elsewhere (Spotify's own slider...)
        now = self.clock()
        self._restore_brave(now)
        for s in self._sessions:
            try:
                self.activity.update(s.exe, s.peak(), now)
            except OSError:
                self._session_error()

    def _active_sessions(self, now):
        """Sessions the knob may change right now."""
        brave_by_extension = self.bridge.state.connected(now)
        allowed = set(self.settings["apps"])
        if self.settings["brave_fallback"] and not brave_by_extension:
            allowed.add(BRAVE)
        if brave_by_extension:
            allowed.discard(BRAVE)  # never touch Brave's sound while the extension does it
        return [s for s in self._sessions if s.exe in allowed]

    def _targets(self, now):
        """(sessions by exe, [router.AppSource], [browser.Tab]) the knob may change."""
        by_exe = {}
        for s in self._active_sessions(now):
            by_exe.setdefault(s.exe, []).append(s)
        tabs = self.bridge.state.tabs(now)
        # an app the knob muted / turned to 0 % while it played makes no sound,
        # but stays the target (so Mute unmutes it) until something else plays
        self._quiet = {exe for exe in self._quiet if self._is_quiet(exe, by_exe.get(exe))}
        if self._quiet and (any(t.playing for t in tabs) or any(
                exe not in self._quiet and self.activity.is_playing(exe, now) for exe in by_exe)):
            self._quiet = set()
        apps = []
        for exe, group in by_exe.items():
            playing = self.activity.is_playing(exe, now) or exe in self._quiet
            if exe == BRAVE and not playing:
                continue  # the Brave fallback only while Brave itself plays
            try:
                muted = all(s.muted() for s in group)
            except OSError:
                muted = False
            apps.append(router.AppSource(exe, playing, muted))
        by_exe = {a.exe: by_exe[a.exe] for a in apps}
        return by_exe, apps, tabs

    def _is_quiet(self, exe, group):
        """All of the app's sessions muted, or its volume at 0 %."""
        if not group:
            return False
        try:
            if all(s.muted() for s in group):
                return True
            if self._spotify_web(exe):
                level = self.spotify.level()
                return level is not None and level <= 0.0005
            return all(s.muted() or s.volume() <= 0.0005 for s in group)
        except OSError:
            return False

    def _note_quiet(self, d, apps, by_exe):
        """After a key: remember which playing apps the knob just silenced."""
        playing = {a.exe for a in apps if a.playing}
        for exe in d.apps:
            if exe in playing and self._is_quiet(exe, by_exe[exe]):
                self._quiet.add(exe)
            else:
                self._quiet.discard(exe)

    def set_level(self, level):
        """Tray menu 25/50/75/100 %: set the knob's target (playing app, else
        all) to exactly `level`, unmuted; a boosted tab drops to it."""
        now = self.clock()
        level = min(1.0, max(0.0, float(level)))
        by_exe, apps, tabs = self._targets(now)
        d = router.decide("up", apps, tabs, self.settings["step"])  # only the targets matter
        log.debug("set %.2f -> %r", level, d)
        for exe in d.apps:
            self._at_max.pop(exe, None)
            web = self._spotify_web(exe)
            for s in by_exe[exe]:
                try:
                    self._remember_brave(s)
                    if s.muted():
                        s.set_mute(False)
                    if not web:
                        s.set_volume(level)
                    elif s.volume() < 1.0 - 1e-6:
                        s.set_volume(1.0)  # a lowered mixer level: the slider alone sets it
                except OSError:
                    self._session_error()
            if web and self.spotify.set_to(level) is None:
                for s in by_exe[exe]:  # Spotify's own volume not known yet: mixer instead
                    try:
                        s.set_volume(level)
                    except OSError:
                        self._session_error()
        by_key = {(t.client, t.tab): t for t in tabs}
        for client, ids in d.tabs.items():
            if "set_volume" not in (self.bridge.features(client) or ()):
                log.warning("Brave tab link %s cannot set an exact volume; tabs left as they are", client)
                continue
            muted = [tid for tid in ids if by_key[(client, tid)].muted]
            if muted:
                self.bridge.command(client, protocol.mute(False, tabs=muted))
            self.bridge.command(client, protocol.set_volume(level, tabs=ids))
            for tid in ids:
                self._guess[(client, tid)] = (level, False, now)
        self._note_quiet(d, apps, by_exe)
        if not d.empty():
            self._shown = self._sources(d, by_key)
            self.update_display(popup=True)
        return d

    def handle(self, action):
        now = self.clock()
        by_exe, apps, tabs = self._targets(now)
        d = router.decide(action, apps, tabs, self.settings["step"])
        log.debug("key %s -> %r", action, d)
        for exe in d.apps:
            self._note_max(exe, by_exe[exe], d, now)
            if d.muted is None and self._spotify_web(exe):
                # Spotify's own slider; mute (and un-muting on a turn) stays the Windows mixer's
                self._spotify_turn(by_exe[exe], d.delta)
                continue
            for s in by_exe[exe]:
                try:
                    self._remember_brave(s)
                    if d.muted is not None:
                        s.set_mute(d.muted)
                    else:
                        if s.muted():
                            s.set_mute(False)
                        s.set_volume(min(1.0, max(0.0, s.volume() + d.delta)))
                except OSError:
                    self._session_error()
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
        self._note_quiet(d, apps, by_exe)
        if not d.empty():
            self._shown = self._sources(d, by_key)
            self.update_display(popup=True)
        return d

    def _spotify_turn(self, group, delta):
        """A turn on Spotify's own slider. Once the slider is full, turning up
        raises Spotify's Windows mixer level too (it may have been lowered
        while the mixer was used instead), so the knob reaches full volume."""
        lowered = []
        for s in group:
            try:
                if s.muted():
                    s.set_mute(False)
                if s.volume() < 1.0 - 1e-6:
                    lowered.append(s)
            except OSError:
                self._session_error()
        level = self.spotify.level()
        if delta > 0 and lowered and level is not None and level >= 1.0 - 1e-6:
            for s in lowered:
                try:
                    s.set_volume(min(1.0, s.volume() + delta))
                except OSError:
                    self._session_error()
            return
        self.spotify.step(delta)

    # --- what the pop-up and the tray icon show ---
    def _note_max(self, exe, group, d, now):
        """Windows apps (Spotify's own slider too) stop at 100 %: a turn up while
        already there marks the app so its pop-up row says "Max"; any other key
        clears it. With Spotify's own slider, its mixer level must be full too."""
        self._at_max.pop(exe, None)
        if d.muted is not None or d.delta <= 0 or not group:
            return
        try:
            level = self.spotify.level() if self._spotify_web(exe) else None
            if level is None:
                level = group[0].volume()
            else:
                level = min(level, min(s.volume() for s in group))
        except OSError:
            return
        if view.percent(level) >= 100:
            self._at_max[exe] = now

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
    def _read_app(self, exe, group):
        """Row for a Windows app: Spotify's own bar while the Web API drives it,
        else its audio sessions (Core Audio volume). Mute is always the mixer's."""
        if not group:
            return None
        try:
            level = self.spotify.level() if self._spotify_web(exe) else None
            if level is None:
                level = group[0].volume()
            t = self._at_max.get(exe)
            at_max = (t is not None and self.clock() - t < MAX_SECONDS
                      and view.percent(level) >= 100)
            return view.Row(view.name_for(exe), level, all(s.muted() for s in group), 1.0,
                            at_max)
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

    # --- settings changed while running ---
    def apply_settings(self, changes):
        """Use new setting values from now on (this thread)."""
        for key, value in changes.items():
            self.settings[key] = value
        if "peak_threshold" in changes:
            self.activity.threshold = changes["peak_threshold"]
        if "hold_seconds" in changes:
            self.activity.hold = changes["hold_seconds"]
        if "failsafe" in changes:
            self.failsafe.enabled = bool(changes["failsafe"])
        if "failsafe_level" in changes:
            self.failsafe.level = changes["failsafe_level"]
        if "failsafe_start_window" in changes:
            self.failsafe.start_window = changes["failsafe_start_window"]
        if "spotify_control" in changes:
            self._switch_spotify(changes["spotify_control"])
        if "apps" in changes or "brave_fallback" in changes:
            self._refreshed = None  # rebuild the session list on the next poll
        self.update_display()

    def _switch_spotify(self, mode):
        if mode == "mixer" and self.spotify is not None:
            old, self.spotify = self.spotify, None
            # its worker may be waiting on the network: don't hold up the knob
            threading.Thread(target=old.close, name="spotify-close", daemon=True).start()
            log.info("Spotify: Windows mixer from now on")
        elif mode == "web" and self.spotify is None:
            if self.spotify_factory is None:
                log.info("Spotify's own slider: used after a restart of the program")
                return
            new = self.spotify_factory()
            new.on_change = self._spotify_changed
            new.start()
            self.spotify = new
            self._refreshed = None  # tells it on the next refresh whether Spotify is open
            log.info("Spotify: its own slider from now on (when logged in)")

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
        if event[0] == "set":  # tray menu 25/50/75/100 % (ui.Ui via press)
            self.set_level(event[1])
            return
        if event[0] == "spotify":  # Spotify's own volume / Web API state changed
            self.update_display()
            return
        if event[0] == "settings":  # saved in the settings window
            self.apply_settings(event[1])
            return
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
                log.warning("boost stopped working in a browser tab (Brave tab link %s); "
                            "the tab may need a reload", client)
        ids = self.failsafe.on_tabs(client, tabs, now, self._apps_open)
        if not ids:
            return
        features = self.bridge.features(client) or ()
        if "set_volume" not in features:
            log.warning("Brave tab link %s cannot set an exact volume; %d loud tab(s) left as they are",
                        client, len(ids))
            return
        log.info("turned %d loud tab(s) (over 100%%) down to %d%%", len(ids),
                 round(self.failsafe.level * 100))
        self.bridge.command(client, protocol.set_volume(self.failsafe.level, tabs=ids))
