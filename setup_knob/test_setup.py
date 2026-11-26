"""Setup / uninstall tests. Everything runs under a test root: test folders in %TEMP%
and HKCU\\Software\\8BitDoVolumeKnob-installer-test-<id>. The real keys are only read,
and the Recycle Bin is faked (the real-bin test runs only with VK_REAL_RECYCLE_TEST=1).

Run from code\\:  python -m unittest setup_knob.test_setup
"""
import json
import os
import shutil
import tempfile
import unittest
import uuid

from setup_knob import setup

REAL = [
    (setup.CHROME_HOSTS + "\\" + setup.HOST_NAME, ""),
    (setup.BRAVE_HOSTS + "\\" + setup.HOST_NAME, ""),
    (setup.RUN_KEY, setup.RUN_VALUE),
    (setup.APPROVED_KEY, setup.RUN_VALUE),
]


def _inside_root(path, root):
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(root)) + os.sep)


def real_snapshot():
    return [setup.read_value(k, n) for k, n in REAL]


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vk_installer_test_")
        self.layout = setup.Layout(self.root)
        self.real_before = real_snapshot()
        # Fake Recycle Bin: nothing in these tests reaches the user's real bin.
        self.real_shell_recycle = setup.shell_recycle
        self.recycled = []

        def fake(path):
            if not _inside_root(path, self.root):
                raise AssertionError("fake recycle outside the test root: " + path)
            self.recycled.append(path)
            shutil.rmtree(path)
            return 0, False
        setup.shell_recycle = fake

    def tearDown(self):
        setup.shell_recycle = self.real_shell_recycle
        try:
            setup.delete_test_tree(self.layout.test_key)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(setup.key_exists(self.layout.test_key))
        self.assertEqual(real_snapshot(), self.real_before, "real registry entries changed")

    def run_cli(self, *args):
        out = os.path.join(self.root, "out.txt")
        rc = setup.run(list(args) + ["--test-root", self.root, "--quiet", "--out", out])
        with open(out, encoding="utf-8") as f:
            return rc, f.read()

    def test_layout_is_test_only(self):
        lay = self.layout
        for key in (lay.chrome_key, lay.brave_key, lay.run_key, lay.approved_key):
            self.assertTrue(key.startswith(setup.TEST_PREFIX), key)
        for path in (lay.local_dir, lay.roaming_dir, lay.manifest, lay.extension_dir):
            self.assertTrue(path.startswith(self.root), path)
        self.assertEqual(setup.Layout(self.root).reg_base, lay.reg_base)  # same root, same key

    def test_install_twice_then_uninstall(self):
        files = setup.program_files()
        for _ in range(2):
            rc, text = self.run_cli("--install")
            self.assertEqual(rc, 0, text)
        lay = self.layout
        with open(lay.manifest, "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "manifest has a BOM")
        m = json.loads(raw.decode("utf-8"))
        self.assertEqual(m["name"], setup.HOST_NAME)
        self.assertEqual(m["type"], "stdio")
        self.assertEqual(m["path"], files["host"])
        self.assertEqual(m["allowed_origins"], ["chrome-extension://%s/" % setup.EXTENSION_ID])
        self.assertTrue(os.path.isfile(os.path.join(lay.extension_dir, "manifest.json")))
        self.assertEqual(setup.read_value(lay.chrome_key), lay.manifest)
        self.assertEqual(setup.read_value(lay.brave_key), lay.manifest)
        self.assertEqual(setup.read_value(lay.run_key, setup.RUN_VALUE),
                         setup.command_line(files["start"]))

        # Things the program and Windows add later, which uninstall must also remove.
        os.makedirs(lay.roaming_dir)
        with open(os.path.join(lay.roaming_dir, "settings.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(lay.local_dir, "volume_knob.log"), "w") as f:
            f.write("log")
        setup._set_value(lay.approved_key, setup.RUN_VALUE, "x")

        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)
        for path in (lay.local_dir, lay.roaming_dir):
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(setup.key_exists(lay.test_key))

    def test_uninstall_when_nothing_installed(self):
        rc, text = self.run_cli("--uninstall")
        self.assertEqual(rc, 0, text)

    def test_prune_keeps_non_empty_parents(self):
        lay = self.layout
        other = lay.reg_base + setup.CHROME_HOSTS + "\\some.other.host"
        setup._set_default(other, "x")
        setup._set_default(lay.chrome_key, "y")
        setup._delete_key(lay.chrome_key)
        setup._prune_empty(lay.reg_base + setup.CHROME_HOSTS, lay.reg_base + "Software")
        self.assertEqual(setup.read_value(other), "x")

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
        setup.shell_recycle = self.real_shell_recycle
        folder = self._folder()
        self.assertTrue(setup.recycle(folder))
        self.assertFalse(os.path.exists(folder))


if __name__ == "__main__":
    unittest.main()
