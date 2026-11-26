"""Unit tests. Run from the code folder:  python -m unittest discover -s tests -t .

Safety guard: the tests must never change real volumes or take the volume
keys, so the real Core Audio object and hotkey registration are blocked here,
before any test runs."""
import logging

import knob.coreaudio
import knob.hotkeys
import knob.menu
import knob.osd
import knob.ui


def _blocked(*args, **kwargs):
    raise AssertionError("tests must not use real Core Audio, hotkeys, the tray, "
                         "show a window or open files")


knob.coreaudio.CoreAudio.__init__ = _blocked
knob.coreaudio.Session.set_volume = _blocked
knob.coreaudio.Session.set_mute = _blocked
knob.coreaudio.running_exes = _blocked
# the loop itself, for tests that fake register/unregister first (no real keys)
REAL_RUN_LOOP = knob.hotkeys.run_loop
knob.hotkeys.register = _blocked
knob.hotkeys.unregister = _blocked
knob.hotkeys.run_loop = _blocked
knob.ui.shell_notify = _blocked        # no icon on the user's taskbar
knob.ui.open_file = _blocked           # no Notepad
knob.osd.Osd._present = _blocked       # the pop-up window is never shown
knob.menu.Menu._present = _blocked     # nor the tray menu
knob.menu.Menu._activate = _blocked

logging.getLogger("knob").addHandler(logging.NullHandler())
logging.getLogger("knob").propagate = False
