"""Setup / uninstall tests. Everything runs under a test root: test folders in %TEMP%
(incl. a test Startup folder) and HKCU\\Software\\8BitDoVolumeKnob-installer-test-<id>.
The real keys and the real Startup folder are only read, the Recycle Bin is faked (the
real-bin test runs only with VK_REAL_RECYCLE_TEST=1), and the admin box is faked: the
"elevated" step runs in-process or as a normal hidden process, without elevation. The real
UAC box and writing into the real Program Files are not covered here.

Run from code\\:  python -E -s -m unittest setup_knob.test_setup
"""
import ctypes
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

from setup_knob import setup

REAL = [
    (setup.RUN_KEY, setup.RUN_VALUE),
    (setup.APPROVED_KEY, setup.RUN_VALUE),
    (setup.APPROVED_FOLDER_KEY, setup.SHORTCUT),
] + [(p + "\\" + setup.OLD_HOST, "") for p in setup.OLD_HOST_PARENTS]
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _inside_root(path, root):
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(root)) + os.sep)


def real_snapshot():
    out = [setup.read_value(k, n) for k, n in REAL]
    real_lnk = setup.Layout().shortcut
    out.append(os.path.getmtime(real_lnk) if os.path.exists(real_lnk) else None)
    return out


def split_args(params):
    """Windows' own command-line splitting (what the elevated python would see)."""
    shell32 = ctypes.WinDLL("shell32")
    shell32.CommandLineToArgvW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    n = ctypes.c_int()
    argv = shell32.CommandLineToArgvW("x " + params, ctypes.byref(n))
    try:
        return [argv[i] for i in range(1, n.value)]
    finally:
        ctypes.WinDLL("kernel32").LocalFree(argv)


def make_source(dest):
    """A copy of code\\ as the program will look (with knob\\ws_server.py, which the core
    agent adds; a placeholder if it isn't there yet), plus an old native_host\\ folder."""
    for name in setup.COPY_FILES:
        shutil.copy2(os.path.join(CODE, name), os.path.join(dest, name))
    for name in setup.COPY_DIRS:
        shutil.copytree(os.path.join(CODE, name), os.path.join(dest, name),
                        ignore=shutil.ignore_patterns("__pycache__"))
    ws = os.path.join(dest, "knob", "ws_server.py")
    if not os.path.exists(ws):
        with open(ws, "w") as f:
            f.write('"""placeholder for the test"""\n')
    os.makedirs(os.path.join(dest, "native_host"), exist_ok=True)
    with open(os.path.join(dest, "native_host", "volume_knob_host.py"), "w") as f:
        f.write("# old\n")


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vk_installer_test_")
        self.layout = setup.Layout(self.root)
        self.real_before = real_snapshot()
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src)
        make_source(self.src)
        py, pyw = setup.python_exes()
        self.saved = (setup.shell_recycle, setup.shell_runas, setup.VERIFY_WAIT,
                      setup.copy_program, setup.program_files)
        self.saved_more = (setup.stop_program, setup.drive_type, setup.python_version,
                           setup._rename)
        setup.VERIFY_WAIT = 0
        setup.program_files = lambda: setup.source_files(self.src, py, pyw)
        self.stops = []   # stop_program calls (the real one does nothing on a test root)
        setup.stop_program = lambda layout, notes: self.stops.append(layout.root)
        # Fake Recycle Bin: nothing in these tests reaches the user's real bin.
        self.recycled = []

        def fake_recycle(path):
            if not _inside_root(path, self.root):
                raise AssertionError("fake recycle outside the test root: " + path)
            self.recycled.append(path)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            return 0, False
        setup.shell_recycle = fake_recycle

        # Fake admin box: run the elevated command in-process (no elevation).
        self.elevated = []

        def fake_runas(exe, params, folder, timeout):
            args = split_args(params)
            self.elevated.append((exe, args, folder))
            self.assertTrue(exe.lower().endswith("\\pythonw.exe"), exe)
            self.assertEqual(args[0], "-I")
            self.assertTrue(setup._same(args[1], os.path.join(CODE, "setup_knob", "setup.py")))
            self.assertIn("--test-root", args)
            return setup.run(args[2:])
        setup.shell_runas = fake_runas

    def tearDown(self):
        (setup.shell_recycle, setup.shell_runas, setup.VERIFY_WAIT,
         setup.copy_program, setup.program_files) = self.saved
        (setup.stop_program, setup.drive_type, setup.python_version,
         setup._rename) = self.saved_more
        try:
            setup.delete_test_tree(self.layout.test_key)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(setup.key_exists(self.layout.test_key))
        self.assertEqual(real_snapshot(), self.real_before, "real entries changed")

    def run_cli(self, *args):
        out = os.path.join(self.root, "out.txt")
        rc = setup.run(list(args) + ["--test-root", self.root, "--quiet", "--out", out])
        with open(out, encoding="utf-8") as f:
            return rc, f.read()

    def assert_nothing_registered(self):
        lay = self.layout
        self.assertFalse(os.path.exists(lay.shortcut))
        self.assertIsNone(setup.read_value(lay.run_key, setup.RUN_VALUE))

    def appdata_files(self):
        found = []
        for top in (os.path.join(self.root, "localappdata"), os.path.join(self.root, "appdata")):
            for folder, _, files in os.walk(top):
                found += [os.path.join(folder, f) for f in files]
        return found

    def test_layout_is_test_only(self):
        lay = self.layout
        for key in [lay.run_key, lay.approved_key, lay.approved_folder_key] + lay.old_host_keys:
            self.assertTrue(key.startswith(setup.TEST_PREFIX), key)
        for path in (lay.local_dir, lay.roaming_dir, lay.program_dir, lay.program_new,
                     lay.program_old, lay.result_file,
                     lay.startup_dir, lay.shortcut):
            self.assertTrue(path.startswith(self.root), path)
        self.assertEqual(setup.Layout(self.root).reg_base, lay.reg_base)  # same root, same key

    def test_real_layout(self):
        lay = setup.Layout()
        pf = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
        self.assertTrue(setup._same(lay.program_dir, os.path.join(pf, setup.APP)))
        self.assertTrue(setup._same(lay.shortcut, os.path.join(
            os.environ["APPDATA"], setup.STARTUP, "8BitDo Volume Knob.lnk")))

    def test_copy_list(self):
        self.assertNotIn("native_host", setup.COPY_DIRS)
        self.assertIn(os.path.join("knob", "ws_server.py"), setup.REQUIRED)
        exp = setup.expected_files(self.src)
        self.assertIn(os.path.join("knob", "ws_server.py"), exp)
        self.assertFalse(any(k.startswith("native_host") for k in exp))
        os.remove(os.path.join(self.src, "knob", "ws_server.py"))
        with self.assertRaises(setup.SetupError):
            setup.expected_files(self.src)

    def test_install_twice_then_uninstall(self):
        lay = self.layout
        pd = lay.program_dir
        py, pyw = setup.python_exes()
        # Leftovers of an earlier version that install must clean up.
        os.makedirs(os.path.join(pd, "native_host"))
        for rel in ("volume_knob_host.cmd", setup.OLD_HOST + ".json"):
            open(os.path.join(pd, rel), "w").close()
        for key in lay.old_host_keys:
            setup._set_default(key, "x")
        setup._set_value(lay.run_key, setup.RUN_VALUE, "old")

        for _ in range(2):
            rc, text = self.run_cli("--install")
            self.assertEqual(rc, 0, text)
        self.assertEqual([a[1][2] for a in self.elevated], ["--elevated-copy"] * 2)
        self.assertIn(os.path.join(pd, "extension"), text)

        # Startup shortcut, exactly like the one made by hand on the real PC.
        self.assertEqual(setup.read_shortcut(lay.shortcut), {
            "target": pyw, "arguments": '-I "%s"' % os.path.join(pd, "volume_knob.py"),
            "workdir": pd, "description": setup.DESCRIPTION})
        self.assertIsNone(setup.read_value(lay.run_key, setup.RUN_VALUE))  # old value gone
        for key in lay.old_host_keys:
            self.assertFalse(setup.key_exists(key), key)

        # Program copy: every runtime file with the right size, no leftovers.
        expected = setup.expected_files(self.src)
        for rel in (os.path.join("extension", "background.js"),
                    os.path.join("extension", "page.js"), os.path.join("knob", "ws_server.py")):
            self.assertIn(rel, expected)
        for rel, size in expected.items():
            self.assertEqual(os.path.getsize(os.path.join(pd, rel)), size, rel)
        self.assertTrue(os.path.isfile(os.path.join(pd, setup.MARKER)))
        for rel in ("native_host", "volume_knob_host.cmd", setup.OLD_HOST + ".json", "tests",
                    os.path.join("setup_knob", "test_setup.py"),
                    os.path.join("knob", "__pycache__")):
            self.assertFalse(os.path.exists(os.path.join(pd, rel)), rel)

        # AppData: only the shortcut; no .js, .cmd or manifest.
        for f in self.appdata_files():
            self.assertTrue(setup._same(f, lay.shortcut), f)

        # Things the program and Windows add later, which uninstall must also remove.
        os.makedirs(lay.roaming_dir)
        with open(os.path.join(lay.roaming_dir, "settings.json"), "w") as f:
            f.write("{}")
        spotify = os.path.join(lay.roaming_dir, "spotify.json")  # encrypted Spotify login
        with open(spotify, "w") as f:
            f.write('{"refresh_token": "test-only"}')
        with open(os.path.join(lay.local_dir, "volume_knob.log"), "w") as f:
            f.write("log")
        setup._set_value(lay.approved_key, setup.RUN_VALUE, "x")
        setup._set_value(lay.approved_folder_key, setup.SHORTCUT, "x")

        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.elevated[-1][1][2], "--elevated-remove")
        self.assertFalse(setup._same(self.elevated[-1][2], pd))  # not working inside it
        for path in (lay.local_dir, lay.roaming_dir, pd, lay.shortcut, spotify):
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(setup.key_exists(lay.test_key))

    def test_all_knob_modules_are_copied(self):
        # Every program module in code\knob\ (e.g. spotify_api.py, spotify_login.py, menu.py)
        # is in the copy list, without listing them one by one; tests never are.
        exp = setup.expected_files(self.src)
        for name in os.listdir(os.path.join(CODE, "knob")):
            if name.endswith(".py"):
                rel = os.path.join("knob", name)
                if name.startswith("test_"):
                    self.assertNotIn(rel, exp)
                else:
                    self.assertIn(rel, exp)
        for name in ("spotify_api.py", "spotify_login.py", "menu.py"):
            if os.path.exists(os.path.join(CODE, "knob", name)):
                self.assertIn(os.path.join("knob", name), exp)

    def test_run_value_option(self):
        lay = self.layout
        rc, text = self.run_cli("--install", "--run-value")
        self.assertEqual(rc, 0, text)
        self.assertEqual(setup.read_value(lay.run_key, setup.RUN_VALUE), '"%s" -I "%s"' % (
            setup.python_exes()[1], os.path.join(lay.program_dir, "volume_knob.py")))
        self.assertFalse(os.path.exists(lay.shortcut))
        rc, text = self.run_cli("--install")  # back to the shortcut: Run value goes
        self.assertEqual(rc, 0, text)
        self.assertTrue(os.path.exists(lay.shortcut))
        self.assertIsNone(setup.read_value(lay.run_key, setup.RUN_VALUE))

    def test_foreign_shortcut_left_alone(self):
        lay = self.layout
        setup.write_shortcut(lay.shortcut, setup.python_exes()[1], "something_else.py",
                             self.root)
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertTrue(os.path.exists(lay.shortcut))
        self.assertIn("left alone", text)

    def test_refused_cleanup_is_a_note(self):
        lay = self.layout
        setup._set_default(lay.old_host_keys[0], "x")
        real = setup._delete_key
        calls = []

        def refused(path):
            if path == lay.old_host_keys[0] and not calls:
                calls.append(path)  # refused once (the test cleanup may delete it later)
                raise PermissionError(5, "Access is denied")
            return real(path)
        setup._delete_key = refused
        try:
            rc, text = self.run_cli("--uninstall")
        finally:
            setup._delete_key = real
        self.assertEqual(rc, 0, text)
        self.assertIn("Access is denied", text)

    def test_elevated_command_runs_as_a_real_process(self):
        # The exact command the admin box would start, run as a normal hidden process
        # (python.exe instead of pythonw.exe so the exit code is the same, no window).
        def subprocess_runas(exe, params, folder, timeout):
            self.elevated.append((exe, split_args(params), folder))
            py = os.path.join(os.path.dirname(exe), "python.exe")
            return subprocess.run([py] + split_args(params), cwd=folder, timeout=timeout,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  creationflags=0x08000000).returncode  # CREATE_NO_WINDOW
        setup.shell_runas = subprocess_runas
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        lay = self.layout
        self.assertTrue(os.path.isfile(os.path.join(lay.program_dir, "extension", "page.js")))
        self.assertTrue(os.path.isfile(lay.shortcut))
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertFalse(os.path.exists(lay.program_dir))
        self.assertEqual([a[1][2] for a in self.elevated], ["--elevated-copy", "--elevated-remove"])

    def test_register_from_installed_copy(self):
        lay = self.layout
        py, pyw = setup.python_exes()
        self.assertTrue(setup.copy_and_verify(lay, self.src, lay.program_dir, py, wait=0)["ok"])
        setup.install(lay, setup.source_files(lay.program_dir, py, pyw), start=False)
        self.assertEqual(self.elevated, [])  # the installed copy itself: no admin box
        self.assertEqual(setup.read_shortcut(lay.shortcut)["target"], pyw)

    def test_lost_js_after_copy_removes_and_registers_nothing(self):
        real_copy = self.saved[3]

        def copy_then_lose(src, dest):
            real_copy(src, dest)
            os.remove(os.path.join(dest, "extension", "page.js"))  # "removed by antivirus"
        setup.copy_program = copy_then_lose
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("page.js", text)
        self.assertFalse(os.path.exists(self.layout.program_dir))
        self.assert_nothing_registered()

    def test_refused_write_removes_and_registers_nothing(self):
        real_copy = self.saved[3]

        def copy_refused(src, dest):
            real_copy(src, dest)
            raise PermissionError(13, "Permission denied", "background.js")
        setup.copy_program = copy_refused
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertFalse(os.path.exists(self.layout.program_dir))
        self.assert_nothing_registered()

    def test_admin_box_answered_no(self):
        def cancelled(exe, params, folder, timeout):
            raise setup.Refused(setup.NO_TEXT)
        setup.shell_runas = cancelled
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("You chose No in the Windows permission box", text)
        self.assertFalse(os.path.exists(self.layout.program_dir))
        self.assert_nothing_registered()
        self.assertEqual(self.stops, [])  # the running knob is left alone

    def test_admin_step_without_result(self):
        setup.shell_runas = lambda exe, params, folder, timeout: 1
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("Copying didn't finish. Try again.", text)
        self.assert_nothing_registered()
        self.assertEqual(self.stops, [])

    def test_admin_step_too_long(self):
        def slow(exe, params, folder, timeout):
            raise setup.TookTooLong("took too long")
        setup.shell_runas = slow
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("Copying took too long. Try again.", text)

    # ---- updates: the installed version survives every failure
    def _install_ok(self):
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        self.stops.clear()
        return self.snapshot(self.layout.program_dir)

    def snapshot(self, top):
        out = {}
        for folder, _, files in os.walk(top):
            for f in files:
                full = os.path.join(folder, f)
                with open(full, "rb") as fh:
                    out[os.path.relpath(full, top)] = fh.read()
        return out

    def assert_old_copy_intact(self, before):
        lay = self.layout
        self.assertEqual(self.snapshot(lay.program_dir), before)
        self.assertTrue(os.path.exists(lay.shortcut))
        self.assertFalse(os.path.exists(lay.program_new))
        self.assertFalse(os.path.exists(lay.program_old))
        self.assertEqual(self.stops, [])

    def _bump_source(self):
        with open(os.path.join(self.src, "version.py"), "w") as f:
            f.write('VERSION = "9.9.9"\n')

    def test_update_refused_write_keeps_old_copy(self):
        before = self._install_ok()
        self._bump_source()
        real_copy = self.saved[3]

        def copy_refused(src, dest):
            real_copy(src, dest)
            raise PermissionError(13, "Permission denied", "background.js")
        setup.copy_program = copy_refused
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("so nothing was changed", text)
        self.assertIn("Your installed version still works.", text)
        self.assert_old_copy_intact(before)

    def test_update_lost_file_keeps_old_copy(self):
        before = self._install_ok()
        self._bump_source()
        real_copy = self.saved[3]

        def copy_then_lose(src, dest):
            real_copy(src, dest)
            os.remove(os.path.join(dest, "extension", "page.js"))
        setup.copy_program = copy_then_lose
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("page.js", text)
        self.assert_old_copy_intact(before)

    def test_update_crash_keeps_old_copy(self):
        before = self._install_ok()
        real_copy = self.saved[3]

        def copy_crash(src, dest):
            real_copy(src, dest)
            raise TypeError("copytree() got an unexpected keyword argument")
        setup.copy_program = copy_crash
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("TypeError", text)
        self.assert_old_copy_intact(before)

    def test_elevated_step_catches_any_error(self):
        def boom(*a, **k):
            raise KeyError("x")
        lay = self.layout
        real = setup.expected_files
        setup.expected_files = boom
        try:
            rc, text = self.run_cli("--install")
        finally:
            setup.expected_files = real
        self.assertEqual(rc, 1, text)
        self.assertIn("KeyError", text)
        self.assertFalse(os.path.exists(lay.program_new))
        self.assert_nothing_registered()

    def test_update_file_in_use_rolls_back(self):
        before = self._install_ok()
        self._bump_source()
        lay = self.layout
        # Held open without delete sharing (like a program reading it): the extension
        # folder can't be moved, so the whole swap is put back.
        with open(os.path.join(lay.program_dir, "extension", "page.js"), "rb"):
            rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("is in use (extension). Close Brave and run setup again.", text)
        self.assertIn("Your installed version still works.", text)
        self.assert_old_copy_intact(before)

    def test_swap_rollback_when_a_move_fails(self):
        before = self._install_ok()
        self._bump_source()
        lay = self.layout
        real = setup._rename
        moves = []

        def failing(a, b):
            if os.path.basename(a) == "setup_knob" and setup._same(
                    os.path.dirname(a), lay.program_new):
                raise PermissionError(5, "Access is denied")
            moves.append((a, b))
            return real(a, b)
        setup._rename = failing
        try:
            rc, text = self.run_cli("--install")
        finally:
            setup._rename = real
        self.assertEqual(rc, 1, text)
        self.assertIn("(setup_knob)", text)
        self.assertTrue(len(moves) > 3, moves)  # it really had started swapping
        self.assert_old_copy_intact(before)

    def test_update_swaps_in_new_files_and_drops_stale_ones(self):
        self._install_ok()
        lay = self.layout
        stale = os.path.join(lay.program_dir, "knob", "removed_in_new_version.py")
        open(stale, "w").close()
        self._bump_source()
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        with open(os.path.join(lay.program_dir, "version.py")) as f:
            self.assertIn("9.9.9", f.read())
        self.assertFalse(os.path.exists(stale))
        for rel, size in setup.expected_files(self.src).items():
            self.assertEqual(os.path.getsize(os.path.join(lay.program_dir, rel)), size, rel)
        self.assertFalse(os.path.exists(lay.program_new))
        self.assertFalse(os.path.exists(lay.program_old))
        self.assertEqual(len(self.stops), 1)  # stopped once, after the copy

    def test_leftover_new_folder_is_cleared(self):
        lay = self.layout
        os.makedirs(os.path.join(lay.program_new, "junk"))
        os.makedirs(os.path.join(lay.program_old, "junk"))
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        self.assertFalse(os.path.exists(os.path.join(lay.program_dir, "junk")))
        self.assertFalse(os.path.exists(lay.program_new))
        self.assertFalse(os.path.exists(lay.program_old))

    def test_uninstall_removes_marked_leftovers(self):
        lay = self.layout
        os.makedirs(lay.program_old)
        open(os.path.join(lay.program_old, setup.MARKER), "w").close()
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertFalse(os.path.exists(lay.program_old))

    def test_uninstall_refused_note(self):
        self._install_ok()

        def cancelled(exe, params, folder, timeout):
            raise setup.Refused(setup.NO_TEXT)
        setup.shell_runas = cancelled
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertIn("You chose No in the Windows permission box, so the program folder", text)
        self.assertTrue(os.path.isfile(os.path.join(self.layout.program_dir, setup.MARKER)))

    def test_network_drive_refused(self):
        setup.drive_type = lambda root: setup.DRIVE_REMOTE
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("is on a network drive", text)
        self.assertEqual(self.elevated, [])
        self.assert_nothing_registered()
        setup.drive_type = lambda root: 3  # DRIVE_FIXED
        self.assertIsNone(setup.network_drive(self.src))
        self.assertIsNone(setup.network_drive(r"\\server\share\x"))

    def test_old_python_refused(self):
        setup.python_version = lambda: (3, 7)
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 1, text)
        self.assertIn("needs Python 3.8 or newer (this is 3.7)", text)
        self.assertEqual(self.elevated, [])

    def test_texts(self):
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        self.assertTrue(text.startswith("8BitDo Volume Knob is installed. It starts with "
                                        "Windows."), text)
        self.assertIn("(one Windows permission box)", text)
        self.assertEqual(setup.read_shortcut(self.layout.shortcut)["description"],
                         "8BitDo Volume Knob")
        self.assertNotIn("admin", text.lower())

    def test_startup_turned_off_is_a_note(self):
        lay = self.layout
        with setup.winreg.CreateKeyEx(setup.HKCU, lay.approved_folder_key, 0,
                                      setup.winreg.KEY_WRITE) as k:
            setup.winreg.SetValueEx(k, setup.SHORTCUT, 0, setup.winreg.REG_BINARY,
                                    b"\x03" + b"\x00" * 11)
        rc, text = self.run_cli("--install")
        self.assertEqual(rc, 0, text)
        self.assertIn("turned off in Task Manager", text)
        with setup.winreg.CreateKeyEx(setup.HKCU, lay.approved_folder_key, 0,
                                      setup.winreg.KEY_WRITE) as k:
            setup.winreg.SetValueEx(k, setup.SHORTCUT, 0, setup.winreg.REG_BINARY,
                                    b"\x02" + b"\x00" * 11)
        rc, text = self.run_cli("--install")
        self.assertNotIn("turned off", text)

    def test_remove_guard(self):
        lay = self.layout
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        open(os.path.join(other, setup.MARKER), "w").close()
        open(os.path.join(other, "volume_knob.py"), "w").close()
        self.assertFalse(setup.remove_guarded(lay, other)["ok"])
        self.assertTrue(os.path.isdir(other))
        os.makedirs(lay.program_dir)
        open(os.path.join(lay.program_dir, "volume_knob.py"), "w").close()
        self.assertFalse(setup.remove_guarded(lay, lay.program_dir)["ok"])  # no marker
        self.assertTrue(os.path.isdir(lay.program_dir))
        with self.assertRaises(setup.SetupError):
            setup.copy_and_verify(lay, self.src, other, setup.python_exes()[0], wait=0)

    def test_uninstall_keeps_unmarked_program_folder(self):
        pd = self.layout.program_dir
        os.makedirs(pd)
        with open(os.path.join(pd, "volume_knob.py"), "w") as f:
            f.write("")
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.elevated, [])  # no admin box at all
        self.assertTrue(os.path.isfile(os.path.join(pd, "volume_knob.py")))

    def test_uninstall_when_nothing_installed(self):
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.elevated, [])

    def test_refuses_store_python(self):
        real = setup.sys.base_prefix
        setup.sys.base_prefix = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps"
        try:
            with self.assertRaises(setup.SetupError):
                setup.python_exes()
        finally:
            setup.sys.base_prefix = real
        with self.assertRaises(setup.SetupError):
            setup._check_python(r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe")

    def test_command_line(self):
        self.assertEqual(setup.command_line([r"C:\p w.exe", "-I", r"C:\s.py"]),
                         '"C:\\p w.exe" -I "C:\\s.py"')

    def test_prune_keeps_non_empty_parents(self):
        lay = self.layout
        parent = lay.reg_base + setup.OLD_HOST_PARENTS[0]
        setup._set_default(parent + "\\some.other.host", "x")
        setup._set_default(lay.old_host_keys[0], "y")
        setup.remove_old_host(lay, [])
        self.assertEqual(setup.read_value(parent + "\\some.other.host"), "x")
        self.assertFalse(setup.key_exists(lay.old_host_keys[0]))

    def test_refuses_real_tree_delete(self):
        with self.assertRaises(setup.SetupError):
            setup.delete_test_tree(setup.RUN_KEY)

    def test_temp_check(self):
        self.assertTrue(setup.in_temp(os.path.join(tempfile.gettempdir(), "x", "y")))
        self.assertFalse(setup.in_temp(os.path.dirname(os.path.abspath(__file__))))

    def test_bad_args(self):
        self.assertNotEqual(setup.run(["--install", "--uninstall", "--quiet"]), 0)

    def _folder(self):
        folder = os.path.join(self.root, "vk_installer_test_recycle_" + uuid.uuid4().hex[:8])
        os.makedirs(folder)
        with open(os.path.join(folder, "a.txt"), "w") as f:
            f.write("test")
        return folder

    def test_recycle_logic(self):
        folder = self._folder()
        self.assertTrue(setup.recycle(folder))          # fake bin: deleted in the test root
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(self.recycled, [folder])
        self.assertTrue(setup.recycle(folder))          # already gone: no second call
        self.assertEqual(len(self.recycled), 1)
        setup.shell_recycle = lambda path: (0x78, False)  # Windows refused
        self.assertFalse(setup.recycle(self._folder()))
        setup.shell_recycle = lambda path: (0, True)      # aborted
        self.assertFalse(setup.recycle(self._folder()))

    @unittest.skipUnless(os.environ.get("VK_REAL_RECYCLE_TEST") == "1",
                         "uses the real Recycle Bin; set VK_REAL_RECYCLE_TEST=1 to run")
    def test_recycle_real_bin(self):
        setup.shell_recycle = self.saved[0]
        folder = self._folder()
        self.assertTrue(setup.recycle(folder))
        self.assertFalse(os.path.exists(folder))


if __name__ == "__main__":
    unittest.main()
