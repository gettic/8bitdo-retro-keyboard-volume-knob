"""Pipe protocol v2 (see PROTOCOL.md): parse and build messages."""
import json
import os

from version import VERSION

PROTOCOL = 2
FEATURES = ("boost", "set_volume")
DEFAULT_PIPE = r"\\.\pipe\8bitdo-volume-knob"
MAX_LINE = 64 * 1024
SITES = ("youtube", "ytmusic")
BOOST_STATES = ("off", "on", "blocked", "unavailable", "error")
HARD_MAX = 1.5


def pipe_name():
    return os.environ.get("VOLUME_KNOB_PIPE") or DEFAULT_PIPE


def decode(line):
    """bytes (without newline) -> dict. Raises ValueError for bad lines."""
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    msg = json.loads(line.decode("utf-8"))
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError("not a message object")
    return msg


def encode(msg):
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def hello():
    return {"type": "hello", "protocol": PROTOCOL, "version": VERSION, "features": list(FEATURES)}


def parse_hello(msg):
    """True if the host speaks our protocol version."""
    p = msg.get("protocol")
    return isinstance(p, int) and not isinstance(p, bool) and p == PROTOCOL


def parse_features(msg):
    f = msg.get("features")
    if not isinstance(f, list):
        return frozenset()
    return frozenset(x for x in f if isinstance(x, str))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _clamp(v, hi=HARD_MAX):
    return float(min(hi, max(0.0, v)))


def parse_state(msg):
    """state message -> list of tab dicts. Bad tabs are skipped; a bad
    `tabs` field gives None (message ignored)."""
    tabs = msg.get("tabs")
    if not isinstance(tabs, list):
        return None
    out = []
    for t in tabs:
        if not isinstance(t, dict):
            continue
        tab = t.get("tab")
        if isinstance(tab, bool) or not isinstance(tab, int):
            continue
        site = t.get("site")
        if site not in SITES:
            continue
        vol = t.get("volume")
        mx = t.get("max_volume")
        boost = t.get("boost")
        out.append({
            "tab": tab,
            "site": site,
            "playing": t.get("playing") is True,
            "volume": _clamp(vol) if _num(vol) else None,
            "muted": t.get("muted") is True,
            "max_volume": _clamp(mx) if _num(mx) else 1.0,
            "boost": boost if boost in BOOST_STATES else "off",
        })
    return out


def _target(tabs, site):
    if tabs is not None:
        return {"tabs": list(tabs)}
    return {"site": site}


def volume(delta, tabs=None, site="all", max_volume=1.0):
    msg = {"type": "volume", "delta": round(float(delta), 4),
           "max": round(_clamp(max_volume), 4)}
    msg.update(_target(tabs, site))
    return msg


def set_volume(value, tabs=None, site="all"):
    msg = {"type": "set_volume", "volume": round(_clamp(value), 4)}
    msg.update(_target(tabs, site))
    return msg


def mute(muted, tabs=None, site="all"):
    msg = {"type": "mute", "muted": bool(muted)}
    msg.update(_target(tabs, site))
    return msg
