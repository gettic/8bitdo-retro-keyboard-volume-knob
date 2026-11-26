"""Failsafe rules (pure logic, no Windows calls)."""
import unittest

from knob.failsafe import Failsafe

START = 1000.0


def t(tid, volume, site="youtube"):
    return {"tab": tid, "site": site, "playing": True, "volume": volume, "muted": False,
            "max_volume": 1.5, "boost": "on" if (volume or 0) > 1 else "off"}


def closed():
    return False


def spotify_open():
    return True


class FailsafeTests(unittest.TestCase):
    def setUp(self):
        self.f = Failsafe(START, level=0.5, start_window=20)

    def late(self):
        return START + 60

    # a) start
    def test_start_window_checks_every_tab(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3), t(2, 0.8), t(3, 1.0)], START + 5,
                                        spotify_open), [1])

    def test_start_window_ignores_others_open(self):
        self.f.on_tabs(1, [t(1, 0.4)], START + 1, closed)
        self.assertEqual(self.f.on_tabs(2, [t(9, 1.2)], START + 10, spotify_open), [9])

    def test_start_check_only_on_first_sight(self):
        self.f.on_tabs(1, [t(1, 0.9)], START + 1, closed)
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3)], START + 5, closed), [])  # user boost

    def test_null_volume_checked_later(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, None)], START + 19, closed), [])
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.4)], START + 25, closed), [1])
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.4)], START + 26, closed), [])  # once

    def test_null_volume_then_quiet(self):
        self.f.on_tabs(1, [t(1, None)], START + 1, closed)
        self.assertEqual(self.f.on_tabs(1, [t(1, 0.7)], START + 2, closed), [])
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3)], START + 3, closed), [])

    # b) opening
    def test_new_tab_alone_reset(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3)], self.late(), closed), [1])

    def test_new_tab_not_loud_untouched(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 0.8), t(2, 1.0)], self.late(), closed), [])

    def test_new_tab_with_spotify_open(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3)], self.late(), spotify_open), [])

    def test_new_tab_with_other_tab_open(self):
        self.f.on_tabs(1, [t(1, 0.5)], self.late(), closed)
        self.assertEqual(self.f.on_tabs(1, [t(1, 0.5), t(2, 1.3)], self.late() + 1, closed), [])

    def test_other_tab_on_other_host_counts(self):
        self.f.on_tabs(1, [t(1, 0.5)], self.late(), closed)
        self.assertEqual(self.f.on_tabs(2, [t(2, 1.3)], self.late() + 1, closed), [])

    def test_two_tabs_in_one_batch(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3), t(2, 1.2, "ytmusic")], self.late(),
                                        closed), [1, 2])

    def test_two_tabs_quickly_one_after_another(self):
        self.assertEqual(self.f.on_tabs(1, [t(1, 1.3)], self.late(), closed), [1])
        self.assertEqual(self.f.on_tabs(1, [t(1, 0.5), t(2, 1.3)], self.late() + 0.2, closed), [])

    def test_closed_tab_no_longer_counts(self):
        self.f.on_tabs(1, [t(1, 0.5)], self.late(), closed)
        self.f.on_tabs(1, [], self.late() + 1, closed)
        self.assertEqual(self.f.on_tabs(1, [t(2, 1.3)], self.late() + 2, closed), [2])

    def test_reconnect_same_ids_not_new(self):
        self.f.on_tabs(1, [t(1, 1.3)], self.late(), spotify_open)  # Spotify open: kept
        self.f.host_gone(1)
        self.assertEqual(self.f.on_tabs(2, [t(1, 1.3)], self.late() + 10, closed), [])

    def test_brave_restart_new_ids(self):
        self.f.on_tabs(1, [t(1, 0.5)], self.late(), closed)
        self.f.host_gone(1)
        self.assertEqual(self.f.on_tabs(2, [t(50, 1.3), t(51, 1.1)], self.late() + 5, closed),
                         [50, 51])

    def test_memory_expires(self):
        self.f.on_tabs(1, [t(1, 0.5)], self.late(), closed)
        self.f.host_gone(1)
        self.assertEqual(self.f.on_tabs(2, [t(1, 1.3)], self.late() + 31, closed), [1])

    def test_apps_open_only_asked_when_needed(self):
        calls = []

        def apps():
            calls.append(1)
            return False
        self.f.on_tabs(1, [t(1, 0.5)], START + 1, apps)       # start window: not asked
        self.f.on_tabs(1, [t(1, 0.5)], START + 25, apps)      # known tab: not asked
        self.assertEqual(calls, [])

    def test_disabled(self):
        f = Failsafe(START, enabled=False)
        self.assertEqual(f.on_tabs(1, [t(1, 1.5)], START + 1, closed), [])


if __name__ == "__main__":
    unittest.main()
