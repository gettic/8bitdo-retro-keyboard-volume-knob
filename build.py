"""Build the release zip: dist\\8bitdo-volume-knob-<version>.zip

    python -I build.py --version <version>      (run from anywhere; stdlib only)

Steps: check the version against version.py, run PyInstaller (onedir, no console,
no UPX, version resource) from build\\venv, stage dist\\8BitDo Volume Knob\\ (exe,
_internal, extension, Install.cmd, Uninstall.cmd), run the exe's --self-test, zip it.
The last line printed is the zip path. Exit 0 = OK, 1 = failed, 3 = build venv missing.

build\\venv is made only with --allow-download (the one download: PyInstaller from
PyPI, versions and hashes pinned in build-requirements.txt).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

CODE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(CODE, "build")
DIST = os.path.join(CODE, "dist")
VENV = os.path.join(BUILD, "venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")
STAGE_NAME = "8BitDo Volume Knob"
CREATE_NO_WINDOW = 0x08000000


def fail(msg, code=1):
    print("build failed: " + msg)
    sys.exit(code)


def read_version():
    with open(os.path.join(CODE, "version.py"), encoding="utf-8") as f:
        m = re.search(r'^VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', f.read(), re.M)
    if not m:
        fail("version.py has no VERSION = \"x.y.z\"")
    return m.group(0).split('"')[1], tuple(int(x) for x in m.groups())


def run(cmd, **kw):
    print("> " + " ".join(cmd))
    r = subprocess.run(cmd, creationflags=CREATE_NO_WINDOW, **kw)
    if r.returncode != 0:
        fail("%s exited with %d" % (os.path.basename(cmd[0]), r.returncode))


def ensure_venv(allow_download):
    if os.path.isfile(VENV_PY):
        r = subprocess.run([VENV_PY, "-I", "-c", "import PyInstaller"],
                           creationflags=CREATE_NO_WINDOW, capture_output=True)
        if r.returncode == 0:
            return
    if not allow_download:
        fail("build\\venv with PyInstaller is missing; run once with --allow-download "
             "(downloads PyInstaller from PyPI, pinned in build-requirements.txt)", 3)
    run([sys.executable, "-m", "venv", VENV])
    run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes",
         "-r", os.path.join(CODE, "build-requirements.txt")])


def version_file(text, nums):
    v4 = nums + (0,)
    strings = {
        "CompanyName": "gettic",
        "FileDescription": "8BitDo Retro Keyboard volume knob",
        "FileVersion": text,
        "InternalName": "volume_knob",
        "LegalCopyright": "gettic",
        "OriginalFilename": "volume_knob.exe",
        "ProductName": "8BitDo Retro Keyboard volume knob",
        "ProductVersion": text,
    }
    items = ",\n".join("          StringStruct(%r, %r)" % kv for kv in strings.items())
    body = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)r, prodvers=%(v)r, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
%(items)s])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"v": v4, "items": items}
    path = os.path.join(BUILD, "version_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def pyinstaller(vfile):
    run([VENV_PY, "-I", "-m", "PyInstaller", os.path.join(CODE, "volume_knob.py"),
         "--name", "volume_knob", "--onedir", "--noconsole", "--noupx",
         "--clean", "--noconfirm", "--log-level", "WARN",
         "--paths", CODE,
         "--hidden-import", "native_host.volume_knob_host",
         "--hidden-import", "knob.selftest",
         "--hidden-import", "knob.coreaudio",
         "--hidden-import", "setup_knob",
         "--hidden-import", "setup_knob.setup",
         "--exclude-module", "tkinter",
         "--exclude-module", "unittest",
         "--exclude-module", "pydoc",
         "--version-file", vfile,
         "--specpath", BUILD,
         "--workpath", os.path.join(BUILD, "work"),
         "--distpath", os.path.join(BUILD, "pyi")], cwd=CODE)


def stage():
    out = os.path.join(DIST, STAGE_NAME)
    if os.path.exists(out):
        shutil.rmtree(out)  # previous build output only
    shutil.copytree(os.path.join(BUILD, "pyi", "volume_knob"), out)
    shutil.copytree(os.path.join(CODE, "extension"), os.path.join(out, "extension"))
    for name in ("Install.cmd", "Uninstall.cmd"):
        src = os.path.join(CODE, "packaging", name)
        with open(src, encoding="utf-8") as f:
            text = f.read().replace("\r\n", "\n")
        with open(os.path.join(out, name), "w", encoding="ascii", newline="\r\n") as f:
            f.write(text)
    return out


def self_test(folder):
    exe = os.path.join(folder, "volume_knob.exe")
    out = os.path.join(BUILD, "selftest.txt")
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run([exe, "--self-test", "--out", out], creationflags=CREATE_NO_WINDOW,
                       timeout=120)
    if os.path.exists(out):
        with open(out, encoding="utf-8", errors="replace") as f:
            print(f.read().rstrip())
    if r.returncode != 0:
        fail("self-test exited with %d" % r.returncode)


def make_zip(folder, version):
    path = os.path.join(DIST, "8bitdo-volume-knob-%s.zip" % version)
    if os.path.exists(path):
        os.remove(path)  # previous build output only
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                arc = os.path.join(STAGE_NAME, os.path.relpath(full, folder))
                z.write(full, arc.replace("\\", "/"))
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            fail("zip check failed at " + bad)
        if STAGE_NAME + "/volume_knob.exe" not in z.namelist():
            fail("zip has no volume_knob.exe")
    return path


def main():
    p = argparse.ArgumentParser(description="Build the release zip.")
    p.add_argument("--version", help="must match version.py (a leading v is allowed)")
    p.add_argument("--no-test", action="store_true", help="skip the exe self-test")
    p.add_argument("--allow-download", action="store_true",
                   help="create build\\venv and install PyInstaller from PyPI if missing")
    args = p.parse_args()
    text, nums = read_version()
    if args.version and args.version.lstrip("vV") != text:
        fail("--version %s does not match version.py (%s)" % (args.version, text))
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(DIST, exist_ok=True)
    ensure_venv(args.allow_download)
    pyinstaller(version_file(text, nums))
    folder = stage()
    if not args.no_test:
        self_test(folder)
    print(make_zip(folder, text))


if __name__ == "__main__":
    main()
