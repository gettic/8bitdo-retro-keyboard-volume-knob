"""Runs the Brave extension's headless JavaScript tests (tests/extension_js)
with Node's built-in test runner, when Node is installed; skipped otherwise.
No browser, no network: the extension files run against fake browser parts."""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JS_TESTS = os.path.join(HERE, "extension_js")


class ExtensionJsTests(unittest.TestCase):
    def test_extension_js(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not installed")
        files = sorted(os.path.join(JS_TESTS, n) for n in os.listdir(JS_TESTS) if n.endswith(".test.js"))
        self.assertTrue(files, "no extension tests found")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run([node, "--test", *files], cwd=JS_TESTS, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120,
                              creationflags=flags)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
