import io
import json
import os
import unittest
from unittest import mock

from spm import __main__ as entry, cli
from spm.server import App
from tests.test_core import Env


class Input(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class Cli(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)

    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            code = cli.run(list(argv), app=self.app, out=out, stdin=stdin or Input())
        return code, out.getvalue(), err.getvalue()

    def test_attach_needs_yes_outside_a_terminal(self):
        code, out, err = self.run_cli("attach", "one", str(self.proj))
        self.assertEqual(code, 2)
        self.assertIn("link alpha, beta", out)
        self.assertIn("--yes", err)
        self.assertEqual(self.linked(self.proj, "alpha"), [])

    def test_attach_with_yes_and_dry_run(self):
        self.assertEqual(self.run_cli("attach", "one", str(self.proj), "--dry-run")[0], 0)
        self.assertEqual(self.linked(self.proj, "alpha"), [])
        code, out, _ = self.run_cli("attach", "one", str(self.proj), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("done, 6 changes", out)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        code, out, _ = self.run_cli("status")
        self.assertIn("in sync", out)
        self.assertIn("[one]", out)
        code, out, _ = self.run_cli("detach", "one", str(self.proj), "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.linked(self.proj, "alpha"), [])
        self.store_intact()

    def test_terminal_asks_before_applying(self):
        code, out, _ = self.run_cli("attach", "one", str(self.proj), stdin=Input("n\n", tty=True))
        self.assertEqual(code, 2)
        self.assertIn("Apply? [y/N]", out)
        self.assertEqual(self.linked(self.proj, "alpha"), [])
        code, _, _ = self.run_cli("attach", "one", str(self.proj), stdin=Input("y\n", tty=True))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.linked(self.proj, "beta")), 3)

    def test_folder_defaults_to_current_directory(self):
        cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            code, _, _ = self.run_cli("extra", "add", "gamma", "-y")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.linked(self.proj, "gamma")), 3)

    def test_json_output(self):
        self.run_cli("attach", "two", str(self.proj), "-y")
        code, out, _ = self.run_cli("status", str(self.proj), "--json")
        v = json.loads(out)
        self.assertEqual(v["packs"], ["two"])
        self.assertTrue(v["in_sync"])
        code, out, _ = self.run_cli("packs", "--json")
        self.assertEqual({p["name"] for p in json.loads(out)}, {"one", "two"})
        code, out, _ = self.run_cli("tokens", str(self.proj), "--json")
        self.assertEqual(json.loads(out)["claude"]["count"], 2)
        code, out, _ = self.run_cli("attach", "one", str(self.proj), "--json")  # plan only, not applied
        self.assertFalse(json.loads(out)["applied"])

    def test_pack_add_and_remove(self):
        self.run_cli("attach", "one", str(self.proj), "-y")
        code, out, _ = self.run_cli("pack", "add", "one", "gamma", "delta", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.linked(self.proj, "delta")), 3)
        code, _, _ = self.run_cli("pack", "remove", "one", "delta", "-y")
        self.assertEqual(self.linked(self.proj, "delta"), [])
        self.assertEqual(self.m.load()["packs"]["one"]["skills"], ["alpha", "beta", "gamma"])

    def test_sync_all_and_views(self):
        self.run_cli("attach", "one", str(self.proj), "-y")
        os.rmdir(self.proj / ".claude/skills/alpha") if os.path.isjunction(self.proj / ".claude/skills/alpha") \
            else os.unlink(self.proj / ".claude/skills/alpha")
        code, out, _ = self.run_cli("status")
        self.assertIn("out of sync", out)
        code, out, _ = self.run_cli("sync", "--all", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        self.assertIn("alpha", self.run_cli("skills", "alph")[1])
        self.assertIn("usage was never counted", self.run_cli("usage")[2])
        self.assertIn("nothing to suggest", self.run_cli("suggest")[1])

    def test_errors_exit_1(self):
        code, _, err = self.run_cli("attach", "nope", str(self.proj), "-y")
        self.assertEqual(code, 1)
        self.assertIn("no such pack: 'nope'", err)
        code, _, err = self.run_cli("check-updates")
        self.assertEqual(code, 1)
        self.assertIn("standalone", err)

    def test_entry_point_routes_commands(self):
        with mock.patch.object(cli, "run", return_value=0) as run:
            with self.assertRaises(SystemExit) as e:
                entry.main(["status", "--json"])
        self.assertEqual(e.exception.code, 0)
        run.assert_called_once_with(["status", "--json"])


if __name__ == "__main__":
    unittest.main()
