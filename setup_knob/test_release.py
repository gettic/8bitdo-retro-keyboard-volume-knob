"""make_release checks on temp copies (no zip, no self-test, git faked).

Run from code\\:  python -E -s -m unittest setup_knob.test_release
"""
import os
import shutil
import tempfile
import unittest

import make_release
from setup_knob import setup

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vk_release_test_")
        self.src = os.path.join(self.root, "src")
        setup.copy_program(CODE, self.src)          # a test copy of what ships
        os.remove(os.path.join(self.src, setup.MARKER))
        for name in make_release.EXTRA_FILES:
            shutil.copy2(os.path.join(CODE, name), os.path.join(self.src, name))
        self.dest = os.path.join(self.root, "stage")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_git(self, untracked=(), changed=()):
        def git(args):
            if args[0] == "ls-files":
                out = []
                for folder, _, files in os.walk(self.src):
                    for f in files:
                        rel = os.path.relpath(os.path.join(folder, f), self.src)
                        if rel not in untracked:
                            out.append(rel.replace(os.sep, "/"))
                return out
            if args[0] == "diff":
                return [c.replace(os.sep, "/") for c in changed]
            raise AssertionError(args)
        return git

    def check(self, **kw):
        make_release.stage(self.dest, self.src)
        return make_release.check(self.dest, self.src, self.fake_git(**kw))

    def test_clean_tree_passes(self):
        files = self.check()
        self.assertIn(os.path.join("knob", "app.py"), files)

    def test_untracked_file_blocked(self):
        stray = os.path.join("knob", "scratch_idea.py")
        open(os.path.join(self.src, stray), "w").close()
        with self.assertRaises(make_release.ReleaseError) as cm:
            self.check(untracked=(stray,))
        self.assertIn("not committed (new file): " + stray, str(cm.exception))

    def test_changed_file_blocked(self):
        rel = os.path.join("knob", "app.py")
        with self.assertRaises(make_release.ReleaseError) as cm:
            self.check(changed=(rel,))
        self.assertIn("not committed (changed): " + rel, str(cm.exception))

    def test_forbidden_extensions_blocked(self):
        for name in ("key.pem", "old.crx", "x.zip", "debug.log"):
            open(os.path.join(self.src, "extension", name), "w").close()
        with self.assertRaises(make_release.ReleaseError) as cm:
            self.check()  # even when tracked
        for name in ("key.pem", "old.crx", "x.zip", "debug.log"):
            self.assertIn("forbidden file: " + os.path.join("extension", name),
                          str(cm.exception))

    def test_real_git_call_is_read_only_and_works(self):
        # Only lists files; works on the real repo, whatever its state.
        tracked = make_release.run_git(["ls-files", "-z"])
        self.assertIn("volume_knob.py", tracked)


if __name__ == "__main__":
    unittest.main()
