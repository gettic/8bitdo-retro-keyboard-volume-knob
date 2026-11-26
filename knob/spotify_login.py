"""One-time Spotify login (--spotify-login) with PKCE: no client secret.

Asks for the Client ID, opens Spotify's consent page in the default browser,
waits up to 2 minutes for one redirect to http://127.0.0.1:38480/callback
(loopback only, exclusive port, `state` checked), exchanges the code, finds
this PC's Spotify device and saves the login (refresh token DPAPI-encrypted).
Nothing secret is printed or logged."""
import base64
import hashlib
import html
import http.server
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser

from . import spotify_api as api

CLIENT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
LOGIN_SECONDS = 120


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_verifier():
    return b64url(os.urandom(64))  # 86 characters, within 43-128


def challenge_for(verifier):
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(accounts, client_id, redirect_uri, state, challenge):
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
         "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
         "scope": api.SCOPES}
    return accounts.rstrip("/") + "/authorize?" + urllib.parse.urlencode(q)


PAGE = ("<!doctype html><meta charset=utf-8><title>8BitDo Volume Knob</title>"
        "<body style='font-family:sans-serif;margin:3em'><h2>{}</h2><p>{}</p></body>")


class _Server(http.server.HTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def wait_for_code(port, state, timeout=LOGIN_SECONDS, ready=None):
    """Serve http://127.0.0.1:<port>/callback until one request with the right
    state arrives. -> (code, None) or (None, reason)."""
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5  # a silent connection can't hold the login up

        def log_message(self, *args):  # never print the URL (it holds the code)
            pass

        def _page(self, status, title, text):
            body = PAGE.format(html.escape(title), html.escape(text)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/callback" or done.is_set():
                self._page(404, "Not found", "")
                return
            q = urllib.parse.parse_qs(parts.query)
            got_state = (q.get("state") or [""])[0]
            if not secrets.compare_digest(got_state.encode("utf-8", "replace"),
                                          state.encode("utf-8")):  # any text is just "wrong"
                self._page(400, "Login not accepted",
                           "This answer does not belong to the login that was started.")
                return  # keep waiting for the real one
            if q.get("error"):
                result["error"] = "Spotify login was cancelled or refused"
                self._page(200, "Spotify login cancelled", "You can close this tab.")
            elif q.get("code"):
                result["code"] = q["code"][0]
                self._page(200, "Spotify connected",
                           "You can close this tab and go back to the console.")
            else:
                result["error"] = "Spotify sent no login code"
                self._page(400, "Login failed", "No login code was received.")
            done.set()

    try:
        srv = _Server(("127.0.0.1", port), Handler)
    except OSError:
        return None, f"port {port} is in use; close the program using it and try again"
    srv.timeout = 0.5
    if ready:
        ready()
    deadline = time.monotonic() + timeout
    try:
        while not done.is_set() and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    if "code" in result:
        return result["code"], None
    return None, result.get("error", "no answer from the browser within 2 minutes")


def exchange(accounts, client_id, code, verifier, redirect_uri, timeout=10.0):
    """-> refresh token, or raises api.ApiError."""
    opener = api._opener(accounts)
    try:
        r = api.token_request(opener, accounts.rstrip("/"),
                              {"grant_type": "authorization_code", "code": code,
                               "redirect_uri": redirect_uri, "client_id": client_id,
                               "code_verifier": verifier}, timeout)
    except OSError:
        raise api.ApiError("Spotify could not be reached")
    tok = r.data.get("refresh_token") if isinstance(r.data, dict) else None
    if r.status != 200 or not isinstance(tok, str) or not tok:
        raise api.ApiError(f"Spotify did not accept the login (HTTP {r.status})")
    return tok


def login(out=print, ask=input, open_browser=webbrowser.open, path=None,
          api_base=api.API, accounts=api.ACCOUNTS, port=api.REDIRECT_PORT,
          timeout=LOGIN_SECONDS, pc_name=None):
    """The whole --spotify-login. Returns a process exit code."""
    path = path or api.store_path()
    redirect = f"http://127.0.0.1:{port}/callback"
    out("Spotify login for the 8BitDo volume knob (needs Spotify Premium).")
    out("Keep Spotify open on this PC while you log in.")
    try:
        cid = (ask("Paste your Spotify Client ID: ") or "").strip()
    except (EOFError, KeyboardInterrupt):
        out("Cancelled.")
        return 1
    if not CLIENT_ID_RE.match(cid):
        out("That is not a Client ID (32 letters and digits from the app's Settings page).")
        return 1
    verifier, state = make_verifier(), secrets.token_urlsafe(24)
    url = authorize_url(accounts, cid, redirect, state, challenge_for(verifier))
    out("Your browser opens Spotify's page: click Agree.")
    code, why = wait_for_code(port, state, timeout, ready=lambda: open_browser(url))
    if code is None:
        out(f"Login not finished: {why}.")
        return 1
    try:
        refresh = exchange(accounts, cid, code, verifier, redirect)
        creds = {"client_id": cid, "refresh_token": refresh, "device_id": None,
                 "device_name": None}
        client = api.Client(creds, path, api_base, accounts, timeout=10.0)
        client.refresh()
        device = api.pick_this_pc(client.devices(), creds, pc_name)
    except api.ApiError as e:
        out(f"Login not finished: {e.reason}.")
        return 1
    if device is not None:
        creds["device_id"], creds["device_name"] = device.get("id"), device.get("name")
    try:
        api.save_store(path, cid, creds["refresh_token"], creds["device_id"],
                       creds["device_name"])
    except OSError as e:
        out(f"Could not save the login: {e.strerror or e}.")
        return 1
    if device is not None:
        out(f"Spotify connected. This PC's Spotify device: {creds['device_name']}.")
    else:
        out("Spotify connected, but this PC's Spotify app was not found. If the knob keeps "
            "using the Windows mixer, open Spotify on this PC and run --spotify-login again.")
    out("The running knob uses it within a few seconds. You can close this window.")
    return 0


def logout(out=print, path=None):
    path = path or api.store_path()
    if api.delete_store(path):
        out("Spotify login removed; the knob uses the Windows mixer for Spotify again.")
    else:
        out("No Spotify login was saved.")
    return 0
