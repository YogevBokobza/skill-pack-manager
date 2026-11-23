"""Migration from cc-switch to standalone mode, against a fake cc-switch."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spm import migrate
from spm.core import Config, classify, is_link, make_junction
from spm.server import App
from spm.sources import SourceError

SECRET = "pg-secret-value"


class Fixture(unittest.TestCase):
    def setUp(self):
        os.environ["SPM_IGNORE_PROCESSES"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.h = t / "home"
        self.cc_store = self.h / ".cc-switch" / "skills"
        for s in ("alpha", "beta", "gamma"):
            (self.cc_store / s / "refs").mkdir(parents=True)
            (self.cc_store / s / "SKILL.md").write_text(f"---\nname: {s}\ndescription: {s} from cc-switch\n---\n")
            (self.cc_store / s / "refs" / "a.md").write_text("keep me")
        db = self.h / ".cc-switch" / "cc-switch.db"
        con = sqlite3.connect(db)
        con.execute("create table skills (id text, name text, description text, directory text, repo_owner text,"
                    " repo_name text, repo_branch text, enabled_claude int, enabled_codex int, enabled_opencode int,"
                    " enabled_gemini int)")
        con.executemany("insert into skills values (?,?,?,?,?,?,?,?,?,?,?)", [
            ("acme/tools:skills/alpha", "alpha", "", "alpha", "acme", "tools", "main", 1, 1, 1, 1),
            ("local:beta", "beta", "", "beta", None, None, None, 0, 0, 0, 0),
            ("acme/tools:skills/gamma", "gamma", "", "gamma", "acme", "tools", "main", 0, 0, 0, 0),
        ])
        con.execute("create table mcp_servers (id text, name text, server_config text, description text,"
                    " enabled_claude int, enabled_codex int, enabled_opencode int)")
        con.execute("insert into mcp_servers values ('pg','pg',?,'',1,0,0)",
                    (json.dumps({"command": "npx", "args": ["pg-mcp"], "env": {"URL": SECRET}}),))
        con.execute("insert into mcp_servers values ('bad','bad','{\"nothing\": 1}','',0,0,0)")
        con.commit()
        con.close()
        # cc-switch copies globally enabled skills into the agents' folders
        for client_dir in (self.h / ".claude/skills", self.h / ".codex/skills"):
            (client_dir / "alpha").mkdir(parents=True)
            (client_dir / "alpha" / "SKILL.md").write_text("cc-switch copy")
        # a project whose pack links point into the cc-switch store
        self.proj = t / "proj"
        (self.proj / ".claude/skills").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        make_junction(self.cc_store / "alpha", self.proj / ".claude/skills/alpha")
        make_junction(self.cc_store / "gamma", self.proj / ".claude/skills/gamma")
        self.packs = t / "packs.json"
        self.packs.write_text(json.dumps({"packs": {"p": {"skills": ["alpha", "gamma"], "mcp": ["pg"], "paths": [str(self.proj)]}}}))
        self.cfg = Config(store=self.cc_store, packs=self.packs, db=db, data_dir=t / "data", home=self.h,
                          claude_json=self.h / ".claude.json", claude_cli=t / "none",
                          codex_global=self.h / ".codex/config.toml", opencode_dir=self.h / ".config/opencode")
        self.app = App(self.cfg)
        self.local = self.app.local_source(self.cfg)
        # "gamma" already in the new store from another source: must be skipped
        (self.local.store / "gamma").mkdir()
        (self.local.store / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\nother")

    def tearDown(self):
        os.environ.pop("SPM_IGNORE_PROCESSES", None)
        for root, dirs, files in os.walk(self.tmp.name):
            for d in dirs + files:
                p = Path(root) / d
                if is_link(p):
                    os.rmdir(p) if os.path.isjunction(p) else os.unlink(p)
        self.tmp.cleanup()


class Migrate(Fixture):
    def test_plan(self):
        p = self.app.post("/api/migrate", {"dry": True})
        self.assertEqual(p["skills_copy"], ["alpha", "beta"])
        self.assertEqual(p["skills_skip"], ["gamma"])
        self.assertEqual(p["mcp_copy"], ["pg"])
        self.assertEqual(len(p["mcp_invalid"]), 1)
        self.assertEqual(p["relink"], [[str(self.proj), ".claude/skills", "alpha"]])
        self.assertEqual(sorted(p["global"]), [["claude", "alpha", "copy"], ["codex", "alpha", "copy"],
                                               ["opencode", "alpha", "missing"]])
        self.assertEqual(p["other_agents"], ["gemini"])
        self.assertNotIn(SECRET, json.dumps(p))
        self.assertIsNone(self.app.settings.load()["mode"])  # dry run changes nothing

    def test_apply(self):
        res = self.app.post("/api/migrate", {"dry": False})
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.app.settings.load()["mode"], "standalone")
        st = self.app.model.state()
        self.assertEqual(st["mode"], "standalone")
        skills = {s["dir"]: s for s in st["skills"]}
        self.assertEqual(skills["alpha"]["source"], {"url": "https://github.com/acme/tools", "ref": "main",
                                                     "kind": "branch", "path": "skills/alpha", "commit": None})
        self.assertEqual(skills["alpha"]["web_url"], "https://github.com/acme/tools/tree/main/skills/alpha")
        self.assertEqual(skills["beta"]["source"]["local"], str(self.cc_store / "beta"))
        self.assertEqual((self.local.store / "alpha/refs/a.md").read_text(), "keep me")
        self.assertEqual(self.local.mcp_catalog().config("pg")["env"], {"URL": SECRET})
        self.assertNotIn(SECRET, json.dumps(st))
        # the project's pack link now points into the new store; gamma (skipped) still at cc-switch
        self.assertEqual(classify(self.proj / ".claude/skills/alpha", self.local.store), "managed")
        self.assertEqual(classify(self.proj / ".claude/skills/gamma", self.cc_store), "managed")
        # global: cc-switch's copies backed up, replaced by links; opencode link created
        for client in ("claude", "codex", "opencode"):
            self.assertIn(client, skills["alpha"]["global"])
        backup = Path(res["backup"])
        self.assertEqual((backup / "claude/alpha/SKILL.md").read_text(), "cc-switch copy")
        # cc-switch's own store is untouched
        self.assertEqual(sorted(x.name for x in self.cc_store.iterdir()), ["alpha", "beta", "gamma"])
        self.assertEqual((self.cc_store / "alpha/refs/a.md").read_text(), "keep me")
        # running again finds nothing left to do
        p2 = self.app.post("/api/migrate", {"dry": True})
        self.assertEqual((p2["skills_copy"], p2["skills_done"], p2["relink"], p2["global"]),
                         ([], ["alpha", "beta"], [], []))

    def test_options_off(self):
        res = self.app.post("/api/migrate", {"dry": False, "relink": False, "take_over_global": False})
        self.assertEqual(res["backup"], None)
        self.assertEqual(classify(self.proj / ".claude/skills/alpha", self.cc_store), "managed")
        self.assertFalse(is_link(self.h / ".claude/skills/alpha"))

    def test_refuses_while_ccswitch_runs(self):
        with mock.patch.object(migrate, "process_running", return_value=True):
            p = self.app.post("/api/migrate", {"dry": True})
            self.assertTrue(p["ccswitch_running"])
            with self.assertRaises(SourceError):
                self.app.post("/api/migrate", {"dry": False})
        self.assertFalse((self.local.store / "alpha").exists())

    def test_without_ccswitch(self):
        (self.h / ".cc-switch/cc-switch.db").unlink()
        with self.assertRaises(SourceError):
            self.app.post("/api/migrate", {"dry": True})


if __name__ == "__main__":
    unittest.main()
