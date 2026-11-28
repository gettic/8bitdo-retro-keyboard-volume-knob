"""What the volume pop-up and the tray icon show. Pure logic, no Windows calls.

A `Row` is one app or site: its name, volume (0-1.5), mute state and the
highest volume it can reach (1.0, or 1.5 for a boost-capable YouTube tab).
`at_max` (default False): the knob was just turned up at the top, so the pop-up
says "Max" instead of the percent (Spotify cannot go past 100 %)."""
import collections
import math

Row = collections.namedtuple("Row", "name level muted max_level at_max", defaults=(False,))

TITLE = "8BitDo Volume Knob"
NAMES = {"spotify.exe": "Spotify", "brave.exe": "Brave", "plex.exe": "Plex",
         "plexamp.exe": "Plexamp", "plex htpc.exe": "Plex HTPC",
         "youtube": "YouTube", "ytmusic": "YouTube Music"}
TIP_MAX = 127  # NOTIFYICONDATAW.szTip holds 128 characters with the terminator


def name_for(source):
    """'spotify.exe' -> 'Spotify', 'ytmusic' -> 'YouTube Music', 'foo.exe' -> 'Foo'."""
    if source in NAMES:
        return NAMES[source]
    base = source[:-4] if source.lower().endswith(".exe") else source
    return base[:1].upper() + base[1:]


def percent(level):
    """Whole percent, half rounded up (1.015 -> 102, not 101 from float error)."""
    return int(math.floor(round(level * 1000) / 10.0 + 0.5))


def predict_tab(old, delta, max_level):
    """The volume a tab will have after a knob step, the way the extension
    applies it (page.js applyNow): at least 1 % per step, crossing 100 % stops
    on 100 % once, clamped to 0..max_level. Levels are 0-1.5."""
    old_p = old * 100.0
    step = delta * 100.0
    if 0 < step < 1:
        step = 1.0
    elif -1 < step < 0:
        step = -1.0
    target = round((old_p + step) * 1000) / 1000
    if (old_p < 100 < target) or (old_p > 100 > target):
        target = 100.0
    target = min(max_level * 100.0, max(0.0, target))
    return target / 100.0


def angle_for(level):
    """Tray knob pointer, degrees clockwise from 12 o'clock: 0 % = 7 o'clock
    (-150), 100 % = 5 o'clock (+150); the boost 100-150 % goes on to 6 o'clock."""
    level = max(0.0, min(1.5, level))
    if level <= 1.0:
        return -150.0 + 300.0 * level
    return 150.0 + 30.0 * (level - 1.0) / 0.5


def row_text(row):
    return f"{row.name} muted" if row.muted else f"{row.name} {percent(row.level)}%"


def tooltip(rows, paused=False, keys_ok=True):
    if paused:
        text = f"{TITLE} - paused (Windows controls the volume)"
    elif not keys_ok:
        text = f"{TITLE} - another program has the volume keys (retrying)"
    elif rows:
        text = f"{TITLE} - " + ", ".join(row_text(r) for r in rows)
    else:
        text = f"{TITLE} - running"
    return text if len(text) <= TIP_MAX else text[:TIP_MAX - 3] + "..."


def icon_key(rows, paused=False):
    """(whole percent or None, muted, paused) for the tray icon: the first row
    (the app the knob changed last; Spotify first when it changed several)."""
    if paused:
        return (None, False, True)
    if not rows:
        return (None, False, False)
    r = rows[0]
    return (min(150, max(0, percent(r.level))), bool(r.muted), False)


def bar_parts(row):
    """Pop-up bar as fractions of the bar width: (normal fill, boost fill,
    position of the 100 % tick or None)."""
    top = row.max_level if row.max_level > 1.0 else 1.0
    level = max(0.0, min(top, row.level))
    normal = min(level, 1.0) / top
    boost = (level - 1.0) / top if level > 1.0 else 0.0
    return normal, boost, (1.0 / top if top > 1.0 else None)
