"""Program start-up: single instance, threads, stop handling, command line."""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

from version import VERSION

from . import config, hotkeys, logsetup, protocol
from .browser import BrowserBridge, BrowserState
from .controller import Controller
from .ws_server import WsError, WsServer, process_check

MUTEX_NAME = "Local\\8BitDoVolumeKnob.Instance"
STOP_EVENT_NAME = "Local\\8BitDoVolumeKnob.Stop"
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
for _name, _res, _args in (
        ("CreateMutexW", wt.HANDLE, (ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)),
        ("OpenMutexW", wt.HANDLE, (wt.DWORD, wt.BOOL, wt.LPCWSTR)),
        ("CreateEventW", wt.HANDLE, (ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR)),
        ("OpenEventW", wt.HANDLE, (wt.DWORD, wt.BOOL, wt.LPCWSTR)),
        ("SetEvent", wt.BOOL, (wt.HANDLE,)),
        ("ResetEvent", wt.BOOL, (wt.HANDLE,)),
        ("CloseHandle", wt.BOOL, (wt.HANDLE,))):
    _f = getattr(kernel32, _name)
    _f.restype, _f.argtypes = _res, _args


def is_running():
    """True if the real program is running (its mutex exists)."""
    h = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def request_stop():
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
    if not h:
        return False
    kernel32.SetEvent(h)
    kernel32.CloseHandle(h)
    return True


def run_program():
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS or not mutex:
        return 0  # already running
    settings, warnings = config.load()
    log = logsetup.setup(settings["log_level"])
    log.info("volume knob %s starting", VERSION)
    for w in warnings:
        log.warning("settings: %s", w)
    stop = kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)
    kernel32.ResetEvent(stop)

    state = BrowserState()
    bridge = BrowserBridge(state, time.monotonic)
    from .coreaudio import CoreAudio
    from .spotify_api import SpotifyVolume
    spotify = None
    if settings["spotify_control"] == "web":
        spotify = SpotifyVolume()  # does nothing online until --spotify-login was done
    # also hooks bridge events; the factory is for switching to "web" in the settings window
    ctl = Controller(settings, CoreAudio, bridge, spotify=spotify, spotify_factory=SpotifyVolume)
    if spotify:
        spotify.start()
    server = None
    try:
        server = WsServer(protocol.ports(), bridge.on_message, bridge.on_connect,
                          bridge.on_disconnect, origin=protocol.EXTENSION_ORIGIN,
                          peer_check=process_check(("brave.exe",)))
        bridge.send = server.send
        port = server.start()
        log.info("browser link on 127.0.0.1:%s", port)
    except (WsError, OSError) as e:
        log.error("browser link not available (%s); YouTube control is off, "
                  "Windows audio still works", e)
        bridge.send = None
        server = None

    ui = _make_ui(settings, ctl, stop, log)
    ctl.display = ui.show if ui else None
    worker = threading.Thread(target=ctl.run, name="audio", daemon=True)
    worker.start()
    try:
        hotkeys.run_loop(ctl.press, stop, hwnd=ui.hwnd if ui else None,
                         on_status=ui.keys_status if ui else None)
    finally:
        ctl.stop()
        worker.join(3)
        if ctl.spotify:  # may have been switched in the settings window
            ctl.spotify.close()
        if ui:
            ui.close()
        if server:
            server.close()
        kernel32.CloseHandle(stop)
        log.info("volume knob stopped")
        kernel32.CloseHandle(mutex)
    return 0


def _make_ui(settings, ctl, stop, log):
    """Tray icon + volume pop-up on this (the hotkey) thread; None if both are
    turned off or the windows can't be made (the knob works without them)."""
    if not (settings["osd"] or settings["tray_icon"]):
        return None
    import os

    def pause(on):
        (hotkeys.pause if on else hotkeys.resume)()

    try:
        from .ui import Ui
        ui = Ui(settings, on_quit=lambda: kernel32.SetEvent(stop), on_pause=pause,
                on_key=ctl.press,
                on_settings=lambda changes: ctl.press(("settings", changes)),
                log_path=os.path.join(config.log_dir(), config.LOG_NAME),
                settings_path=os.path.join(config.data_dir(), config.SETTINGS_NAME))
        ui.create()
    except Exception as e:
        log.error("taskbar icon / volume pop-up not available (%s); the knob still works", e)
        return None
    return ui


def _print(text):
    if sys.stdout is not None:
        try:
            print(text)
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="volume_knob", add_help=True,
        description="8BitDo Volume Knob: without options it runs in the background.",
        epilog="Set up / remove: volume_knob.py --install  or  volume_knob.py --uninstall")
    p.add_argument("--version", action="store_true", help="show the version")
    p.add_argument("--stop", action="store_true", help="stop the running program")
    p.add_argument("--test", action="store_true", help="developer check (see selftest.py)")
    p.add_argument("--self-test", action="store_true", help="check that everything works (changes nothing)")
    p.add_argument("--out", help="also write test results to this file")
    p.add_argument("--spotify-login", action="store_true",
                   help="one-time Spotify login (Premium) so the knob moves Spotify's own slider")
    p.add_argument("--spotify-logout", action="store_true", help="remove the Spotify login")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.version:
        _print(VERSION)
        return 0
    if args.stop:
        ok = request_stop()
        _print("Stopping the program." if ok else "The program isn't running.")
        return 0
    if args.spotify_login or args.spotify_logout:
        from . import spotify_login
        if args.spotify_logout:
            return spotify_login.logout(_print)
        return spotify_login.login(_print)
    if args.self_test or args.test:
        from . import selftest
        if args.self_test:
            return selftest.self_test(args.out)
        return selftest.dev_test(args.out)
    return run_program()
