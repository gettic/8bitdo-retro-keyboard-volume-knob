"""Per-user setup and uninstall (no admin rights, HKCU and the user's own folders only).

volume_knob.exe --install     register the Brave bridge, copy the extension, start with Windows
volume_knob.exe --uninstall   stop the program and remove all of that again

Test options: --test-root <dir> moves every folder under <dir> and every registry
entry under HKCU\\Software\\8BitDoVolumeKnob-installer-test-<id>, and never stops or
starts the real program. --quiet: no message box. --out <file>: write the result there.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

APP = "8BitDo Volume Knob"
HOST_NAME = "com.gettic.volume_knob"
EXTENSION_ID = "ocheindfkmmnaekoolhggndpapplccpd"
HOST_DESCRIPTION = ("8BitDo Retro Keyboard volume knob: bridge between the Brave extension "
                    "and the program")
RUN_VALUE = APP
CHROME_HOSTS = r"Software\Google\Chrome\NativeMessagingHosts"
BRAVE_HOSTS = r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
TEST_PREFIX = "Software\\8BitDoVolumeKnob-installer-test-"
HKCU = winreg.HKEY_CURRENT_USER


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- where things go
class Layout:
    """All paths setup writes. With a test root, nothing real is touched."""

    def __init__(self, test_root=None):
        self.test = bool(test_root)
        if test_root:
            root = os.path.abspath(test_root)
            ident = hashlib.sha1(os.path.normcase(root).encode("utf-8")).hexdigest()[:12]
            self.reg_base = TEST_PREFIX + ident + "\\"
            local = os.path.join(root, "localappdata")
            roaming = os.path.join(root, "appdata")
        else:
            self.reg_base = ""
            local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            roaming = os.environ.get("APPDATA") or os.path.expanduser("~")
        self.local_dir = os.path.join(local, APP)          # manifest, extension, logs
        self.roaming_dir = os.path.join(roaming, APP)      # settings.json (program's)
        self.extension_dir = os.path.join(self.local_dir, "extension")
        self.manifest = os.path.join(self.local_dir, HOST_NAME + ".json")
        self.chrome_key = self.reg_base + CHROME_HOSTS + "\\" + HOST_NAME
        self.brave_key = self.reg_base + BRAVE_HOSTS + "\\" + HOST_NAME
        self.run_key = self.reg_base + RUN_KEY
        self.approved_key = self.reg_base + APPROVED_KEY

    @property
    def test_key(self):
        return self.reg_base.rstrip("\\") if self.test else None


def program_files():
    """The program's own files: the frozen exe's folder, or code\\ when run from source."""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        folder = os.path.dirname(exe)
        return {"folder": folder, "extension": os.path.join(folder, "extension"),
                "host": exe, "start": [exe]}
    code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyw = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = os.path.abspath(sys.executable)
    return {"folder": code, "extension": os.path.join(code, "extension"),
            "host": os.path.join(code, "native_host", "run_host.cmd"),
            "start": [pyw, os.path.join(code, "volume_knob.py")]}


def _inside(path, folder):
    p = os.path.normcase(os.path.realpath(path))
    f = os.path.normcase(os.path.realpath(folder))
    return p == f or p.startswith(f.rstrip("\\") + "\\")


def in_temp(path):
    return _inside(path, tempfile.gettempdir())


def command_line(args):
    return " ".join('"%s"' % a for a in args)


# ---------------------------------------------------------------- registry
def _set_default(path, value):
    with winreg.CreateKeyEx(HKCU, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, value)


def _set_value(path, name, value):
    with winreg.CreateKeyEx(HKCU, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def read_value(path, name=""):
    """Value or None (used by tests too)."""
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


def key_exists(path):
    try:
        winreg.CloseKey(winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ))
        return True
    except FileNotFoundError:
        return False


def _delete_value(path, name):
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return False


def _delete_key(path):
    try:
        winreg.DeleteKey(HKCU, path)
        return True
    except FileNotFoundError:
        return False


def _is_empty(path):
    try:
        with winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ) as k:
            subkeys, values, _ = winreg.QueryInfoKey(k)
            return subkeys == 0 and values == 0
    except FileNotFoundError:
        return False


def _prune_empty(path, stop_at):
    """Delete path and its parents while they hold nothing at all, never stop_at itself."""
    while path.lower() != stop_at.lower() and "\\" in path and _is_empty(path):
        _delete_key(path)
        path = path.rsplit("\\", 1)[0]


def delete_test_tree(path):
    """Remove a test registry tree. Refuses anything outside the test prefix."""
    if not path.startswith(TEST_PREFIX):
        raise SetupError("refusing to delete a non-test registry key")
    try:
        k = winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return
    with k:
        subs = []
        i = 0
        while True:
            try:
                subs.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
    for s in subs:
        delete_test_tree(path + "\\" + s)
    _delete_key(path)


# ---------------------------------------------------------------- Recycle Bin
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR),
                ("pTo", wt.LPCWSTR), ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wt.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wt.LPCWSTR)]


FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


def shell_recycle(path):
    """SHFileOperationW to the Recycle Bin. Returns (error code, aborted).
    Tests replace this with a fake (see test_setup.py) so the real bin is untouched."""
    buf = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0")  # double null end
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wt.LPCWSTR)
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = (ctypes.POINTER(_SHFILEOPSTRUCTW),)
    shell32.SHFileOperationW.restype = ctypes.c_int
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    return rc, bool(op.fAnyOperationsAborted)


def recycle(path):
    """Move a file or folder to the Recycle Bin, silently. True if it's gone."""
    if not os.path.exists(path):
        return True
    rc, aborted = shell_recycle(path)
    return rc == 0 and not aborted and not os.path.exists(path)


# ---------------------------------------------------------------- the program
def stop_program(layout, notes):
    if layout.test:
        return
    from knob import app
    if not app.is_running():
        return
    app.request_stop()
    end = time.monotonic() + 5
    while app.is_running() and time.monotonic() < end:
        time.sleep(0.1)
    if app.is_running():
        notes.append("The running program did not stop within 5 seconds.")


def start_program(files):
    subprocess.Popen(files["start"], cwd=files["folder"], close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200 | 0x08000000)
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


# ---------------------------------------------------------------- install / uninstall
def install(layout, files, start=True):
    notes = []
    if not layout.test and in_temp(files["folder"]):
        raise SetupError("The program is in a temporary folder (probably still inside the "
                         "zip). Unzip the whole folder somewhere permanent first, then run "
                         "Install.cmd from there.")
    if not os.path.isfile(os.path.join(files["extension"], "manifest.json")):
        raise SetupError("The 'extension' folder is missing next to the program.")
    if not os.path.isfile(files["host"]):
        raise SetupError("Program file not found: " + files["host"])

    stop_program(layout, notes)

    os.makedirs(layout.local_dir, exist_ok=True)
    shutil.copytree(files["extension"], layout.extension_dir, dirs_exist_ok=True)

    manifest = {"name": HOST_NAME, "description": HOST_DESCRIPTION, "path": files["host"],
                "type": "stdio", "allowed_origins": ["chrome-extension://%s/" % EXTENSION_ID]}
    tmp = layout.manifest + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # UTF-8 without BOM
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.replace(tmp, layout.manifest)

    _set_default(layout.chrome_key, layout.manifest)
    _set_default(layout.brave_key, layout.manifest)
    _set_value(layout.run_key, RUN_VALUE, command_line(files["start"]))

    if start and not layout.test:
        start_program(files)
    return notes


def uninstall(layout):
    notes = []
    stop_program(layout, notes)

    _delete_key(layout.chrome_key)
    _delete_key(layout.brave_key)
    software = layout.reg_base + "Software"
    _prune_empty(layout.reg_base + CHROME_HOSTS, software)
    _prune_empty(layout.reg_base + BRAVE_HOSTS, software)
    _delete_value(layout.run_key, RUN_VALUE)
    _delete_value(layout.approved_key, RUN_VALUE)

    for folder in (layout.local_dir, layout.roaming_dir):
        if not os.path.exists(folder):
            continue
        if layout.test:
            shutil.rmtree(folder)
        elif not recycle(folder):
            notes.append("Could not move %s to the Recycle Bin (a file may be in use)." % folder)

    if layout.test:
        delete_test_tree(layout.test_key)
    return notes


# ---------------------------------------------------------------- messages
def _show(text, error=False):
    try:
        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.WinDLL("user32").MessageBoxW(None, text, APP, flags)
    except OSError:
        pass


def _report(text, args, error=False):
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
    if sys.stdout is not None:
        try:
            print(text)
        except OSError:
            pass
    if not args.quiet:
        _show(text, error)


def run(argv):
    p = argparse.ArgumentParser(prog="volume_knob")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    p.add_argument("--test-root", help="tests: put everything under this folder")
    p.add_argument("--quiet", action="store_true", help="no message box")
    p.add_argument("--no-start", action="store_true", help="don't start the program")
    p.add_argument("--out", help="write the result to this file")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    layout = Layout(args.test_root)
    try:
        if args.install:
            files = program_files()
            notes = install(layout, files, start=not args.no_start)
            text = ("8BitDo Volume Knob is set up%s and starts with Windows.\n\n"
                    "One step left for YouTube in Brave:\n"
                    "1. Open brave://extensions\n"
                    "2. Turn on Developer mode\n"
                    "3. Click Load unpacked and choose this folder:\n%s\n\n"
                    "To update later: close Brave, unzip the new version over the old "
                    "folder, and run Install.cmd again."
                    % ("" if args.no_start or layout.test else ", running", layout.extension_dir))
        else:
            notes = uninstall(layout)
            text = ("8BitDo Volume Knob is removed (settings and logs are in the Recycle "
                    "Bin).\n\nStill to do by hand: remove the extension in "
                    "brave://extensions, and delete the program folder.")
    except (SetupError, OSError) as e:
        _report("Setup failed: %s" % e, args, error=True)
        return 1
    if notes:
        text += "\n\n" + "\n".join(notes)
    _report(text, args)
    return 0
