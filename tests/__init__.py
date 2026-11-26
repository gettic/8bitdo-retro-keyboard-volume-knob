"""Unit tests. Run from the code folder:  python -m unittest discover -s tests -t .

Safety guard: the tests must never change real volumes or take the volume
keys, so the real Core Audio object and hotkey registration are blocked here,
before any test runs."""
import logging

import knob.coreaudio
import knob.hotkeys


def _blocked(*args, **kwargs):
    raise AssertionError("tests must not use real Core Audio or hotkeys")


knob.coreaudio.CoreAudio.__init__ = _blocked
knob.coreaudio.Session.set_volume = _blocked
knob.coreaudio.Session.set_mute = _blocked
knob.coreaudio.running_exes = _blocked
knob.hotkeys.register = _blocked
knob.hotkeys.run_loop = _blocked

logging.getLogger("knob").addHandler(logging.NullHandler())
logging.getLogger("knob").propagate = False
