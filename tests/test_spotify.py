"""Spotify Web API path against a fake Spotify server on a random loopback port.
No real network calls, no real login, no real settings (temp folders only)."""
import http.server
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

from knob import config, controller, spotify_api as api, spotify_login
from knob.controller import Controller
from tests.fakes import FakeAudio, FakeClock, FakeSession, ProcessList, make_bridge, settings

PC = "test-pc"
CID = "0123456789abcdef0123456789abcdef"
REFRESH = "refresh-SECRET-1"
ACCESS = "access-SECRET-1"


def wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeSpotify:
    """Accounts + Web API on one random port."""

    def __init__(self):
        self.lock = threading.Lock()
        self.volume = 50
        self.device = {"id": "dev-pc", "name": "Test-PC", "type": "Computer", "is_active": True,
                       "supports_volume": True}
        self.devices = [dict(self.device), {"id": "dev-phone", "name": "Phone",
                                            "type": "Smartphone"}]
        self.player_status = 200
        self.put_status = 200
        self.token_status = 200
        self.retry_after = None
        self.unauthorized_once = False
        self.delay = 0.0
        self.access = ACCESS
        self.new_refresh = None
        self.puts = []
        self.gets = 0
        self.token_calls = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data=None, headers=None):
                body = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _auth(self):
                with fake.lock:
                    if fake.unauthorized_once:
                        fake.unauthorized_once = False
                        return False
                    return self.headers.get("Authorization") == "Bearer " + fake.access

            def _api_status(self, status):
                headers = {}
                if status == 429 and fake.retry_after:
                    headers["Retry-After"] = str(fake.retry_after)
                self._send(status, {"error": {"status": status}}, headers)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                with fake.lock:
                    fake.token_calls.append({k: v[0] for k, v in form.items()})
                if self.path != "/api/token" or fake.token_status != 200:
                    return self._send(fake.token_status if self.path == "/api/token" else 404,
                                      {"error": "invalid_grant"})
                out = {"access_token": fake.access, "token_type": "Bearer", "expires_in": 3600}
                if form.get("grant_type") == ["authorization_code"]:
                    out["refresh_token"] = REFRESH
                elif fake.new_refresh:
                    out["refresh_token"] = fake.new_refresh
                self._send(200, out)

            def do_GET(self):
                time.sleep(fake.delay)
                if not self._auth():
                    return self._send(401, {"error": {"status": 401}})
                if self.path == "/v1/me/player":
                    with fake.lock:
                        fake.gets += 1
                        st = fake.player_status
                        dev = dict(fake.device, volume_percent=fake.volume)
                    if st == 204:
                        return self._send(204)
                    if st != 200:
                        return self._api_status(st)
                    return self._send(200, {"device": dev, "is_playing": True,
                                            "item": {"name": "SECRET-TRACK"}})
                if self.path == "/v1/me/player/devices":
                    return self._send(200, {"devices": fake.devices})
                self._send(404)

            def do_PUT(self):
                time.sleep(fake.delay)
                if not self._auth():
                    return self._send(401, {"error": {"status": 401}})
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path != "/v1/me/player/volume":
                    return self._send(404)
                with fake.lock:
                    st = fake.put_status
                    if st == 200:
                        fake.puts.append((int(q["volume_percent"]), q.get("device_id")))
                        fake.volume = int(q["volume_percent"])
                if st != 200:
                    return self._api_status(st)
                self._send(204)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.srv.handle_error = lambda request, address: None  # timeout tests hang up on purpose
        self.port = self.srv.server_address[1]
        self.api = f"http://127.0.0.1:{self.port}/v1"
        self.accounts = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="knob_spotify_test_")
        self.path = os.path.join(self.tmp.name, api.STORE_NAME)
        self.fake = FakeSpotify()
        self.logs = []
        self.handler = _ListHandler(self.logs)
        lg = logging.getLogger("knob.spotify")
        lg.addHandler(self.handler)
        self.old_level = lg.level
        lg.setLevel(logging.DEBUG)

    def tearDown(self):
        lg = logging.getLogger("knob.spotify")
        lg.removeHandler(self.handler)
        lg.setLevel(self.old_level)
        self.fake.close()
        self.tmp.cleanup()
        joined = "\n".join(self.logs)
        for secret in (REFRESH, ACCESS, "SECRET-TRACK", "refresh-new"):
            self.assertNotIn(secret, joined)  # no token or track ever logged

    def login_file(self, device_id="dev-pc"):
        api.save_store(self.path, CID, REFRESH, device_id, "Test-PC" if device_id else None)


class _ListHandler(logging.Handler):
    def __init__(self, out):
        super().__init__(logging.DEBUG)
        self.out = out

    def emit(self, record):
        self.out.append(record.getMessage())


class StoreTests(Base):
    def test_dpapi_round_trip(self):
        blob = api.protect(b"hello token")
        self.assertNotIn(b"hello token", blob)
        self.assertEqual(api.unprotect(blob), b"hello token")

    def test_store_round_trip_token_not_plain(self):
        self.login_file()
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(REFRESH, text)
        self.assertIn(CID, text)
        creds = api.load_store(self.path)
        self.assertEqual((creds["client_id"], creds["refresh_token"], creds["device_id"]),
                         (CID, REFRESH, "dev-pc"))

    def test_bad_store(self):
        self.assertIsNone(api.load_store(self.path))
        with open(self.path, "w") as f:
            f.write('{"client_id": "x", "refresh_token_dpapi": "AAAA"}')
        self.assertIsNone(api.load_store(self.path))

    def test_logout(self):
        self.login_file()
        out = []
        self.assertEqual(spotify_login.logout(out.append, self.path), 0)
        self.assertFalse(os.path.exists(self.path))
        spotify_login.logout(out.append, self.path)
        self.assertIn("No Spotify login", out[-1])


class ClientTests(Base):
    def client(self):
        self.login_file()
        return api.Client(api.load_store(self.path), self.path, self.fake.api, self.fake.accounts)

    def test_refresh_and_player(self):
        c = self.client()
        p = c.player()
        self.assertEqual(p["device"]["volume_percent"], 50)
        self.assertEqual(self.fake.token_calls[0]["grant_type"], "refresh_token")
        self.assertEqual(self.fake.token_calls[0]["client_id"], CID)
        self.assertNotIn("client_secret", self.fake.token_calls[0])

    def test_401_refreshes_and_retries(self):
        c = self.client()
        c.player()
        self.fake.unauthorized_once = True
        self.assertIsNotNone(c.player())
        self.assertEqual(len(self.fake.token_calls), 2)

    def test_rotated_refresh_token_saved(self):
        self.fake.new_refresh = "refresh-new"
        c = self.client()
        c.player()
        self.assertEqual(api.load_store(self.path)["refresh_token"], "refresh-new")

    def test_errors(self):
        c = self.client()
        for status, part in ((403, "Premium"), (404, "no active device"), (429, "slow down"),
                             (500, "HTTP 500")):
            self.fake.player_status = status
            with self.assertRaises(api.ApiError) as e:
                c.player()
            self.assertIn(part, e.exception.reason)
        self.fake.player_status = 204
        self.assertIsNone(c.player())

    def test_retry_after(self):
        c = self.client()
        self.fake.player_status, self.fake.retry_after = 429, 42
        with self.assertRaises(api.ApiError) as e:
            c.player()
        self.assertEqual(e.exception.wait, 42)

    def test_refresh_refused_needs_relogin(self):
        self.fake.token_status = 400
        with self.assertRaises(api.ApiError) as e:
            self.client().player()
        self.assertTrue(e.exception.relogin)

    def test_offline_and_timeout(self):
        self.login_file()
        dead = f"http://127.0.0.1:{free_port()}"
        c = api.Client(api.load_store(self.path), self.path, dead + "/v1", dead)
        with self.assertRaises(api.ApiError) as e:
            c.player()
        self.assertIn("offline", e.exception.reason)
        self.fake.delay = 1.0
        c = api.Client(api.load_store(self.path), self.path, self.fake.api, self.fake.accounts,
                       timeout=0.3)
        with self.assertRaises(api.ApiError) as e:
            c.player()
        self.assertIn("offline", e.exception.reason)

    def test_stalled_lookup_bounded(self):
        class SlowOpener:  # like a name lookup that hangs past urllib's timeout
            def open(self, req, timeout=None):
                time.sleep(3)
                raise OSError("late")
        t = time.monotonic()
        with self.assertRaises(OSError):
            api.http_call(SlowOpener(), "GET", "https://example.invalid/", timeout=0.2)
        self.assertLess(time.monotonic() - t, 1.5)  # the worker is free again quickly

    def test_device_match(self):
        creds = {"device_id": None}
        self.assertTrue(api.is_this_pc({"type": "Computer", "name": " Test-PC "}, creds, PC))
        self.assertFalse(api.is_this_pc({"type": "Smartphone", "name": "test-pc"}, creds, PC))
        self.assertFalse(api.is_this_pc({"type": "Computer", "name": "Other"}, creds, PC))
        self.assertTrue(api.is_this_pc({"id": "x", "name": "Other"}, {"device_id": "x"}, PC))
        devs = [{"id": "a", "type": "Computer", "name": "Laptop"},
                {"id": "b", "type": "Smartphone", "name": "Phone"}]
        self.assertEqual(api.pick_this_pc(devs, creds, PC)["id"], "a")  # only computer
        devs.append({"id": "c", "type": "Computer", "name": "TEST-PC"})
        self.assertEqual(api.pick_this_pc(devs, creds, PC)["id"], "c")
        devs.append({"id": "d", "type": "Computer", "name": "Other"})
        devs.pop(2)
        self.assertIsNone(api.pick_this_pc(devs, creds, PC))  # two unknown computers


class VolumeTests(Base):
    def make(self, login=True, **kw):
        if login:
            self.login_file(kw.pop("device_id", "dev-pc"))
        self.changes = []
        v = api.SpotifyVolume(self.path, self.fake.api, self.fake.accounts, pc_name=PC,
                              on_change=lambda: self.changes.append(1), tick=0.01, **kw)
        self.addCleanup(v.close)
        v.start()
        return v

    def test_not_logged_in_no_network(self):
        v = self.make(login=False)
        v.set_present(True)
        time.sleep(0.3)
        self.assertFalse(v.usable())
        self.assertEqual((self.fake.gets, self.fake.token_calls), (0, []))
        self.assertEqual(sum("not logged in" in m for m in self.logs), 1)

    def test_not_present_no_calls(self):
        v = self.make()
        time.sleep(0.3)
        self.assertFalse(v.usable())
        self.assertEqual(self.fake.gets, 0)

    def test_usable_reads_volume(self):
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        self.assertAlmostEqual(v.level(), 0.5)
        self.assertTrue(self.changes)
        self.assertEqual(sum("own volume bar" in m for m in self.logs), 1)

    def test_clicks_bundled_rounded_and_device_id(self):
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        for _ in range(10):
            v.step(0.013)
            time.sleep(0.02)
        self.assertAlmostEqual(v.level(), 0.63)
        self.assertTrue(wait_for(lambda: self.fake.puts and self.fake.puts[-1][0] == 63))
        time.sleep(0.4)
        self.assertLessEqual(len(self.fake.puts), 3)          # 10 clicks, few calls
        self.assertTrue(all(d == "dev-pc" for _, d in self.fake.puts))
        self.assertEqual(self.fake.volume, 63)

    def test_fractional_steps_accumulate(self):
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        sent = []
        for _ in range(3):
            v.step(0.013)
            self.assertTrue(wait_for(lambda: len(self.fake.puts) > len(sent)))
            sent.append(self.fake.puts[-1][0])
            time.sleep(0.35)
        self.assertEqual(sent, [51, 53, 54])  # 51.3, 52.6, 53.9 -> not 51, 52, 53

    def test_ends_reached_exactly(self):
        self.fake.volume = 99
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        v.step(0.013)
        v.step(0.013)
        self.assertTrue(wait_for(lambda: self.fake.volume == 100))
        self.assertEqual(v.level(), 1.0)
        v.step(0.013)  # already at the top: stays (no boost for Spotify)
        time.sleep(0.4)
        self.assertEqual(self.fake.volume, 100)
        self.assertEqual([p for p, _ in self.fake.puts], [100])

    def test_moved_in_spotify_is_picked_up(self):
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        self.fake.volume = 20
        time.sleep(api.REREAD_AFTER + 0.2)
        v.step(0.013)
        self.assertTrue(wait_for(lambda: self.fake.puts and self.fake.puts[-1][0] == 21))

    def test_other_device_falls_back(self):
        self.fake.device = {"id": "dev-phone", "name": "Phone", "type": "Smartphone",
                            "supports_volume": True}
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(lambda: any("another device" in m for m in self.logs)))
        self.assertFalse(v.usable())
        self.assertEqual(self.fake.puts, [])

    def test_name_match_learns_device_id(self):
        v = self.make(device_id=None)
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        v.step(0.013)
        self.assertTrue(wait_for(lambda: self.fake.puts))
        self.assertEqual(self.fake.puts[0][1], "dev-pc")

    def test_fallbacks_log_once(self):
        for status, part in ((403, "Premium"), (404, "no active device"), (429, "slow down"),
                             (204, "no active device")):
            with self.subTest(status):
                self.logs.clear()
                self.fake.player_status = status
                self.fake.retry_after = 1 if status == 429 else None
                v = self.make()
                v.set_present(True)
                self.assertTrue(wait_for(lambda: any(part in m for m in self.logs)))
                time.sleep(0.3)
                self.assertFalse(v.usable())
                self.assertEqual(sum("Windows mixer" in m for m in self.logs), 1)
                self.assertEqual(self.fake.puts, [])
                v.close()

    def test_put_failure_falls_back(self):
        v = self.make()
        v.set_present(True)
        self.assertTrue(wait_for(v.usable))
        self.fake.put_status = 403
        v.step(0.013)
        self.assertTrue(wait_for(lambda: not v.usable()))
        self.assertTrue(any("Premium" in m for m in self.logs))

    def test_offline_falls_back(self):
        self.login_file()
        dead = f"http://127.0.0.1:{free_port()}"
        v = api.SpotifyVolume(self.path, dead + "/v1", dead, pc_name=PC, tick=0.01)
        self.addCleanup(v.close)
        v.start()
        v.set_present(True)
        self.assertTrue(wait_for(lambda: any("offline" in m for m in self.logs)))
        self.assertFalse(v.usable())

    def test_new_login_picked_up_without_restart(self):
        v = self.make(login=False)
        v.set_present(True)
        time.sleep(0.2)
        self.assertFalse(v.usable())
        self.login_file()
        self.assertTrue(wait_for(v.usable, api.RELOAD_CHECK + 2))
        api.delete_store(self.path)
        self.assertTrue(wait_for(lambda: not v.usable(), api.RELOAD_CHECK + 2))

    def test_close_is_quick(self):
        v = self.make()
        t = time.monotonic()
        v.close()
        self.assertLess(time.monotonic() - t, 1.0)


class LoginTests(Base):
    def test_pkce_rfc7636_vector(self):
        self.assertEqual(spotify_login.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        v = spotify_login.make_verifier()
        self.assertTrue(43 <= len(v) <= 128)

    def test_callback_rejects_wrong_state_and_path_then_takes_right_one(self):
        port = free_port()
        result = {}

        def serve():
            result["r"] = spotify_login.wait_for_code(port, "good-state", timeout=5)
        t = threading.Thread(target=serve)
        t.start()
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path):
            try:
                return opener.open(base + path, timeout=2).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertTrue(wait_for(lambda: _port_open(port)))
        self.assertEqual(get("/other?state=good-state&code=x"), 404)
        self.assertEqual(get("/callback?state=bad&code=evil"), 400)
        self.assertEqual(get("/callback?state=%C3%A9%E2%82%AC&code=evil"), 400)  # non-ASCII
        self.assertEqual(get("/callback?state=good-state&code=the-code"), 200)
        t.join(5)
        self.assertEqual(result["r"], ("the-code", None))
        self.assertFalse(_port_open(port))  # server gone: no second answer

    def test_callback_error_and_timeout(self):
        port = free_port()
        result = {}
        t = threading.Thread(target=lambda: result.update(
            r=spotify_login.wait_for_code(port, "s", timeout=5)))
        t.start()
        self.assertTrue(wait_for(lambda: _port_open(port)))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(f"http://127.0.0.1:{port}/callback?state=s&error=access_denied", timeout=2)
        t.join(5)
        self.assertIsNone(result["r"][0])
        self.assertIn("cancelled", result["r"][1])
        t0 = time.monotonic()
        code, why = spotify_login.wait_for_code(free_port(), "s", timeout=0.5)
        self.assertIsNone(code)
        self.assertIn("2 minutes", why)
        self.assertLess(time.monotonic() - t0, 2)

    def test_port_taken(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            code, why = spotify_login.wait_for_code(s.getsockname()[1], "s", timeout=1)
        finally:
            s.close()
        self.assertIsNone(code)
        self.assertIn("in use", why)

    def run_login(self, client_id=CID, answer=True):
        port = free_port()
        out, opened = [], []

        def browser(url):
            opened.append(url)
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

            def answer_it():
                if not answer:
                    return
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                opener.open(f"{q['redirect_uri']}?state={q['state']}&code=auth-code", timeout=2)
            threading.Thread(target=answer_it, daemon=True).start()
            return True
        rc = spotify_login.login(out.append, lambda prompt: client_id, browser, self.path,
                                 self.fake.api, self.fake.accounts, port, timeout=3, pc_name=PC)
        return rc, out, opened, port

    def test_full_login(self):
        rc, out, opened, port = self.run_login()
        self.assertEqual(rc, 0, out)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
        self.assertEqual((q["client_id"], q["response_type"], q["code_challenge_method"]),
                         (CID, "code", "S256"))
        self.assertEqual(q["redirect_uri"], f"http://127.0.0.1:{port}/callback")
        self.assertEqual(q["scope"], api.SCOPES)
        ex = self.fake.token_calls[0]
        self.assertEqual((ex["grant_type"], ex["code"], ex["redirect_uri"]),
                         ("authorization_code", "auth-code", q["redirect_uri"]))
        self.assertEqual(spotify_login.challenge_for(ex["code_verifier"]), q["code_challenge"])
        creds = api.load_store(self.path)
        self.assertEqual((creds["refresh_token"], creds["device_id"]), (REFRESH, "dev-pc"))
        self.assertTrue(any("Test-PC" in line for line in out))
        self.assertFalse(any(REFRESH in line or ACCESS in line for line in out))

    def test_bad_client_id(self):
        rc, out, opened, _ = self.run_login(client_id="not an id")
        self.assertEqual((rc, opened), (1, []))
        self.assertFalse(os.path.exists(self.path))

    def test_refused_exchange(self):
        self.fake.token_status = 400
        rc, out, _, _ = self.run_login()
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_no_answer(self):
        rc, out, _, _ = self.run_login(answer=False)
        self.assertEqual(rc, 1)
        self.assertIn("2 minutes", out[-1])

    def test_default_redirect_matches_dashboard_step(self):
        self.assertEqual(api.REDIRECT_URI, "http://127.0.0.1:38480/callback")


def _port_open(port):
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class FakeSpotifyVolume:
    def __init__(self, usable=True, level=0.4):
        self._usable, self._level = usable, level
        self.steps, self.present = [], None
        self.on_change = None

    def usable(self):
        return self._usable

    def level(self):
        return self._level if self._usable else None

    def step(self, delta):
        self.steps.append(delta)
        self._level = min(1.0, max(0.0, self._level + delta))
        return self._level

    def set_present(self, present):
        self.present = present


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge, self.rec = make_bridge(self.clock)
        self.spotify = FakeSession("spotify.exe", volume=0.7, peak=0.3)
        self.audio = FakeAudio([self.spotify])
        self.web = FakeSpotifyVolume()
        self.shown = []
        self.ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                              process_lister=ProcessList(), spotify=self.web,
                              display=lambda rows, popup: self.shown.append(rows))
        self.ctl.audio = self.audio
        self.ctl.poll()

    def test_presence_reported(self):
        self.assertTrue(self.web.present)
        self.audio.all = []
        self.ctl.refresh(force=True)
        self.assertFalse(self.web.present)

    def test_turn_moves_spotify_bar_not_mixer(self):
        self.spotify._muted = True
        self.ctl.handle("up")
        self.assertEqual(self.web.steps, [settings()["step"]])
        self.assertEqual(self.spotify._volume, 0.7)       # mixer untouched
        self.assertFalse(self.spotify._muted)             # a turn still unmutes
        self.assertAlmostEqual(self.shown[-1][0].level, 0.4 + settings()["step"])

    def test_mute_stays_mixer(self):
        self.ctl.handle("mute")
        self.assertTrue(self.spotify._muted)
        self.assertEqual(self.web.steps, [])
        self.assertTrue(self.shown[-1][0].muted)

    def test_not_usable_uses_mixer(self):
        self.web._usable = False
        self.ctl.handle("up")
        self.assertEqual(self.web.steps, [])
        self.assertAlmostEqual(self.spotify._volume, 0.7 + settings()["step"])
        self.assertAlmostEqual(self.shown[-1][0].level, 0.7 + settings()["step"])

    def test_change_event_updates_display(self):
        self.ctl.update_display()
        n = len(self.shown)
        self.web._level = 0.9
        self.web.on_change()
        self.ctl.on_event(self.ctl.keys.get_nowait())
        self.assertEqual(len(self.shown), n + 1)
        self.assertAlmostEqual(self.shown[-1][0].level, 0.9)

    def test_without_spotify_object_unchanged(self):
        ctl = Controller(settings(), lambda: self.audio, self.bridge, clock=self.clock,
                         process_lister=ProcessList())
        ctl.audio = self.audio
        ctl.poll()
        ctl.handle("down")
        self.assertAlmostEqual(self.spotify._volume, 0.7 - settings()["step"])


class ConfigTests(unittest.TestCase):
    def test_spotify_control(self):
        self.assertEqual(config.DEFAULTS["spotify_control"], "web")
        s, w = config.validate({"spotify_control": " Mixer "})
        self.assertEqual((s["spotify_control"], w), ("mixer", []))
        s, w = config.validate({"spotify_control": "api"})
        self.assertEqual(s["spotify_control"], "web")
        self.assertTrue(w)


class SelfTestTests(Base):
    def report(self):
        from knob import selftest
        rep = selftest._Report(None)
        selftest._check_spotify(rep, self.path, self.fake.api, self.fake.accounts)
        return rep

    def test_not_logged_in_is_ok_and_offline(self):
        rep = self.report()
        self.assertEqual(rep.failed, 0)
        self.assertIn("not logged in", rep.lines[0])
        self.assertEqual((self.fake.gets, self.fake.token_calls), (0, []))

    def test_logged_in_reads_only(self):
        self.login_file()
        rep = self.report()
        self.assertEqual(rep.failed, 0, rep.lines)
        self.assertIn("on this PC=True", rep.lines[0])
        self.assertNotIn("SECRET-TRACK", rep.lines[0])
        self.assertEqual(self.fake.puts, [])


if __name__ == "__main__":
    unittest.main()
