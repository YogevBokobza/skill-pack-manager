"""When the app process exits: it must outlive a throttled background window."""
import unittest
import urllib.error
import urllib.request

from spm.__main__ import HEARTBEAT_TIMEOUT, RELOAD_GRACE, exit_reason
from spm.server import App, serve
from tests.test_core import Env


class Proc:
    def __init__(self, alive):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


class FakeApp:
    def __init__(self, last_ping, bye_at=0.0):
        self.last_ping, self.bye_at = last_ping, bye_at


class ExitReason(unittest.TestCase):
    def test_window_process_alive_keeps_serving_without_pings(self):
        # a background window whose timers are throttled for an hour
        self.assertIsNone(exit_reason(FakeApp(last_ping=0), Proc(True), now=3600))

    def test_window_closed_after_bye(self):
        self.assertIsNone(exit_reason(FakeApp(100, bye_at=101), Proc(False), now=101 + RELOAD_GRACE - 1))
        self.assertEqual(exit_reason(FakeApp(100, bye_at=101), Proc(False), now=101 + RELOAD_GRACE + 1),
                         "window closed, exiting")

    def test_reload_pings_again_after_bye(self):
        self.assertIsNone(exit_reason(FakeApp(last_ping=105, bye_at=101), Proc(False), now=130))

    def test_no_browser_process_falls_back_to_long_heartbeat(self):
        self.assertIsNone(exit_reason(FakeApp(100), None, now=100 + 90))  # one throttled minute is fine
        self.assertEqual(exit_reason(FakeApp(100), None, now=100 + HEARTBEAT_TIMEOUT + 1), "no heartbeat, exiting")


class Bye(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.srv, self.port = serve(self.app)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, query):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/bye{query}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def test_bye_needs_the_token(self):
        self.assertEqual(self.post("?t=wrong"), 403)
        self.assertEqual(self.app.bye_at, 0.0)
        self.assertEqual(self.post("?t=" + self.app.token), 200)
        self.assertGreater(self.app.bye_at, 0)


if __name__ == "__main__":
    unittest.main()
