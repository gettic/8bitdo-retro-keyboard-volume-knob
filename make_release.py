"""Builds the release zip from the Python source (standard library only, nothing downloaded,
no exe). The zip holds the same files setup copies to Program Files, plus README.md:

    python -I make_release.py --version 1.1.0
    -> dist\\8bitdo-volume-knob-1.1.0.zip   (top folder "8BitDo Volume Knob\\")

Steps: version check against version.py, stage the files in dist\\stage\\, check required
and forbidden files, run the staged program's --self-test once (read-only, quiet), zip.
Exit 0: OK, the last printed line is the zip path. Exit 1: something failed (printed).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from setup_knob import setup  # noqa: E402  (the copy list lives there)

TOP = setup.APP                                  # "8BitDo Volume Knob"
EXTRA_FILES = ("README.md",)
FORBIDDEN_EXT = (".exe", ".dll", ".pyd", ".cmd", ".bat", ".vbs", ".ps1", ".pyc", ".spec")
FORBIDDEN_PARTS = ("tests", "__pycache__", "build", "dist", ".git", "native_host")
REQUIRED_EXTRA = (os.path.join("extension", "background.js"),
                  os.path.join("extension", "content.js"),
                  os.path.join("extension", "page.js"), "LICENSE", "README.md")


class ReleaseError(Exception):
    pass


def read_version():
    with open(os.path.join(HERE, "version.py"), encoding="utf-8") as f:
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise ReleaseError("no VERSION in version.py")
    return m.group(1)


def stage(dest):
    """Copy setup's file list plus the extra files into dest (fresh)."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    setup.copy_program(HERE, dest)
    os.remove(os.path.join(dest, setup.MARKER))  # setup writes its own marker on install
    for name in EXTRA_FILES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(dest, name))


def check(dest):
    """Every expected file there with the right size, nothing forbidden. Returns the list."""
    expected = setup.expected_files(HERE)
    for name in EXTRA_FILES:
        expected[name] = os.path.getsize(os.path.join(HERE, name))
    found = []
    for folder, _, files in os.walk(dest):
        for f in files:
            found.append(os.path.relpath(os.path.join(folder, f), dest))
    problems = []
    for rel, size in expected.items():
        full = os.path.join(dest, rel)
        if not os.path.isfile(full) or os.path.getsize(full) != size:
            problems.append("missing or changed: " + rel)
    for rel in setup.REQUIRED + REQUIRED_EXTRA:
        if rel not in found:
            problems.append("required file missing: " + rel)
    for rel in found:
        parts = rel.lower().split(os.sep)
        if rel not in expected:
            problems.append("unexpected file: " + rel)
        if rel.lower().endswith(FORBIDDEN_EXT) or any(p in FORBIDDEN_PARTS for p in parts[:-1]) \
                or os.path.basename(rel).lower().startswith("test_"):
            problems.append("forbidden file: " + rel)
    if problems:
        raise ReleaseError("staged files are wrong:\n  " + "\n  ".join(problems))
    return sorted(found)


def self_test(dest, out):
    """The staged program's own --self-test (read-only), hidden, no window."""
    py = setup.python_exes()[0]
    p = subprocess.run([py, "-I", os.path.join(dest, "volume_knob.py"), "--self-test",
                        "--out", out], cwd=dest, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120, creationflags=0x08000000)  # CREATE_NO_WINDOW
    text = open(out, encoding="utf-8").read() if os.path.exists(out) else "(no output)"
    if p.returncode != 0 or "RESULT: healthy" not in text:
        raise ReleaseError("self-test of the staged copy failed (exit %d):\n%s"
                           % (p.returncode, text))


def make_zip(dest, files, path):
    tmp = path + ".new"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            z.write(os.path.join(dest, rel), TOP + "/" + rel.replace(os.sep, "/"))
    with zipfile.ZipFile(tmp) as z:
        if z.testzip() is not None or sorted(z.namelist()) != sorted(
                TOP + "/" + r.replace(os.sep, "/") for r in files):
            raise ReleaseError("zip check failed")
    os.replace(tmp, path)


def main(argv=None):
    p = argparse.ArgumentParser(prog="make_release")
    p.add_argument("--version", required=True)
    args = p.parse_args(argv)
    try:
        if args.version != read_version():
            raise ReleaseError("--version %s doesn't match version.py (%s)"
                               % (args.version, read_version()))
        dist = os.path.join(HERE, "dist")
        os.makedirs(dist, exist_ok=True)
        dest = os.path.join(dist, "stage", TOP)
        stage(dest)
        files = check(dest)
        self_test(dest, os.path.join(dist, "selftest.txt"))
        path = os.path.join(dist, "8bitdo-volume-knob-%s.zip" % args.version)
        make_zip(dest, files, path)
    except (ReleaseError, setup.SetupError, OSError, subprocess.SubprocessError) as e:
        print("FAILED: %s" % e)
        return 1
    print("%d files, %d bytes" % (len(files), os.path.getsize(path)))
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
