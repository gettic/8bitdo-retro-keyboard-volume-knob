"""--self-test (read-only health check) and --test (developer check)."""
import ctypes
import logging
import os
import re
import shutil
import tempfile
import threading
import time

from version import VERSION

from . import config, hotkeys, logsetup, protocol


class _Report:
    def __init__(self, out_path):
        self.lines = []
        self.failed = 0
        self.out_path = out_path

    def add(self, name, ok, detail=""):
        if not ok:
            self.failed += 1
        self.lines.append(f"[{'OK' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    def info(self, text):
        self.lines.append("       " + text)

    def finish(self):
        self.lines.append("RESULT: " + ("healthy" if not self.failed else f"{self.failed} failed"))
        text = "\n".join(self.lines)
        from .app import _print
        _print(text)
        if self.out_path:
            with open(self.out_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        return 0 if not self.failed else 1


def _in_thread(fn):
    """Run fn on a fresh thread (fresh COM apartment); return (result, error)."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except Exception as e:  # reported, not raised
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(15)
    if t.is_alive():
        return None, TimeoutError("timed out")
    return box.get("result"), box.get("error")


def _check_settings(rep):
    tmp = tempfile.mkdtemp(prefix="volume_knob_selftest_")
    try:
        s, w = config.load(tmp)
        ok = (s == config.DEFAULTS and not w
              and os.path.exists(os.path.join(tmp, config.SETTINGS_NAME)))
        s2, w2 = config.load(tmp)
        rep.add("settings", ok and s2 == s and not w2)
        log = logsetup.setup("INFO", tmp)
        log.info("self-test")
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
        rep.add("log file", os.path.exists(os.path.join(tmp, config.LOG_NAME)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_audio(rep):
    def read_only():
        from .coreaudio import CoreAudio
        audio = CoreAudio()
        try:
            sessions = audio.sessions(None)
            try:
                names = sorted({s.exe for s in sessions})
                for s in sessions:  # reads only
                    s.peak()
                    s.volume()
                    s.muted()
            finally:
                for s in sessions:
                    s.release()
            return len(sessions), names, audio.master_volume()
        finally:
            audio.close()
    res, err = _in_thread(read_only)
    if err:
        rep.add("core audio (read only)", False, repr(err))
    else:
        n, names, master = res
        rep.add("core audio (read only)", True,
                f"{n} app sessions ({', '.join(names) or 'none'}), output device found: "
                f"{master is not None}")


def _check_link(rep):
    """WebSocket round trip on a random free port (never the program's own ports).
    The peer check is the real one, allowing only our own exe name."""
    from .browser import BrowserBridge, BrowserState
    from .controller import Controller
    from .ws_server import WsClient, WsServer, process_check, process_info

    class _NoAudio:  # the link check never touches real audio
        def sessions(self, apps=None):
            return []

        def close(self):
            pass

    state = BrowserState()
    bridge = BrowserBridge(state, time.monotonic)
    ctl = Controller(dict(config.DEFAULTS), _NoAudio, bridge, process_lister=lambda n: set())
    worker = threading.Thread(target=ctl.run, daemon=True)
    worker.start()
    client = None
    server = None
    try:
        own_exe = process_info(os.getpid())[0] or "?"
        server = WsServer([0], bridge.on_message, bridge.on_connect, bridge.on_disconnect,
                          origin=protocol.EXTENSION_ORIGIN, peer_check=process_check([own_exe]))
        bridge.send = server.send
        port = server.start()
        client = WsClient(port)
        if not client.ok:
            raise OSError(f"handshake refused (status {client.status})")
        client.send({"type": "hello", "protocol": protocol.PROTOCOL, "host_version": "selftest",
                     "features": list(protocol.FEATURES)})
        reply = client.read()
        ok_hello = (bool(reply) and reply.get("type") == "hello"
                    and reply.get("protocol") == protocol.PROTOCOL)
        # a boosted tab at start -> failsafe sets it to 50 %
        client.send({"type": "state", "tabs": [
            {"tab": 1, "site": "youtube", "playing": True, "volume": 1.3, "muted": False,
             "max_volume": 1.5, "boost": "on"}]})
        got = client.read()
        ok_fail = bool(got) and got == protocol.set_volume(0.5, tabs=[1])
        ok_state = len(state.tabs(time.monotonic())) == 1
        ctl.press("up")
        got = client.read()
        ok_cmd = bool(got) and got == protocol.volume(config.DEFAULTS["step"], tabs=[1],
                                                       max_volume=1.5)
        rep.add("browser link", ok_hello and ok_state and ok_fail and ok_cmd,
                f"hello={ok_hello} state={ok_state} failsafe={ok_fail} knob={ok_cmd}")
    except OSError as e:
        rep.add("browser link", False, repr(e))
    finally:
        if client:
            client.close()
        ctl.stop()
        worker.join(3)
        if server:
            server.close()


def _check_hotkeys(rep):
    from .app import is_running
    if is_running():
        rep.add("volume keys", True, "held by the running program (not checked)")
        return

    def try_register():
        ok, err = hotkeys.register()
        if ok:
            hotkeys.unregister()
        return ok, err
    res, e = _in_thread(try_register)
    if e:
        rep.add("volume keys", False, repr(e))
        return
    ok, err = res
    if ok:
        rep.add("volume keys", True, "can be registered")
    elif err == hotkeys.ERROR_HOTKEY_ALREADY_REGISTERED:
        rep.add("volume keys", True, "WARNING: another program holds them")
    else:
        rep.add("volume keys", False, f"error {err}")


def _check_display(rep):
    """Tray icon + pop-up: windows created hidden and destroyed, the tray call
    faked (no icon appears), the pop-up and icons drawn in memory only."""
    from . import display, menu, osd, ui

    def run():
        calls = []
        u = ui.Ui(dict(config.DEFAULTS), on_quit=lambda: None, notify=lambda m, d: calls.append(m) or True,
                  opener=lambda p: None, dpi_aware=False)
        u.create()
        try:
            shown = []
            u.osd._present = lambda *a: shown.append(a)
            hidden = not any(ui.user32.IsWindowVisible(h)
                             for h in (u.hwnd, u.osd.hwnd, u.menu.hwnd))
            row = display.Row("Spotify", 0.45, False, 1.0)
            w, h, data = osd.render([row], 1.0, False)
            mw, mh, mdata = menu.render(ui.menu_items(False), None, 1.0, False)
            icon = ui.make_hicon(24, ui.icon_pixels(24, 45, number=True))
            ui.user32.DestroyIcon(icon)
            from . import settings_window
            sw, sh, sdata = settings_window.render(dict(config.DEFAULTS))  # memory only
            drawn = (len(data) == w * h * 4 and data[3] == 0
                     and len(mdata) == mw * mh * 4 and mdata[3] == 0
                     and len(sdata) == sw * sh * 4)
            return hidden, calls[:1] == [ui.NIM_ADD], drawn
        finally:
            u.close()
    res, err = _in_thread(run)
    if err:
        rep.add("tray icon + pop-up (hidden)", False, repr(err))
        return
    hidden, tray, drawn = res
    rep.add("tray icon + pop-up (hidden)", hidden and tray and drawn,
            f"windows hidden={hidden} tray={tray} drawn={drawn}")


def _check_spotify(rep, path=None, api_base=None, accounts=None):
    """Login file readable (DPAPI); only when logged in: one read-only
    GET /me/player (no volume change; the track is not shown)."""
    from . import spotify_api as api
    path = path or api.store_path()
    if not os.path.exists(path):
        rep.add("spotify", True, "not logged in (Windows mixer used for Spotify)")
        return
    creds = api.load_store(path)
    if creds is None:
        rep.add("spotify", False, "login file unreadable; run --spotify-login again")
        return
    client = api.Client(creds, path, api_base or api.API, accounts or api.ACCOUNTS)
    try:
        p = client.player()
    except api.ApiError as e:
        rep.add("spotify", False, e.reason)
        return
    dev = p.get("device") if p else None
    if not isinstance(dev, dict):
        rep.add("spotify", True, "logged in; no active Spotify device right now")
        return
    ours = api.is_this_pc(dev, creds)
    rep.add("spotify", True, f"logged in; active device on this PC={ours}, "
                             f"volume {dev.get('volume_percent')}%")


def self_test(out_path=None):
    """Health check. Changes no volume, presses no keys, opens no window.
    Returns 0 when healthy."""
    logging.getLogger("knob").addHandler(logging.NullHandler())
    rep = _Report(out_path)
    rep.add("version", bool(re.fullmatch(r"\d+\.\d+\.\d+", VERSION)), VERSION)
    _check_settings(rep)
    _check_audio(rep)
    _check_link(rep)
    _check_display(rep)
    _check_spotify(rep)
    _check_hotkeys(rep)
    return rep.finish()


# ---------------------------------------------------------------- dev check
def dev_test(out_path=None):
    """Developer check on the real PC (quiet):
    - nudges each target app's session volume by 0.001 and restores it exactly;
    - if the volume keys can be registered, sends two fake volume key presses
      that cancel out (down+up when the output volume is high, else up+down)
      and reports whether both were caught (caught keys change nothing)."""
    from .app import is_running
    rep = _Report(out_path)
    if is_running():
        rep.add("dev test", False, "the program is running; stop it first (--stop)")
        return rep.finish()
    apps = set(config.DEFAULTS["apps"]) | {"brave.exe"}

    def nudge():
        from .coreaudio import CoreAudio
        audio = CoreAudio()
        lines = []
        try:
            for s in audio.sessions(apps):
                try:
                    before = s.volume()
                    s.set_volume(before - 0.001 if before > 0.01 else before + 0.001)
                    moved = abs(s.volume() - before) > 0.0005
                    s.set_volume(before)
                    restored = abs(s.volume() - before) < 0.0001
                    lines.append((s.exe, before, moved, restored))
                finally:
                    s.release()
            return lines, audio.master_volume()
        finally:
            audio.close()
    res, err = _in_thread(nudge)
    if err:
        rep.add("app volumes", False, repr(err))
        master = None
    else:
        lines, master = res
        for exe, before, moved, restored in lines:
            rep.add(f"app volume {exe}", moved and restored,
                    f"at {before:.0%}, can change={moved}, restored={restored}")
        if not lines:
            rep.info(f"no sessions of {', '.join(sorted(apps))} open")

    def keys():
        from .app import kernel32
        CreateEventW, SetEvent, CloseHandle = (kernel32.CreateEventW, kernel32.SetEvent,
                                               kernel32.CloseHandle)
        ok, err = hotkeys.register()
        if not ok:
            return None, err
        hotkeys.unregister()
        user32 = ctypes.WinDLL("user32")
        order = ((hotkeys.VK_VOLUME_DOWN, hotkeys.VK_VOLUME_UP) if (master or 0) >= 0.5
                 else (hotkeys.VK_VOLUME_UP, hotkeys.VK_VOLUME_DOWN))
        stop = CreateEventW(None, True, False, None)
        got = []

        def press():
            time.sleep(0.3)
            for vk in order:
                user32.keybd_event(vk, 0, 1, 0)      # KEYEVENTF_EXTENDEDKEY
                user32.keybd_event(vk, 0, 3, 0)      # + KEYEVENTF_KEYUP
                time.sleep(0.1)
            time.sleep(1.0)
            SetEvent(stop)  # backstop if keys were missed
        t = threading.Thread(target=press, daemon=True)
        t.start()
        hotkeys.run_loop(got.append, stop, max_keys=2)
        t.join(3)
        CloseHandle(stop)
        return got, 0
    res, err = _in_thread(keys)
    if err:
        rep.add("volume keys", False, repr(err))
    else:
        got, code = res
        if got is None:
            rep.add("volume keys", False, f"could not register (error {code}); no keys sent")
        else:
            rep.add("volume keys caught", len(got) == 2, f"{got}")
    return rep.finish()
