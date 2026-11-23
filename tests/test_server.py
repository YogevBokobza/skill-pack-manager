import json
import urllib.error
import urllib.request

from spm.server import App, serve
from tests.test_core import Env


class Server(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.srv, self.port = serve(self.app)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("X-SPM-Token", self.app.token)
        if host:
            req.add_header("Host", host)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_closed_page_is_not_an_error(self):
        """A window closed mid-request: the answer can't be sent, and that is
        no reason for a traceback (it used to print two)."""
        import io
        from unittest import mock
        from spm.server import make_handler
        h = object.__new__(make_handler(self.app, {"port": self.port}))
        h._allowed = lambda: True
        sent = []

        def gone(code, body):
            sent.append(code)
            raise ConnectionAbortedError(10053, "aborted")
        h._json = gone
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            h._dispatch(lambda: {"ok": True})
            h._dispatch(lambda: 1 / 0)  # a real error is still printed
        self.assertEqual(sent, [200, 500])
        self.assertEqual(err.getvalue().count("Traceback"), 1)
        self.assertIn("ZeroDivisionError", err.getvalue())

    def test_rejects_missing_token(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 403)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)

    def test_state_and_op_roundtrip(self):
        code, st = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual({p["name"] for p in st["packs"]}, {"one", "two"})
        code, res = self.call("/api/op", {"op": "attach", "args": {"pack": "one", "folder": str(self.proj)}, "dry": False})
        self.assertEqual(code, 200)
        self.assertEqual(len(res["log"]), 6)
        code, v = self.call("/api/folder?path=" + urllib.request.quote(str(self.proj)))
        self.assertTrue(v["in_sync"])

    def test_bad_op_is_400(self):
        code, res = self.call("/api/op", {"op": "attach", "args": {"pack": "nope", "folder": "x"}, "dry": True})
        self.assertEqual(code, 400)
        self.assertIn("no such pack", res["error"])

    def test_theme_is_saved_and_served_in_html(self):
        def page():
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
                return r.read().decode()
        self.assertIn('data-theme-pref="system"', page())
        code, s = self.call("/api/settings", {"theme": "dark"})
        self.assertEqual((code, s["theme"]), (200, "dark"))
        self.assertIn('data-theme-pref="dark"', page())
        self.assertEqual(App(self.cfg).settings.load()["theme"], "dark")  # survives a restart

    def test_first_run_then_mode_switch(self):
        code, st = self.call("/api/state")
        self.assertIsNone(st["settings"]["mode"])  # the UI asks
        self.assertEqual(st["mode"], "cc-switch")
        code, res = self.call("/api/repo/refs", {"url": "https://example.test/x.git"})
        self.assertEqual(code, 400)
        self.assertIn("standalone mode", res["error"])
        self.call("/api/settings", {"mode": "standalone"})
        code, st = self.call("/api/state")
        self.assertEqual((st["mode"], st["editable"]), ("standalone", True))
        self.assertEqual(st["skills"], [])  # own, empty store
        self.assertTrue(st["config"]["store"].startswith(str(self.cfg.data_dir)))
        code, res = self.call("/api/mcp/save", {"name": "srv", "config": {"command": "node", "env": {"K": "secret-x"}}})
        self.assertEqual(code, 200)
        code, st = self.call("/api/state")
        self.assertEqual(st["mcp"][0]["env_keys"], ["K"])
        self.assertNotIn("secret-x", json.dumps(st))
        code, res = self.call("/api/settings", {"mode": "nope"})
        self.assertEqual(code, 400)

    def test_scan_roots_setting(self):
        code, s = self.call("/api/settings", {"scan_roots": [str(self.other)]})
        self.assertEqual(code, 200)
        code, st = self.call("/api/state")
        self.assertEqual(st["settings"]["scan_roots"], [str(self.other)])

    def test_open_url_only_http(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "C:\\Windows\\system32\\calc.exe", "https://x y"):
            self.assertEqual(self.call("/api/open-url", {"url": bad})[0], 400)

    def test_ccswitch_skills_carry_source_and_link(self):
        import sqlite3
        con = sqlite3.connect(self.cfg.db)
        con.execute("create table skills (id text, name text, description text, directory text, repo_owner text,"
                    " repo_name text, repo_branch text, enabled_claude int)")
        con.execute("insert into skills values ('acme/tools:skills/alpha','alpha','','alpha','acme','tools','main',1)")
        con.commit()
        con.close()
        code, st = self.call("/api/state")
        alpha = {s["dir"]: s for s in st["skills"]}["alpha"]
        self.assertEqual((alpha["source"]["ref"], alpha["source"]["kind"]), ("main", "branch"))
        self.assertEqual(alpha["web_url"], "https://github.com/acme/tools/tree/main/skills/alpha")

    def test_bad_theme_rejected(self):
        code, res = self.call("/api/settings", {"theme": "\"><script>"})
        self.assertEqual(code, 400)

    def test_index_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn(b"Skill Pack Manager", r.read())
