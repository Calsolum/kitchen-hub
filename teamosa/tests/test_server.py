"""Hub service tests. A fake runner stands in for the CLI, so nothing here uses Bluetooth."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import flask  # noqa: F401
    from teamosa import server
except ImportError:  # Flask is on the hub, not necessarily on a dev machine
    server = None

MON_0644 = datetime(2026, 10, 26, 6, 44, 59)   # 2026-10-26 is a Monday
MON_0645 = datetime(2026, 10, 26, 6, 45, 5)
GUEST = {"REMOTE_ADDR": "10.0.0.57"}


def settings(**changes):
    s = {"enabled": True, "days": [0], "time": "06:45", "temp": 95, "minutes": 3.5}
    s.update(changes)
    return s


class FakeRunner:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, args):
        self.calls.append(args)
        return self.code


def wait_idle(hub):
    for _ in range(200):
        if not hub.lock.locked():
            return
        time.sleep(0.01)
    raise AssertionError("action did not finish")


@unittest.skipIf(server is None, "needs Flask")
class Access(unittest.TestCase):
    def check(self, peer, header, allowed, kiosk=None):
        _, a, k = server.client_access(peer, header)
        self.assertEqual(a, allowed, (peer, header))
        if kiosk is not None:
            self.assertEqual(k, kiosk, (peer, header))

    def test_kiosk_on_the_pi(self):
        self.check("127.0.0.1", None, True, kiosk=True)

    def test_tailnet_device_via_nginx(self):
        self.check("172.18.0.3", "100.101.102.103", True, kiosk=False)
        self.check("172.18.0.3", "fd7a:115c:a1e0::5", True, kiosk=False)

    def test_guest_via_nginx(self):
        self.check("172.18.0.3", "10.0.0.57", False)

    def test_guest_straight_to_the_port_cannot_fake_the_header(self):
        self.check("10.0.0.57", "127.0.0.1", False)

    def test_missing_or_bad_header(self):
        self.check("172.18.0.3", None, False)
        self.check("172.18.0.3", "nonsense", False)


@unittest.skipIf(server is None, "needs Flask")
class Settings(unittest.TestCase):
    def test_normalises(self):
        s = server.validate_settings(settings(time="6:05", days=[4, 0, 0], temp=95.0))
        self.assertEqual(s, {"enabled": True, "days": [0, 4], "time": "06:05",
                             "temp": 95, "minutes": 3.5})

    def test_schedule_off_needs_no_days(self):
        self.assertEqual(server.validate_settings(settings(enabled=False, days=[]))["days"], [])

    def test_rejects(self):
        for bad in [settings(time="25:00"), settings(time="noon"), settings(temp=100),
                    settings(minutes=3.25), settings(enabled=True, days=[]),
                    settings(days=[True]), settings(days=[7]), settings(enabled="yes"),
                    settings(temp="95"), None, []]:
            with self.assertRaises(server.SettingsError, msg=bad):
                server.validate_settings(bad)


@unittest.skipIf(server is None, "needs Flask")
class Schedule(unittest.TestCase):
    def test_next_and_due(self):
        s = settings()
        self.assertEqual(server.next_run(s, MON_0644), datetime(2026, 10, 26, 6, 45))
        self.assertIsNone(server.due(s, MON_0644, None))
        self.assertEqual(server.due(s, MON_0645, None), datetime(2026, 10, 26, 6, 45))

    def test_fired_minute_moves_to_next_week(self):
        s = settings()
        self.assertIsNone(server.due(s, MON_0645, "2026-10-26 06:45"))
        self.assertEqual(server.next_run(s, MON_0645, "2026-10-26 06:45"),
                         datetime(2026, 11, 2, 6, 45))

    def test_passed_minute_is_not_run_late(self):
        self.assertEqual(server.next_run(settings(), datetime(2026, 10, 26, 6, 46)),
                         datetime(2026, 11, 2, 6, 45))

    def test_weekdays(self):
        s = settings(days=[0, 1, 2, 3, 4])
        self.assertEqual(server.next_run(s, datetime(2026, 10, 30, 7, 0)),   # Friday after 6:45
                         datetime(2026, 11, 2, 6, 45))                       # -> Monday

    def test_off(self):
        self.assertIsNone(server.next_run(settings(enabled=False), MON_0644))


@unittest.skipIf(server is None, "needs Flask")
class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = FakeRunner()
        self.hub = server.Hub(self.tmp.name, runner=self.runner, clock=lambda: MON_0644)
        self.client = server.create_app(self.hub).test_client()

    def tearDown(self):
        wait_idle(self.hub)
        self.tmp.cleanup()

    def test_guest_sees_nothing(self):
        r = self.client.get("/status", environ_base=GUEST)
        self.assertEqual(r.get_json(), {"allowed": False})

    def test_guest_cannot_change_or_brew(self):
        self.assertEqual(self.client.post("/settings", json=settings(), environ_base=GUEST).status_code, 403)
        self.assertEqual(self.client.post("/brew", json={"confirm": True}, environ_base=GUEST).status_code, 403)
        self.assertEqual(self.client.post("/check", json={"go": True}, environ_base=GUEST).status_code, 403)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "settings.json")))
        self.assertEqual(self.runner.calls, [])

    def test_kiosk_saves_settings(self):
        r = self.client.post("/settings", json=settings())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["next_run"], "2026-10-26T06:45")
        with open(os.path.join(self.tmp.name, "settings.json")) as f:
            self.assertEqual(json.load(f)["temp"], 95)

    def test_bad_settings_explained(self):
        r = self.client.post("/settings", json=settings(temp=100))
        self.assertEqual(r.status_code, 400)
        self.assertIn("100", r.get_json()["error"])

    def test_brew_needs_confirm(self):
        self.assertEqual(self.client.post("/brew", json={}).status_code, 400)
        self.assertEqual(self.client.post("/brew", data="confirm=true").status_code, 400)
        self.assertEqual(self.runner.calls, [])

    def test_brew_now_uses_saved_settings(self):
        self.client.post("/settings", json=settings(temp=80, minutes=4))
        self.assertEqual(self.client.post("/brew", json={"confirm": True}).status_code, 202)
        wait_idle(self.hub)
        state = os.path.join(self.tmp.name, "state")
        self.assertEqual(self.runner.calls,
                         [["brew", "--state-dir", state, "--temp", "80", "--time", "4.0"]])
        self.assertEqual(self.hub.action["exit"], 0)

    def test_check(self):
        self.assertEqual(self.client.post("/check", json={}).status_code, 400)
        self.assertEqual(self.client.post("/check", json={"go": True}).status_code, 202)
        wait_idle(self.hub)
        self.assertEqual(self.runner.calls[0][0], "check")

    def test_busy(self):
        self.hub.lock.acquire()
        try:
            self.assertEqual(self.client.post("/brew", json={"confirm": True}).status_code, 409)
        finally:
            self.hub.lock.release()


@unittest.skipIf(server is None, "needs Flask")
class Tick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "settings.json"), "w") as f:
            json.dump(settings(), f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fires_once_per_minute_even_across_a_restart(self):
        runner = FakeRunner()
        hub = server.Hub(self.tmp.name, runner=runner, clock=lambda: MON_0645)
        hub.tick()
        hub.tick()
        server.Hub(self.tmp.name, runner=runner, clock=lambda: MON_0645).tick()
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0], "brew")
        self.assertEqual(hub.action["kind"], "scheduled")

    def test_quiet_before_time(self):
        runner = FakeRunner()
        server.Hub(self.tmp.name, runner=runner, clock=lambda: MON_0644).tick()
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
