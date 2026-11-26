"""Settings file: load, check, fill in defaults."""
import json
import os

APP_FOLDER = "8BitDo Volume Knob"
SETTINGS_NAME = "settings.json"
LOG_NAME = "volume_knob.log"

DEFAULTS = {
    "step": 0.015,            # one knob click = 1.5 % (0-100 % in about 2 turns)
    "apps": ["spotify.exe"],  # Windows apps the knob controls (exe names)
    "peak_threshold": 0.001,  # sound level above this counts as "playing" (~ -60 dBFS)
    "hold_seconds": 1.5,      # an app stays "playing" this long after its last sound
    "brave_fallback": True,   # no extension connected: change Brave's whole sound instead
    "boost_max": 1.5,         # YouTube / YT Music may go up to 150 % (1.0 = no boost)
    "failsafe": True,         # tab over 100 % at start / when it opens alone -> failsafe_level
    "failsafe_level": 0.5,
    "failsafe_start_window": 20.0,  # seconds after start in which every tab is checked
    "log_level": "INFO",
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def data_dir():
    """Folder for settings.json (VOLUME_KNOB_DATA overrides it, for tests)."""
    override = os.environ.get("VOLUME_KNOB_DATA")
    if override:
        return override
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_FOLDER)


def log_dir():
    override = os.environ.get("VOLUME_KNOB_DATA")
    if override:
        return override
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_FOLDER)


def _number(value, default, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default, False
    if value != value:  # NaN
        return default, False
    return float(min(hi, max(lo, value))), lo <= value <= hi


def validate(raw):
    """Return (settings dict, list of warnings). Never raises."""
    warnings = []
    if not isinstance(raw, dict):
        return dict(DEFAULTS), ["settings file is not a JSON object; using defaults"]
    out = dict(DEFAULTS)
    limits = {"step": (0.01, 0.20), "peak_threshold": (0.00001, 0.5), "hold_seconds": (0.2, 10.0),
              "boost_max": (1.0, 1.5), "failsafe_level": (0.05, 1.0),
              "failsafe_start_window": (5.0, 120.0)}
    for key, (lo, hi) in limits.items():
        if key in raw:
            out[key], ok = _number(raw[key], DEFAULTS[key], lo, hi)
            if not ok:
                warnings.append(f"{key}: bad or out-of-range value, using {out[key]}")
    if "apps" in raw:
        apps = raw["apps"]
        if isinstance(apps, list) and all(isinstance(a, str) and a.strip() for a in apps):
            out["apps"] = sorted({a.strip().lower() for a in apps})
        else:
            warnings.append("apps: must be a list of exe names, using default")
    for key in ("brave_fallback", "failsafe"):
        if key in raw:
            if isinstance(raw[key], bool):
                out[key] = raw[key]
            else:
                warnings.append(f"{key}: must be true or false, using default")
    if "log_level" in raw:
        level = str(raw["log_level"]).upper()
        if level in LOG_LEVELS:
            out["log_level"] = level
        else:
            warnings.append("log_level: unknown, using INFO")
    return out, warnings


def load(folder=None):
    """Load settings from folder (default data_dir()). Writes a default file if missing."""
    folder = folder or data_dir()
    path = os.path.join(folder, SETTINGS_NAME)
    if not os.path.exists(path):
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2)
        except OSError as e:
            return dict(DEFAULTS), [f"could not write default settings: {e.strerror}"]
        return dict(DEFAULTS), []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS), ["settings file could not be read; using defaults"]
    return validate(raw)
