"""The rule: only what is playing changes; if nothing plays, everything changes.

Pure logic, no Windows calls."""


class AppSource:
    __slots__ = ("exe", "playing", "muted")

    def __init__(self, exe, playing, muted):
        self.exe, self.playing, self.muted = exe, playing, muted


class Decision:
    def __init__(self, apps, tabs, delta=None, muted=None):
        self.apps = apps        # list of exe names to change
        self.tabs = tabs        # {client: [tab ids]}
        self.delta = delta      # volume change, or None for mute
        self.muted = muted      # mute state to set, or None for volume

    def empty(self):
        return not self.apps and not self.tabs

    def __repr__(self):
        return f"Decision(apps={self.apps}, tabs={self.tabs}, delta={self.delta}, muted={self.muted})"


def decide(action, apps, tabs, step):
    """action: "up" | "down" | "mute". apps: [AppSource]. tabs: [browser.Tab]."""
    playing_apps = [a for a in apps if a.playing]
    playing_tabs = [t for t in tabs if t.playing]
    if playing_apps or playing_tabs:
        t_apps, t_tabs = playing_apps, playing_tabs
    else:
        t_apps, t_tabs = list(apps), list(tabs)
    by_client = {}
    for t in t_tabs:
        by_client.setdefault(t.client, []).append(t.tab)
    names = sorted({a.exe for a in t_apps})
    if action == "mute":
        any_unmuted = any(not a.muted for a in t_apps) or any(not t.muted for t in t_tabs)
        return Decision(names, by_client, muted=any_unmuted)
    delta = step if action == "up" else -step
    return Decision(names, by_client, delta=delta)
