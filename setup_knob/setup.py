"""Per-user setup and uninstall.

From source (python.org Python, no exe, no packages):
  py -I volume_knob.py --install
      One admin box (shown as Python): only the copy runs elevated. It copies the program
      and the Brave extension to C:\\Program Files\\8BitDo Volume Knob, waits, checks every
      file is still there, and writes a result file. Then, as the normal user: a shortcut
      in the user's Startup folder (pythonw.exe -I volume_knob.py), and start it.
      --run-value: start with Windows through the HKCU Run value instead of the shortcut.
  py -I "C:\\Program Files\\8BitDo Volume Knob\\volume_knob.py" --uninstall
      Shortcut, Run value, logs and settings as the user, then one admin box to recycle
      the folder (only if it holds setup's marker file).
  Run from the installed copy itself, --install only registers again (no admin box).
--elevated-copy / --elevated-remove are internal: setup runs them through the admin box.
Brave talks to the program over a local connection; there is no native-messaging host.
Old host keys and files from earlier versions are cleaned up by install and uninstall.

Test options: --test-root <dir> moves every folder under <dir> and every registry
entry under HKCU\\Software\\8BitDoVolumeKnob-installer-test-<id>, and never stops or
starts the real program. --quiet: no message box. --out <file>: write the result there.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import fnmatch
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
DESCRIPTION = "8BitDo Retro Keyboard volume knob"
RUN_VALUE = APP
SHORTCUT = APP + ".lnk"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
                       r"\StartupFolder")
STARTUP = r"Microsoft\Windows\Start Menu\Programs\Startup"   # under %APPDATA%
# Earlier versions only (cleanup): the native-messaging host registration and files.
OLD_HOST = "com.gettic.volume_knob"
OLD_HOST_PARENTS = (r"Software\Google\Chrome\NativeMessagingHosts",
                    r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts")
OLD_PROGRAM_FILES = ("native_host", "volume_knob_host.cmd", OLD_HOST + ".json",
                     os.path.join("knob", "pipe_server.py"))
OLD_DATA_FILES = (OLD_HOST + ".json", "extension")   # exe versions put these in %LOCALAPPDATA%
TEST_PREFIX = "Software\\8BitDoVolumeKnob-installer-test-"
HKCU = winreg.HKEY_CURRENT_USER
MARKER = "installed-from-source.txt"       # marks a program copy setup made (uninstall removes it)
VERIFY_WAIT = 30                            # seconds the elevated copy waits before checking
ELEVATED_TIMEOUT = 600                      # seconds to wait for the elevated step to finish
COPY_FILES = ("volume_knob.py", "version.py", "LICENSE")
COPY_DIRS = ("knob", "setup_knob", "extension")
COPY_SKIP = ("__pycache__", "*.pyc", "test_*.py")
REQUIRED = ("volume_knob.py", os.path.join("knob", "app.py"),
            os.path.join("knob", "ws_server.py"), os.path.join("extension", "manifest.json"))


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- where things go
class Layout:
    """All paths setup writes. With a test root, nothing real is touched."""

    def __init__(self, test_root=None):
        self.test = bool(test_root)
        self.root = os.path.abspath(test_root) if test_root else None
        if test_root:
            root = self.root
            ident = hashlib.sha1(os.path.normcase(root).encode("utf-8")).hexdigest()[:12]
            self.reg_base = TEST_PREFIX + ident + "\\"
            local = os.path.join(root, "localappdata")
            roaming = os.path.join(root, "appdata")
            progfiles = os.path.join(root, "programfiles")
        else:
            self.reg_base = ""
            local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            roaming = os.environ.get("APPDATA") or os.path.expanduser("~")
            progfiles = (os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
                         or r"C:\Program Files")
        self.local_dir = os.path.join(local, APP)          # logs (the program's)
        self.roaming_dir = os.path.join(roaming, APP)      # settings.json (the program's)
        self.startup_dir = os.path.join(roaming, STARTUP)
        self.shortcut = os.path.join(self.startup_dir, SHORTCUT)
        self.program_dir = os.path.join(progfiles, APP)    # program + extension (admin copy)
        self.result_file = os.path.join(self.local_dir, "setup-result.json")
        self.run_key = self.reg_base + RUN_KEY
        self.approved_key = self.reg_base + APPROVED_KEY
        self.approved_folder_key = self.reg_base + APPROVED_FOLDER_KEY
        self.old_host_keys = [self.reg_base + p + "\\" + OLD_HOST for p in OLD_HOST_PARENTS]

    @property
    def test_key(self):
        return self.reg_base.rstrip("\\") if self.test else None


def python_exes():
    """(python.exe, pythonw.exe) of the real Python install (not a venv, never the
    Microsoft Store stub in WindowsApps)."""
    base = os.path.abspath(getattr(sys, "base_prefix", "") or
                           os.path.dirname(os.path.abspath(sys.executable)))
    py = os.path.join(base, "python.exe")
    pyw = os.path.join(base, "pythonw.exe")
    for exe in (py, pyw):
        if "\\windowsapps\\" in os.path.normcase(exe):
            raise SetupError("This Python is the Microsoft Store one (%s). Use the Python "
                             "from python.org instead." % exe)
        if not os.path.isfile(exe):
            raise SetupError("Python file not found: " + exe)
    return py, pyw


def source_files(folder, python, pythonw):
    """Paths when the program runs from its .py files in folder."""
    return {"source": True, "folder": folder, "extension": os.path.join(folder, "extension"),
            "python": python, "pythonw": pythonw,
            "start": [pythonw, "-I", os.path.join(folder, "volume_knob.py")]}


def program_files():
    """The program's own files: the frozen exe's folder, or code\\ when run from source."""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        folder = os.path.dirname(exe)
        return {"source": False, "folder": folder, "extension": os.path.join(folder, "extension"),
                "start": [exe]}
    code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return source_files(code, *python_exes())


def _skipped(name):
    return any(fnmatch.fnmatch(name, p) for p in COPY_SKIP)


def check_source(src):
    for rel in COPY_FILES + COPY_DIRS + REQUIRED:
        if not os.path.exists(os.path.join(src, rel)):
            raise SetupError("Program file missing: " + os.path.join(src, rel))


def copy_program(src, dest):
    """Copy the program's runtime files (an allowlist, no tests or build files) to dest."""
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
        return
    check_source(src)
    os.makedirs(dest, exist_ok=True)
    for name in COPY_FILES:
        shutil.copy2(os.path.join(src, name), os.path.join(dest, name))
    for name in COPY_DIRS:
        shutil.copytree(os.path.join(src, name), os.path.join(dest, name),
                        ignore=shutil.ignore_patterns(*COPY_SKIP), dirs_exist_ok=True)
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8") as f:
        f.write("Copied here by 8BitDo Volume Knob setup. Uninstall moves this folder to "
                "the Recycle Bin.\n")


def expected_files(src):
    """{relative path: size} of everything copy_program copies from src."""
    check_source(src)
    out = {}
    for name in COPY_FILES:
        out[name] = os.path.getsize(os.path.join(src, name))
    for name in COPY_DIRS:
        top = os.path.join(src, name)
        for folder, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if not _skipped(d)]
            for f in files:
                if not _skipped(f):
                    full = os.path.join(folder, f)
                    out[os.path.relpath(full, src)] = os.path.getsize(full)
    return out


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _check_python(python):
    if (os.path.basename(python).lower() != "python.exe" or not os.path.isfile(python)
            or "\\windowsapps\\" in os.path.normcase(python)):
        raise SetupError("Not a usable python.exe: " + python)


def _remove_path(layout, path):
    """Remove a file or folder (test root: delete; real: Recycle Bin). True if gone."""
    if not os.path.exists(path):
        return True
    if layout.test:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
        return not os.path.exists(path)
    return recycle(path)


def copy_and_verify(layout, src, dest, python, wait=None):
    """The elevated step: copy, remove old-version leftovers, wait, check every file is
    still there with the right size. On any loss the folder is removed again."""
    if not _same(dest, layout.program_dir):
        raise SetupError("Refusing to copy to " + dest)
    _check_python(python)
    expected = expected_files(src)
    try:
        copy_program(src, dest)
        for rel in OLD_PROGRAM_FILES:  # only what this version no longer ships
            if rel not in expected and not any(k.startswith(rel + os.sep) for k in expected):
                _remove_path(layout, os.path.join(dest, rel))
    except (OSError, SetupError) as e:  # e.g. a file write refused: take it all back out
        removed = _remove_path(layout, dest)
        return {"ok": False, "step": "copy", "error": str(e)[:2000], "removed": removed}
    time.sleep(VERIFY_WAIT if wait is None else wait)
    missing = [rel for rel, size in sorted(expected.items())
               if not os.path.isfile(os.path.join(dest, rel))
               or os.path.getsize(os.path.join(dest, rel)) != size]
    if not os.path.isfile(os.path.join(dest, MARKER)):
        missing.append(MARKER)
    if missing:
        removed = _remove_path(layout, dest)
        return {"ok": False, "step": "copy", "missing": missing, "removed": removed,
                "checked": len(expected)}
    return {"ok": True, "step": "copy", "checked": len(expected)}


def remove_guarded(layout, dest):
    """The elevated uninstall step: only the exact program folder, only with the marker."""
    if not _same(dest, layout.program_dir):
        return {"ok": False, "step": "remove", "error": "wrong folder: " + dest}
    if not os.path.exists(dest):
        return {"ok": True, "step": "remove", "note": "already gone"}
    if not (os.path.isfile(os.path.join(dest, MARKER)) and
            os.path.isfile(os.path.join(dest, "volume_knob.py"))):
        return {"ok": False, "step": "remove", "error": "no setup marker, folder left alone"}
    return {"ok": _remove_path(layout, dest), "step": "remove"}


# ---------------------------------------------------------------- the one admin box
class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE)]


SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_NOASYNC = 0x100
SEE_MASK_FLAG_NO_UI = 0x400
ERROR_CANCELLED = 1223


def shell_runas(exe, params, folder, timeout):
    """Start exe elevated (the normal UAC box) and wait. Returns the exit code.
    Tests replace this (see test_setup.py): the real box can't run in tests."""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = folder
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_SHELLEXECUTEINFOW),)
    shell32.ShellExecuteExW.restype = wt.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise SetupError("The admin box was answered No. Nothing was changed.")
        raise SetupError("Could not start the admin step (error %d)." % err)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
    k32.GetExitCodeProcess.argtypes = (wt.HANDLE, ctypes.POINTER(wt.DWORD))
    k32.CloseHandle.argtypes = (wt.HANDLE,)
    try:
        if k32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise SetupError("The admin step did not finish in time.")
        code = wt.DWORD()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(info.hProcess)


def run_elevated(layout, args):
    """Run setup with args through one admin box; return its result dict."""
    pythonw = python_exes()[1]
    # This file itself (stdlib only), not volume_knob.py: its entry only passes
    # --install/--uninstall on to setup.
    script = os.path.abspath(__file__)
    os.makedirs(layout.local_dir, exist_ok=True)
    if os.path.exists(layout.result_file):
        os.remove(layout.result_file)
    full = list(args) + ["--result", layout.result_file]
    if layout.test:
        full += ["--test-root", layout.root, "--wait", str(VERIFY_WAIT)]
    # Working folder outside the program folder, so the remove step doesn't hold it open.
    shell_runas(pythonw, subprocess.list2cmdline(["-I", script] + full),
                os.path.dirname(layout.local_dir), ELEVATED_TIMEOUT)
    try:
        with open(layout.result_file, encoding="utf-8") as f:
            result = json.load(f)
        os.remove(layout.result_file)
    except (OSError, ValueError):
        raise SetupError("The admin step left no result.")
    return result


# ---------------------------------------------------------------- Startup shortcut
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"


def _guid(text):
    g = _GUID()
    ole32 = ctypes.OleDLL("ole32")
    ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _vcall(ptr, index, *args, argtypes=()):
    """Call COM method number index on interface pointer ptr (raises OSError on failure)."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def _release(ptr):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)


class _ShellLink:
    """IShellLinkW + IPersistFile through plain ctypes (shell32's own shortcut object)."""

    def __enter__(self):
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        self.uninit = hr in (0, 1)
        self.link = ctypes.c_void_p()
        self.file = ctypes.c_void_p()
        try:
            ctypes.OleDLL("ole32").CoCreateInstance(
                ctypes.byref(_guid(CLSID_SHELLLINK)), None, 1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(_guid(IID_ISHELLLINKW)), ctypes.byref(self.link))
            _vcall(self.link, 0, ctypes.byref(_guid(IID_IPERSISTFILE)),
                   ctypes.byref(self.file), argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        except OSError:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        for p in (self.file, self.link):
            if p:
                _release(p)
        if self.uninit:
            ctypes.WinDLL("ole32").CoUninitialize()

    def set(self, index, text):
        _vcall(self.link, index, text, argtypes=(wt.LPCWSTR,))

    def get(self, index, *extra, argtypes=()):
        buf = ctypes.create_unicode_buffer(1024)
        _vcall(self.link, index, buf, 1024, *extra,
               argtypes=(wt.LPWSTR, ctypes.c_int) + argtypes)
        return buf.value


def write_shortcut(path, target, arguments, workdir, description=DESCRIPTION):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _ShellLink() as s:
        s.set(20, target)          # SetPath
        s.set(11, arguments)       # SetArguments
        s.set(9, workdir)          # SetWorkingDirectory
        s.set(7, description)      # SetDescription
        _vcall(s.file, 6, path, True, argtypes=(wt.LPCWSTR, wt.BOOL))  # IPersistFile.Save


def read_shortcut(path):
    """{target, arguments, workdir, description} of a .lnk (used by tests and checks)."""
    with _ShellLink() as s:
        _vcall(s.file, 5, path, 0, argtypes=(wt.LPCWSTR, wt.DWORD))  # IPersistFile.Load
        return {"target": s.get(3, None, 4, argtypes=(ctypes.c_void_p, wt.DWORD)),  # RAWPATH
                "arguments": s.get(10), "workdir": s.get(8), "description": s.get(6)}


def shortcut_args(files):
    """The shortcut's argument string: everything after the program in files['start']."""
    return subprocess.list2cmdline(files["start"][1:])


# ---------------------------------------------------------------- small helpers
def _inside(path, folder):
    p = os.path.normcase(os.path.realpath(path))
    f = os.path.normcase(os.path.realpath(folder))
    return p == f or p.startswith(f.rstrip("\\") + "\\")


def in_temp(path):
    return _inside(path, tempfile.gettempdir())


def command_line(args):
    return " ".join(a if a.startswith("-") else '"%s"' % a for a in args)


# ---------------------------------------------------------------- registry
def _set_value(path, name, value):
    with winreg.CreateKeyEx(HKCU, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _set_default(path, value):
    _set_value(path, "", value)


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


def _try(notes, what, fn, *args):
    """Run a cleanup step; a refusal (e.g. Access is denied) becomes a note."""
    try:
        return fn(*args)
    except PermissionError as e:
        notes.append("Could not remove %s (%s)." % (what, e.strerror or e))
    except OSError as e:
        notes.append("Could not remove %s (%s)." % (what, e))
    return False


def remove_old_host(layout, notes):
    """Earlier versions' native-messaging host keys (and empty parents). Never fails."""
    software = layout.reg_base + "Software"
    for key in layout.old_host_keys:
        if key_exists(key) and _try(notes, "old browser key HKCU\\" + key, _delete_key, key):
            _try(notes, "empty key", _prune_empty, key.rsplit("\\", 1)[0], software)


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


def _is_our_shortcut(path):
    try:
        info = read_shortcut(path)
    except OSError:
        return False
    return "volume_knob.py" in info["arguments"] or \
        os.path.basename(info["target"]).lower() == "volume_knob.exe"


def remove_shortcut(layout, notes):
    if not os.path.exists(layout.shortcut):
        return
    if not _is_our_shortcut(layout.shortcut):
        notes.append("%s doesn't point to this program, left alone." % layout.shortcut)
        return
    if not _try(notes, layout.shortcut, _remove_path, layout, layout.shortcut):
        notes.append("Could not remove %s." % layout.shortcut)


# ---------------------------------------------------------------- install / uninstall
def install(layout, files, start=True, method="shortcut"):
    """Returns (notes, files actually used). method: "shortcut" (Startup folder, default)
    or "run" (HKCU Run value). Run from the installed copy itself, no admin box."""
    notes = []
    source = files.get("source", False)
    if not source and not layout.test and in_temp(files["folder"]):
        raise SetupError("The program is in a temporary folder (probably still inside the "
                         "zip). Unzip the whole folder somewhere permanent first.")
    if not os.path.isfile(os.path.join(files["extension"], "manifest.json")):
        raise SetupError("The 'extension' folder is missing next to the program.")

    stop_program(layout, notes)

    os.makedirs(layout.local_dir, exist_ok=True)
    if source:
        if _same(files["folder"], layout.program_dir) and \
                os.path.isfile(os.path.join(layout.program_dir, MARKER)):
            pass  # already the installed copy: just register again, no admin box
        else:
            # The only elevated part: copy into Program Files, verified.
            result = run_elevated(layout, ["--elevated-copy", "--src", files["folder"],
                                           "--dest", layout.program_dir,
                                           "--python", files["python"]])
            if not result.get("ok"):
                raise SetupError("Copying to %s failed, nothing was registered. %s"
                                 % (layout.program_dir, json.dumps(result)))
        files = source_files(layout.program_dir, files["python"], files["pythonw"])

    # As the normal user. Leftovers of earlier versions first (best effort).
    remove_old_host(layout, notes)
    for name in OLD_DATA_FILES:
        old = os.path.join(layout.local_dir, name)
        if os.path.exists(old):
            _try(notes, old, _remove_path, layout, old)

    if method == "run":
        _set_value(layout.run_key, RUN_VALUE, command_line(files["start"]))
        remove_shortcut(layout, notes)
    else:
        write_shortcut(layout.shortcut, files["start"][0], shortcut_args(files),
                       files["folder"])
        if read_value(layout.run_key, RUN_VALUE) is not None:
            _try(notes, "the old Run value", _delete_value, layout.run_key, RUN_VALUE)

    if start and not layout.test:
        start_program(files)
    return notes, files


def uninstall(layout):
    notes = []
    stop_program(layout, notes)
    if not layout.test:
        try:  # don't hold the program folder open as the current directory
            os.chdir(os.path.dirname(layout.local_dir))
        except OSError:
            pass

    remove_shortcut(layout, notes)
    for key, name in ((layout.run_key, RUN_VALUE), (layout.approved_key, RUN_VALUE),
                      (layout.approved_folder_key, SHORTCUT)):
        _try(notes, "HKCU\\%s value %s" % (key, name), _delete_value, key, name)
    remove_old_host(layout, notes)

    # Program Files copy: one admin box, only if setup's marker is there.
    if os.path.isfile(os.path.join(layout.program_dir, MARKER)):
        try:
            result = run_elevated(layout, ["--elevated-remove", "--dest", layout.program_dir])
        except SetupError as e:
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            notes.append("The program folder %s is still there (%s). Run uninstall again "
                         "to remove it." % (layout.program_dir, result.get("error", "failed")))

    for folder in (layout.local_dir, layout.roaming_dir):
        if os.path.exists(folder) and not _remove_path(layout, folder):
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


def _elevated(layout, args):
    """Inside the admin box: no message boxes, the result goes to --result."""
    try:
        if not args.result or not args.dest:
            raise SetupError("missing --result/--dest")
        if args.elevated_copy:
            if not args.src or not args.python:
                raise SetupError("missing --src/--python")
            wait = args.wait if (layout.test and args.wait is not None) else None
            result = copy_and_verify(layout, args.src, args.dest, args.python, wait)
        else:
            result = remove_guarded(layout, args.dest)
    except (SetupError, OSError) as e:
        result = {"ok": False, "error": str(e)}
    if args.result:
        try:
            tmp = args.result + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f)
            os.replace(tmp, args.result)
        except OSError:
            pass
    return 0 if result.get("ok") else 1


def run(argv):
    p = argparse.ArgumentParser(prog="volume_knob")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--elevated-copy", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--elevated-remove", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--test-root", help="tests: put everything under this folder")
    p.add_argument("--quiet", action="store_true", help="no message box")
    p.add_argument("--no-start", action="store_true", help="don't start the program")
    p.add_argument("--run-value", action="store_true",
                   help="start with Windows through the HKCU Run value, not a Startup shortcut")
    p.add_argument("--no-browser-bridge", action="store_true",
                   help=argparse.SUPPRESS)  # no-op: there is no browser bridge any more
    p.add_argument("--out", help="write the result to this file")
    for name in ("--src", "--dest", "--python", "--result"):
        p.add_argument(name, help=argparse.SUPPRESS)
    p.add_argument("--wait", type=float, help=argparse.SUPPRESS)  # tests only
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    layout = Layout(args.test_root)
    if args.elevated_copy or args.elevated_remove:
        return _elevated(layout, args)
    try:
        if args.install:
            notes, files = install(layout, program_files(), start=not args.no_start,
                                   method="run" if args.run_value else "shortcut")
            if files.get("source"):
                update = ("To update later: run setup again from the new version's folder:  "
                          "py -I volume_knob.py --install  (one admin box)\n"
                          "To uninstall:  py -I \"%s\" --uninstall  (one admin box)"
                          % os.path.join(layout.program_dir, "volume_knob.py"))
            else:
                update = "To update later: put the new version in the same folder and " \
                         "run --install again."
            text = ("8BitDo Volume Knob is set up%s and starts with Windows.\n\n"
                    "One step left for YouTube in Brave:\n"
                    "1. Open brave://extensions\n"
                    "2. Turn on Developer mode\n"
                    "3. Click Load unpacked and choose this folder:\n%s\n\n%s"
                    % ("" if args.no_start or layout.test else ", running",
                       files["extension"], update))
        else:
            had_copy = os.path.isfile(os.path.join(layout.program_dir, MARKER))
            notes = uninstall(layout)
            text = ("8BitDo Volume Knob is removed (settings and logs are in the Recycle "
                    "Bin).\n\nStill to do by hand: remove the extension in "
                    "brave://extensions" +
                    ("." if had_copy else ", and delete the program folder."))
    except (SetupError, OSError) as e:
        _report("Setup failed: %s" % e, args, error=True)
        return 1
    if notes:
        text += "\n\n" + "\n".join(notes)
    _report(text, args)
    return 0


if __name__ == "__main__":  # the elevated step: pythonw -I setup.py --elevated-... (stdlib only)
    sys.exit(run(sys.argv[1:]))
