"""Settings file: load, check, fill in defaults."""
import json
import os

APP_FOLDER = "8BitDo Volume Knob"
SETTINGS_NAME = "settings.json"
LOG_NAME = "volume_knob.log"

DEFAULTS = {
    "step": 0.013,            # one knob click = 1.3 % (0-100 % in about 2 full turns, 38 clicks per turn)
    "apps": ["spotify.exe"],  # Windows apps the knob controls (exe names)
    "peak_threshold": 0.001,  # sound level above this counts as "playing" (~ -60 dBFS)
    "hold_seconds": 1.5,      # an app stays "playing" this long after its last sound
    "brave_fallback": True,   # no extension connected: change Brave's whole sound instead
    "boost_max": 1.5,         # YouTube / YT Music may go up to 150 % (1.0 = no boost)
    "failsafe": True,         # tab over 100 % at start / when it opens alone -> failsafe_level
    "failsafe_level": 0.5,
    "failsafe_start_window": 20.0,  # seconds after start in which every tab is checked
    "osd": True,              # volume pop-up on knob turns
    "osd_seconds": 1.5,       # how long the pop-up stays after the last turn
    "osd_monitor": "primary",  # pop-up on the main screen, or "mouse": the screen with the mouse
    "tray_icon": True,        # knob icon in the notification area (shows the volume)
    "tray_style": "knob_number",  # knob with the % on a strip along the bottom, or "knob"
    "spotify_control": "web",  # "web": Spotify's own bar (after --spotify-login), "mixer": Windows mixer
    "log_level": "INFO",
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
OSD_MONITORS = ("primary", "mouse")
TRAY_STYLES = ("knob_number", "knob")
SPOTIFY_CONTROLS = ("web", "mixer")


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
              "failsafe_start_window": (5.0, 120.0), "osd_seconds": (0.5, 10.0)}
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
    for key in ("brave_fallback", "failsafe", "osd", "tray_icon"):
        if key in raw:
            if isinstance(raw[key], bool):
                out[key] = raw[key]
            else:
                warnings.append(f"{key}: must be true or false, using default")
    if "osd_monitor" in raw:
        mode = str(raw["osd_monitor"]).strip().lower()
        if mode in OSD_MONITORS:
            out["osd_monitor"] = mode
        else:
            warnings.append('osd_monitor: must be "primary" or "mouse", using primary')
    if "tray_style" in raw:
        style = str(raw["tray_style"]).strip().lower()
        if style in TRAY_STYLES:
            out["tray_style"] = style
        else:
            warnings.append('tray_style: must be "knob_number" or "knob", using knob_number')
    if "spotify_control" in raw:
        mode = str(raw["spotify_control"]).strip().lower()
        if mode in SPOTIFY_CONTROLS:
            out["spotify_control"] = mode
        else:
            warnings.append('spotify_control: must be "web" or "mixer", using web')
    if "log_level" in raw:
        level = str(raw["log_level"]).upper()
        if level in LOG_LEVELS:
            out["log_level"] = level
        else:
            warnings.append("log_level: unknown, using INFO")
    return out, warnings


BROKEN_NAME = "settings.broken.json"


class SettingsFileError(Exception):
    """The settings file exists but can't be read as settings (hand-edit
    mistake): the settings window never overwrites it."""


def _path(folder):
    return os.path.join(folder or data_dir(), SETTINGS_NAME)


def _read_raw(path):
    """The file's JSON object, {} when it doesn't exist. Raises SettingsFileError."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise SettingsFileError(f"settings file could not be read ({e.__class__.__name__})")
    if not isinstance(raw, dict):
        raise SettingsFileError("settings file is not a JSON object")
    return raw


def _write_atomic(path, data):
    """Write the whole file next to it first, then swap it in (never half-written)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read(folder=None):
    """Settings as they are in the file now (validated), for the settings
    window. Raises SettingsFileError for a broken file; a missing file = defaults."""
    raw = _read_raw(_path(folder))
    return validate(raw)[0]


def save_changes(changes, folder=None):
    """Write only `changes` ({key: value}) into the settings file, keeping every
    other key exactly as it is (hand edits, unknown keys). Each value must be
    valid as given (ValueError otherwise). Raises SettingsFileError (nothing
    written) when the file is broken, OSError when it can't be written.
    Returns the validated changes."""
    path = _path(folder)
    raw = _read_raw(path)
    for key in changes:
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}")
    checked, warnings = validate(dict(changes))
    bad = [w for w in warnings if w.split(":")[0] in changes]
    if bad:
        raise ValueError(bad[0])
    out = {key: checked[key] for key in changes}
    for key, value in out.items():
        if isinstance(value, float) and value != changes[key]:
            raise ValueError(f"{key}: out of range")
    data = dict(raw) if raw else dict(DEFAULTS)
    data.update(out)
    _write_atomic(path, data)
    return out


def broken_path(folder):
    """First free backup name: settings.broken.json, then settings.broken-2.json, ..."""
    base, ext = os.path.splitext(os.path.join(folder, BROKEN_NAME))
    path, n = base + ext, 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}{ext}"
    return path


def reset(folder=None):
    """Keep the current (broken) file as a new backup (never over an older
    one: settings.broken.json, settings.broken-2.json, ...), then write the
    defaults. Returns the defaults."""
    path = _path(folder)
    if os.path.exists(path):
        os.rename(path, broken_path(os.path.dirname(path)))  # rename never overwrites
    _write_atomic(path, DEFAULTS)
    return dict(DEFAULTS)


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
