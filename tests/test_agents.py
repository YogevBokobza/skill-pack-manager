"""Switching agents on and off: what they get, what Sync removes, what stays."""
import io
import json
import unittest
from unittest import mock

from spm import cli, core, mcp
from spm.core import Model
from spm.server import App
from tests.test_core import git
from tests.test_mcp import FakeClaude, McpEnv


class Switches(McpEnv):
    def model(self, agents=("claude", "opencode", "codex"), gemini=False):
        self.cfg.agents, self.cfg.gemini = tuple(agents), gemini
        m = Model(self.cfg)
        fake = FakeClaude(self.cfg.claude_json, self.cfg.claude_cli)
        m.all_adapters["claude"] = fake
        if "claude" in m.adapters:
            m.adapters["claude"] = fake
        return m

    def gemini_file(self):
        return self.proj / ".gemini/settings.json"

    def test_switching_gemini_off_removes_its_entries(self):
        m = self.model(gemini=True)
        m.run("attach", {"pack": "web", "folder": str(self.proj)}, dry=False)
        self.assertIn("web-mcp", json.loads(self.gemini_file().read_text())["mcpServers"])
        self.assertIn("/.gemini/settings.json", self.excludes())
        m = self.model(gemini=False)
        v = m.folder_view(m.load(), str(self.proj))
        self.assertFalse(v["in_sync"])  # what was added for Gemini is left over
        self.assertEqual(sorted(v["plan"]["mcp_remove"]), [["gemini", "http-mcp"], ["gemini", "web-mcp"]])
        res = m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertEqual(res["errors"], [])
        self.assertFalse(self.gemini_file().exists())  # left empty, so deleted
        self.assertFalse((self.proj / ".gemini").exists())
        self.assertNotIn("/.gemini/settings.json", self.excludes())
        self.assertIn("web-mcp", self.opencode()["mcp"])  # the other agents keep theirs
        self.assertTrue(m.folder_view(m.load(), str(self.proj))["in_sync"])

    def test_user_settings_and_own_entries_stay(self):
        f = self.gemini_file()
        f.parent.mkdir()
        f.write_text(json.dumps({"theme": "x", "mcpServers": {"mine": {"command": "m"}}}))
        m = self.model(gemini=True)
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        m = self.model(gemini=False)
        m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertEqual(json.loads(f.read_text()), {"theme": "x", "mcpServers": {"mine": {"command": "m"}}})

    def test_git_tracked_file_of_an_off_agent_is_not_touched(self):
        m = self.model(gemini=True)
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        git("add", "-f", ".gemini/settings.json", cwd=self.proj)
        m = self.model(gemini=False)
        self.assertEqual(m.folder_view(m.load(), str(self.proj))["plan"]["mcp_remove"], [])

    def test_opencode_off_drops_its_links_and_entries(self):
        m = self.model()
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        m = self.model(agents=("claude", "codex"))
        self.assertEqual(m.skill_dirs, (".claude/skills", ".agents/skills"))
        m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertEqual(self.linked(self.proj, "alpha"), [".claude/skills", ".agents/skills"])
        self.assertNotIn("alpha-mcp", self.opencode().get("mcp", {}))
        v = m.folder_view(m.load(), str(self.proj))
        self.assertEqual({s["skill"]: s["status"] for s in v["skills"]}, {"alpha": "ok", "beta": "ok"})
        self.assertNotIn("opencode", {c for x in v["mcp"] for c in x["clients"]})
        self.store_intact()

    def test_agents_folder_is_shared_by_codex_and_gemini(self):
        m = self.model(gemini=True)
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        m = self.model(agents=("claude", "opencode"), gemini=True)  # Codex off, Gemini on
        m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertIn(".agents/skills", self.linked(self.proj, "alpha"))
        m = self.model(agents=("claude", "opencode"), gemini=False)  # both off
        m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertNotIn(".agents/skills", self.linked(self.proj, "alpha"))

    def test_off_agents_cause_no_global_warnings(self):
        cat = mcp.Catalog({"srv": {"config": {"command": "x"}, "global": ["gemini"], "description": ""}})
        data = {"packs": {"p": {"skills": [], "mcp": ["srv"], "paths": [str(self.proj)]}}}
        on = self.model(gemini=True)
        self.assertTrue(mcp.plan(data, self.proj, cat, on.adapters)["mcp_warnings"])
        off = self.model(gemini=False)
        self.assertEqual(mcp.plan(data, self.proj, cat, off.adapters)["mcp_warnings"], [])
        self.assertEqual(off.on(["gemini", "claude"]), ["claude"])

    def test_sync_all(self):
        m = self.model()
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        m.run("attach", {"pack": "two", "folder": str(self.other)}, dry=False)
        m = self.model(agents=("claude",))
        res = m.run("sync_all", {}, dry=False)
        self.assertEqual(len(res["plans"]), 2)
        self.assertEqual(self.linked(self.other, "gamma"), [".claude/skills"])


class SettingsAndCli(McpEnv):
    def test_settings_switch_and_cli(self):
        app = App(self.cfg)
        self.assertEqual(app.model.agents, ("claude", "opencode", "codex"))
        app.settings.update({"agents": {"opencode": False}})
        app.build()
        self.assertEqual(app.model.agents, ("claude", "codex"))
        self.assertEqual(app.state()["settings"]["agents"]["on"], ["claude", "codex"])
        with self.assertRaises(ValueError):
            app.settings.update({"agents": {"cursor": True}})
        out = io.StringIO()
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cli.run(["agents", "opencode", "on"], app=app, out=out), 0)
            self.assertIn("OpenCode     on", out.getvalue())
            self.assertEqual(cli.run(["agents", "codex", "auto"], app=app, out=io.StringIO()), 1)
        self.assertIn("opencode", app.model.agents)

    def test_skill_dirs_for(self):
        self.assertEqual(core.skill_dirs_for(["gemini"]), (".agents/skills",))
        self.assertEqual(core.skill_dirs_for([]), ())


if __name__ == "__main__":
    unittest.main()
