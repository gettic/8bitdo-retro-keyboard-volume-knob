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
from .pipe_server import PipeError, PipeServer

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
    server = PipeServer(protocol.pipe_name(), bridge.on_message, bridge.on_connect,
                        bridge.on_disconnect)
    bridge.send = server.send
    from .coreaudio import CoreAudio
    ctl = Controller(settings, CoreAudio, bridge)  # also hooks bridge events
    try:
        server.start()
    except PipeError as e:
        log.error("browser pipe not available (error %s); YouTube control is off", e.errno)
        server = None

    worker = threading.Thread(target=ctl.run, name="audio", daemon=True)
    worker.start()
    try:
        hotkeys.run_loop(ctl.press, stop)
    finally:
        ctl.stop()
        worker.join(3)
        if server:
            server.close()
        kernel32.CloseHandle(stop)
        log.info("volume knob stopped")
        kernel32.CloseHandle(mutex)
    return 0


def _print(text):
    if sys.stdout is not None:
        try:
            print(text)
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="volume_knob", add_help=True)
    p.add_argument("--version", action="store_true")
    p.add_argument("--stop", action="store_true", help="stop the running program")
    p.add_argument("--test", action="store_true", help="developer check (see selftest.py)")
    p.add_argument("--self-test", action="store_true", help="read-only health check")
    p.add_argument("--out", help="also write test results to this file")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.version:
        _print(VERSION)
        return 0
    if args.stop:
        ok = request_stop()
        _print("stop sent" if ok else "not running")
        return 0
    if args.self_test or args.test:
        from . import selftest
        if args.self_test:
            return selftest.self_test(args.out)
        return selftest.dev_test(args.out)
    return run_program()
