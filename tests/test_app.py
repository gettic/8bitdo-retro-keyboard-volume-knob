"""Start-up code: command line, names, setup dispatch."""
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

import volume_knob
from knob import app
from version import VERSION


class AppTests(unittest.TestCase):
    def test_version_file(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_version_flag(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(app.main(["--version"]), 0)
        self.assertEqual(buf.getvalue().strip(), VERSION)

    def test_neutral_names(self):
        for name in (app.MUTEX_NAME, app.STOP_EVENT_NAME):
            self.assertTrue(name.startswith("Local\\8BitDoVolumeKnob."))
            self.assertNotIn("gettic", name.lower())

    def test_bad_argument(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(app.main(["--nope"]), 2)

    def test_stop_when_not_running(self):
        with mock.patch.object(app, "STOP_EVENT_NAME", "Local\\8BitDoVolumeKnob.Test.Nothing"):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(app.request_stop())

    def test_old_native_host_call_is_refused(self):
        # an old Brave registration starting us as a native host must not start the program
        argv = ["volume_knob.py", "chrome-extension://abc/", "--parent-window=0"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(app, "run_program") as run, \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(volume_knob.main(), 2)
        run.assert_not_called()

    def test_setup_dispatch(self):
        fake = types.ModuleType("setup_knob")
        calls = []
        fake.run = lambda argv: calls.append(argv) or 3
        for flag in ("--install", "--uninstall"):
            with mock.patch.dict(sys.modules, {"setup_knob": fake}), \
                    mock.patch.object(sys, "argv", ["volume_knob.py", flag, "--quiet"]):
                self.assertEqual(volume_knob.main(), 3)
        self.assertEqual(calls, [["--install", "--quiet"], ["--uninstall", "--quiet"]])


if __name__ == "__main__":
    unittest.main()
