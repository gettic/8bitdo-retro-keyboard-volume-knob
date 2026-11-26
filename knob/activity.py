"""Decides which Windows apps are "playing" from their sound level."""


class ActivityTracker:
    """An app is playing if its peak went above `threshold` within the last
    `hold` seconds. Times are passed in (seconds, monotonic)."""

    def __init__(self, threshold=0.001, hold=1.5):
        self.threshold = threshold
        self.hold = hold
        self._last_sound = {}

    def update(self, app, peak, now):
        if peak > self.threshold:
            self._last_sound[app] = now

    def is_playing(self, app, now):
        t = self._last_sound.get(app)
        return t is not None and now - t <= self.hold

    def forget_except(self, apps):
        for app in list(self._last_sound):
            if app not in apps:
                del self._last_sound[app]
