"""Spotify's own volume bar through the Spotify Web API (Premium).

- Login data: %APPDATA%\\8BitDo Volume Knob\\spotify.json (client ID in plain text,
  refresh token encrypted with Windows DPAPI for the current user). The access
  token lives in memory only. Tokens, codes and track data are never logged.
- `SpotifyVolume` runs one worker thread: knob clicks are bundled into at most
  one PUT every 0.3 s, the volume is read with GET /me/player when the last
  read is over 5 s old and every 10 s while Spotify runs. It is only "usable"
  while Spotify plays on this PC (the device saved at login, or the Computer
  device named like this PC); otherwise the controller uses the Windows mixer.
Standard library and ctypes only."""
import base64
import ctypes
import ctypes.wintypes as wt
import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("knob.spotify")

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
STORE_NAME = "spotify.json"
SCOPES = "user-read-playback-state user-modify-playback-state"
REDIRECT_PORT = 38480
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
TIMEOUT = 2.0
MAX_BODY = 1024 * 1024

MIN_GAP = 0.3        # seconds between two volume PUTs
REREAD_AFTER = 5.0   # a click after this long without news reads the volume first
POLL = 10.0          # read the volume this often while Spotify runs
RETRY = 30.0         # after a failure, try again this much later
RELOAD_CHECK = 3.0   # look for a new login file this often


def store_path():
    return os.path.join(config.data_dir(), STORE_NAME)


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_Protect = _crypt32.CryptProtectData
_Protect.restype = wt.BOOL
_Protect.argtypes = (ctypes.POINTER(_Blob), wt.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
                     ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob))
_Unprotect = _crypt32.CryptUnprotectData
_Unprotect.restype = wt.BOOL
_Unprotect.argtypes = (ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob))
_LocalFree = _k32.LocalFree
_LocalFree.restype = ctypes.c_void_p
_LocalFree.argtypes = (ctypes.c_void_p,)
_ENTROPY = b"8BitDoVolumeKnob.spotify.v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(data):
    """bytes -> DPAPI blob (current user only)."""
    src, _k1 = _blob(data)
    ent, _k2 = _blob(_ENTROPY)
    out = _Blob()
    if not _Protect(ctypes.byref(src), "8BitDo Volume Knob", ctypes.byref(ent), None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def unprotect(data):
    src, _k1 = _blob(data)
    ent, _k2 = _blob(_ENTROPY)
    out = _Blob()
    if not _Unprotect(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------- login file
def load_store(path):
    """dict(client_id, refresh_token, device_id, device_name) or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cid = raw.get("client_id")
        token = unprotect(base64.b64decode(raw["refresh_token_dpapi"])).decode("utf-8")
        if not isinstance(cid, str) or not cid or not token:
            return None
        return {"client_id": cid, "refresh_token": token,
                "device_id": raw.get("device_id") if isinstance(raw.get("device_id"), str) else None,
                "device_name": raw.get("device_name") if isinstance(raw.get("device_name"), str)
                else None}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_store(path, client_id, refresh_token, device_id=None, device_name=None):
    data = {"client_id": client_id,
            "refresh_token_dpapi": base64.b64encode(protect(refresh_token.encode("utf-8")))
            .decode("ascii"),
            "device_id": device_id, "device_name": device_name}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def delete_store(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an HTTPError: tokens never follow it


def _opener(base):
    host = urllib.parse.urlsplit(base).hostname
    proxies = {} if host == "127.0.0.1" else None  # tests: never through a proxy
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler(proxies))


class Response:
    __slots__ = ("status", "headers", "data")

    def __init__(self, status, headers, data):
        self.status, self.headers, self.data = status, headers, data


_stalled = []  # calls still stuck (e.g. a name lookup urllib's timeout doesn't cover)


def http_call(opener, method, url, headers=None, body=None, timeout=TIMEOUT):
    """-> Response (any HTTP status). Raises OSError when there is no answer
    within about 2 x timeout, whatever part (name lookup included) stalls."""
    _stalled[:] = [t for t in _stalled if t.is_alive()]
    if len(_stalled) >= 2:
        raise OSError("earlier requests still stuck")
    box = {}

    def run():
        try:
            box["r"] = _http_call_now(opener, method, url, headers, body, timeout)
        except BaseException as e:  # handed to the caller below
            box["e"] = e
    t = threading.Thread(target=run, name="spotify-http", daemon=True)
    t.start()
    t.join(timeout * 2 + 0.5)
    if t.is_alive():
        _stalled.append(t)
        raise OSError("timed out")
    if "e" in box:
        raise box["e"]
    return box["r"]


def _http_call_now(opener, method, url, headers=None, body=None, timeout=TIMEOUT):
    req =urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(MAX_BODY + 1)
            status, hdrs = r.status, r.headers
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_BODY + 1)
        except OSError:
            raw = b""
        status, hdrs = e.code, e.headers
        e.close()
    except urllib.error.URLError as e:
        raise OSError(str(getattr(e, "reason", "no connection"))) from None
    except http.client.HTTPException:
        raise OSError("broken answer") from None
    data = None
    if raw and len(raw) <= MAX_BODY:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = None
    return Response(status, hdrs, data)


def token_request(opener, accounts, fields, timeout=TIMEOUT):
    body = urllib.parse.urlencode(fields).encode("ascii")
    return http_call(opener, "POST", accounts + "/api/token",
                {"Content-Type": "application/x-www-form-urlencoded"}, body, timeout)


class ApiError(Exception):
    """A failure with a short, token-free reason and how long to wait."""

    def __init__(self, reason, wait=RETRY, relogin=False):
        super().__init__(reason)
        self.reason, self.wait, self.relogin = reason, wait, relogin


def _retry_after(resp):
    try:
        return min(300.0, max(1.0, float(resp.headers.get("Retry-After", "5"))))
    except (TypeError, ValueError):
        return 5.0


class Client:
    """Blocking Web API calls with token refresh (worker thread only)."""

    def __init__(self, creds, path, api=API, accounts=ACCOUNTS, clock=time.monotonic,
                 timeout=TIMEOUT):
        self.creds = creds
        self.path = path
        self.api, self.accounts = api.rstrip("/"), accounts.rstrip("/")
        self.clock = clock
        self.timeout = timeout
        self._api_opener = _opener(self.api)
        self._acc_opener = _opener(self.accounts)
        self._token = None
        self._expires = 0.0

    def refresh(self):
        try:
            r = token_request(self._acc_opener, self.accounts,
                              {"grant_type": "refresh_token",
                               "refresh_token": self.creds["refresh_token"],
                               "client_id": self.creds["client_id"]}, self.timeout)
        except OSError:
            raise ApiError("offline or Spotify not reachable")
        if r.status == 429:
            raise ApiError("Spotify asked to slow down", _retry_after(r))
        if r.status in (400, 401):
            raise ApiError("Spotify login no longer valid; run --spotify-login again",
                           wait=3600, relogin=True)
        if r.status != 200 or not isinstance(r.data, dict) or \
                not isinstance(r.data.get("access_token"), str):
            raise ApiError(f"Spotify login refresh failed (HTTP {r.status})")
        self._token = r.data["access_token"]
        exp = r.data.get("expires_in")
        self._expires = self.clock() + (float(exp) if isinstance(exp, (int, float)) else 3600) - 60
        new = r.data.get("refresh_token")
        if isinstance(new, str) and new and new != self.creds["refresh_token"]:
            self.creds["refresh_token"] = new  # rotated: keep the new one
            try:
                save_store(self.path, self.creds["client_id"], new, self.creds.get("device_id"),
                           self.creds.get("device_name"))
            except OSError:
                log.warning("could not save the renewed Spotify login")

    def call(self, method, path, query=None):
        """-> Response for 2xx; raises ApiError otherwise."""
        url = self.api + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        for attempt in (1, 2):
            if self._token is None or self.clock() >= self._expires:
                self.refresh()
            try:
                r = http_call(self._api_opener, method, url,
                         {"Authorization": "Bearer " + self._token},
                         b"" if method == "PUT" else None, self.timeout)
            except OSError:
                raise ApiError("offline or Spotify not reachable")
            if r.status == 401 and attempt == 1:
                self._token = None
                continue
            break
        if 200 <= r.status < 300:
            return r
        if r.status == 401:
            raise ApiError("Spotify login no longer valid; run --spotify-login again",
                           wait=3600, relogin=True)
        if r.status == 403:
            raise ApiError("Spotify refused volume control (Premium needed?)", wait=300)
        if r.status == 404:
            raise ApiError("Spotify has no active device", wait=5)
        if r.status == 429:
            raise ApiError("Spotify asked to slow down", _retry_after(r))
        raise ApiError(f"Spotify answered HTTP {r.status}")

    def player(self):
        """Current playback dict, or None when nothing is active (204)."""
        r = self.call("GET", "/me/player")
        return r.data if r.status == 200 and isinstance(r.data, dict) else None

    def devices(self):
        r = self.call("GET", "/me/player/devices")
        d = r.data.get("devices") if isinstance(r.data, dict) else None
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def set_volume(self, percent, device_id):
        self.call("PUT", "/me/player/volume",
                  {"volume_percent": int(percent), "device_id": device_id})


def computer_name():
    return (os.environ.get("COMPUTERNAME") or "").strip().lower()


def is_this_pc(device, creds, pc_name=None):
    """True if a device dict is the Spotify app on this PC."""
    if not isinstance(device, dict):
        return False
    did = device.get("id")
    if creds.get("device_id") and did == creds["device_id"]:
        return True
    pc = computer_name() if pc_name is None else pc_name
    name = device.get("name")
    return (device.get("type") == "Computer" and isinstance(name, str) and bool(pc)
            and name.strip().lower() == pc)


def pick_this_pc(devices, creds, pc_name=None):
    """The device of this PC from a device list: saved id, else name match,
    else the only Computer device. None if unclear."""
    for d in devices:
        if is_this_pc(d, creds, pc_name):
            return d
    computers = [d for d in devices if d.get("type") == "Computer"]
    return computers[0] if len(computers) == 1 else None


def _clamp(v):
    return min(1.0, max(0.0, v))


class SpotifyVolume:
    """Spotify's in-app volume for the controller. The controller-facing
    methods (usable, level, step, set_present) never block on the network."""

    def __init__(self, path=None, api=API, accounts=ACCOUNTS, clock=time.monotonic,
                 on_change=None, timeout=TIMEOUT, pc_name=None, tick=0.05):
        self.path = path or store_path()
        self.api, self.accounts = api, accounts
        self.clock = clock
        self.on_change = on_change
        self.timeout = timeout
        self.pc_name = pc_name
        self.tick = tick
        self._cond = threading.Condition()
        self._stop = False
        self._thread = None
        self._present = False
        self._ok = False
        self._reason = None
        self._known = None       # last volume Spotify reported or we set (0-1)
        self._exact = None       # exact (fractional) target of our last PUT
        self._pending = 0.0      # clicks not sent yet
        self._last_read = None
        self._last_put = None
        self._wait_until = 0.0
        self._client = None
        self._creds = None
        self._mtime = None
        self._loaded = False
        self._checked = None

    # ---- controller side ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name="spotify", daemon=True)
        self._thread.start()

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread:
            self._thread.join(self.timeout * 2 + 1)

    def set_present(self, present):
        with self._cond:
            if present != self._present:
                self._present = present
                self._cond.notify_all()

    def usable(self):
        with self._cond:
            return self._ok and self._present

    def level(self):
        """Volume to show (0-1, incl. clicks not sent yet), or None."""
        with self._cond:
            if not (self._ok and self._present) or self._known is None:
                return None
            base = self._base()
            return _clamp(base + self._pending)

    def step(self, delta):
        """Queue a knob click. Returns the volume to show."""
        with self._cond:
            self._pending += delta
            if self._known is not None:
                base = self._base()
                # never queue more than what can still move the bar
                self._pending = _clamp(base + self._pending) - base
            self._cond.notify_all()
            return None if self._known is None else _clamp(self._base() + self._pending)

    def set_to(self, level):
        """Queue an exact volume (tray menu 25/50/75/100 %). Returns the volume
        to show, or None when Spotify's volume isn't known yet (nothing queued)."""
        with self._cond:
            if self._known is None:
                return None
            self._pending = _clamp(level) - self._base()
            self._cond.notify_all()
            return _clamp(self._base() + self._pending)

    # ---- worker ----
    def _base(self):
        if self._exact is not None and round(self._exact * 100) == round(self._known * 100):
            return self._exact
        return self._known

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("spotify change callback failed")

    def _set_state(self, ok, reason=None):
        with self._cond:
            changed = ok != self._ok or reason != self._reason
            self._ok, self._reason = ok, reason
            if not ok:
                self._pending = 0.0
        if changed:
            if ok:
                log.info("Spotify volume: using Spotify's own volume bar")
            else:
                log.info("Spotify volume: %s; using the Windows mixer", reason)
            self._changed()

    def _reload(self, now):
        if self._checked is not None and now - self._checked < RELOAD_CHECK:
            return
        self._checked = now
        try:
            st = os.stat(self.path)
            mtime = (st.st_mtime_ns, st.st_size)
        except OSError:
            mtime = None
        if self._loaded and mtime == self._mtime:
            return
        self._loaded = True
        self._mtime = mtime
        creds = load_store(self.path) if mtime is not None else None
        self._creds = creds
        self._client = (Client(creds, self.path, self.api, self.accounts, self.clock,
                               self.timeout) if creds else None)
        with self._cond:
            self._wait_until = 0.0
            self._last_read = None
        if creds is None:
            self._set_state(False, "not logged in" if mtime is None
                            else "Spotify login file unreadable; run --spotify-login again")

    def _fail(self, err, now):
        with self._cond:
            self._wait_until = now + err.wait
        self._set_state(False, err.reason)

    def _read(self, now):
        try:
            p = self._client.player()
        except ApiError as e:
            return self._fail(e, now)
        dev = p.get("device") if p else None
        vol = dev.get("volume_percent") if isinstance(dev, dict) else None
        if not p:
            self._fail(ApiError("Spotify has no active device", wait=0), now)
        elif not is_this_pc(dev, self._creds, self.pc_name):
            self._fail(ApiError("Spotify is playing on another device", wait=0), now)
        elif dev.get("supports_volume") is False or isinstance(vol, bool) or \
                not isinstance(vol, (int, float)):
            self._fail(ApiError("this Spotify device has no volume control", wait=0), now)
        else:
            if not self._creds.get("device_id") and isinstance(dev.get("id"), str):
                self._creds["device_id"] = dev["id"]
            with self._cond:
                old = self._known
                self._known = _clamp(vol / 100.0)
                if self._exact is not None and round(self._exact * 100) != round(vol):
                    self._exact = None  # moved in Spotify itself
                self._last_read = now
            self._set_state(True)
            if old != self._known:
                self._changed()
        with self._cond:
            self._last_read = now

    def _send(self, now):
        with self._cond:
            if self._known is None:
                self._pending = 0.0
                return None
            base = self._base()
            new = _clamp(base + self._pending)
            self._pending = 0.0
            dev = self._creds.get("device_id") if self._creds else None
        if not dev:
            return self._fail(ApiError("this PC's Spotify device is not known yet", wait=0), now)
        try:
            self._client.set_volume(round(new * 100), dev)
        except ApiError as e:
            return self._fail(e, now)
        with self._cond:
            self._exact = new
            self._known = round(new * 100) / 100.0
            self._last_put = self._last_read = now
        self._changed()

    def _run(self):
        while True:
            with self._cond:
                if self._stop:
                    return
                # short ticks only while clicks wait; a click wakes us (notify)
                self._cond.wait(self.tick if self._pending else max(self.tick, 0.5))
                if self._stop:
                    return
            try:
                self._work(self.clock())
            except Exception:  # never let the worker die
                log.exception("spotify worker error")
                with self._cond:
                    self._wait_until = self.clock() + RETRY

    def _work(self, now):
        self._reload(now)
        if self._client is None:
            return
        with self._cond:
            present, pending = self._present, self._pending
            waiting = now < self._wait_until
            last_read, last_put = self._last_read, self._last_put
        if not present or waiting:
            if pending:
                with self._cond:
                    self._pending = 0.0
            return
        stale = last_read is None or now - last_read >= REREAD_AFTER
        if pending:
            if last_put is not None and now - last_put < MIN_GAP:
                return
            if stale:
                self._read(now)
                if not self.usable():
                    return
            self._send(now)
        elif last_read is None or now - last_read >= POLL:
            self._read(now)
