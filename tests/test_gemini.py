"""Gemini CLI: MCP in .gemini/settings.json, global skills, usage, costs, the switch."""
import hashlib
import json
import unittest
from pathlib import Path

from spm import insights, mcp, usage
from spm.core import Model
from spm.server import App
from tests.test_core import Env, git
from tests.test_mcp import FakeClaude, McpEnv, SECRET


class Adapter(Env):
    def test_spec_per_transport(self):
        self.assertEqual(mcp.GeminiFile.spec({"command": "node", "args": ["a"], "env": {"K": "v"}}),
                         {"command": "node", "args": ["a"], "env": {"K": "v"}})
        self.assertEqual(mcp.GeminiFile.spec({"type": "sse", "url": "https://x/sse"}), {"url": "https://x/sse"})
        self.assertEqual(mcp.GeminiFile.spec({"type": "http", "url": "https://x/mcp", "headers": {"A": "b"}}),
                         {"httpUrl": "https://x/mcp", "headers": {"A": "b"}})

    def test_keeps_other_settings_and_refuses_comments(self):
        a = mcp.GeminiFile()
        f = self.proj / ".gemini/settings.json"
        f.parent.mkdir()
        f.write_text(json.dumps({"theme": "Dracula", "mcpServers": {"mine": {"command": "x"}}}))
        a.add(self.proj, "srv", {"command": "node"})
        j = json.loads(f.read_text())
        self.assertEqual(j["theme"], "Dracula")
        self.assertEqual(set(j["mcpServers"]), {"mine", "srv"})
        a.remove(self.proj, "srv")
        self.assertEqual(json.loads(f.read_text()), {"theme": "Dracula", "mcpServers": {"mine": {"command": "x"}}})
        f.write_text('{\n  // my notes\n  "theme": "x"\n}\n')
        self.assertEqual(a.read(self.proj), {})
        with self.assertRaises(mcp.McpError):
            a.add(self.proj, "srv", {"command": "node"})
        self.assertIn("// my notes", f.read_text())  # untouched


class Folder(McpEnv):
    def setUp(self):
        super().setUp()
        self.cfg.gemini = True
        self.m = Model(self.cfg)
        self.m.adapters["claude"] = FakeClaude(self.cfg.claude_json, self.cfg.claude_cli)

    def gemini(self):
        f = self.proj / ".gemini/settings.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def test_attach_and_detach_write_gemini_settings(self):
        self.assertIn("gemini", self.m.agents)
        self.m.run("attach", {"pack": "web", "folder": str(self.proj)}, dry=False)
        servers = self.gemini()["mcpServers"]
        self.assertEqual(servers["web-mcp"], {"url": "https://example.test/sse", "headers": {"Authorization": SECRET}})
        self.assertEqual(servers["http-mcp"], {"httpUrl": "https://example.test/mcp"})
        self.assertIn("/.gemini/settings.json", self.excludes())
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual({m["name"]: m["status"] for m in v["mcp"]}, {"http-mcp": "ok", "web-mcp": "ok"})
        self.assertNotIn(SECRET, json.dumps(v))
        self.m.run("detach", {"pack": "web", "folder": str(self.proj)}, dry=False)
        self.assertEqual(self.gemini(), {})

    def test_git_tracked_settings_are_left_alone(self):
        f = self.proj / ".gemini/settings.json"
        f.parent.mkdir()
        f.write_text('{"theme": "x"}')
        git("add", ".gemini/settings.json", cwd=self.proj)
        res = self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=True)
        self.assertIn(["gemini", "alpha-mcp", "locked"], res["plans"][0]["mcp_conflicts"])

    def test_off_by_default_in_a_plain_config(self):
        self.cfg.gemini = False
        self.assertNotIn("gemini", Model(self.cfg).agents)
        self.assertNotIn("gemini", Model(self.cfg).adapters)


class Switch(Env):
    def test_automatic_follows_the_gemini_folder(self):
        app = App(self.cfg)
        self.assertNotIn("gemini", app.model.agents)
        (self.home / ".gemini").mkdir(parents=True)
        app.build()
        self.assertIn("gemini", app.model.agents)
        app.settings.update({"agents": {"gemini": False}})
        app.build()
        self.assertNotIn("gemini", app.model.agents)
        s = app.state()["settings"]["agents"]
        self.assertEqual((s["gemini"], s["gemini_found"], s["gemini_on"]), (False, True, False))

    def test_standalone_global_skills_and_mcp(self):
        (self.home / ".gemini").mkdir(parents=True)
        app = App(self.cfg)
        app.settings.update({"mode": "standalone"})
        app.build()
        src = app.local()
        self.assertEqual(src.global_dirs["gemini"], self.home / ".gemini/skills")
        self.assertIn("gemini", src.global_mcp)
        src.save_mcp("srv", {"command": "node"})
        src.set_mcp_global("srv", "gemini", True)
        self.assertEqual(json.loads((self.home / ".gemini/settings.json").read_text())["mcpServers"]["srv"],
                         {"command": "node", "args": []})
        self.assertEqual(src.mcp_catalog().servers["srv"]["global"], ["gemini"])


class GeminiUsage(Env):
    def test_sessions_by_slug_and_by_hash(self):
        gh = self.home / ".gemini"
        proj = str(self.proj)
        legacy = str(self.other)
        (gh / "tmp/my-proj/chats").mkdir(parents=True)
        (gh / "projects.json").write_text(json.dumps({"projects": {proj: "my-proj", legacy: "other"}}))
        rec = {"id": "1", "timestamp": "2026-04-02T10:00:00Z", "type": "gemini",
               "toolCalls": [{"id": "t", "name": "activate_skill", "args": {"name": "alpha"}, "status": "success"}]}
        read = {"timestamp": "2026-04-03T10:00:00Z", "type": "gemini",
                "toolCalls": [{"name": "read_file", "args": {"file_path": "/d/routers/using-x/references/beta/beta.md"}}]}
        (gh / "tmp/my-proj/chats/session-1.jsonl").write_text(
            json.dumps({"sessionId": "s", "startTime": "2026-04-02T09:00:00Z"}) + "\n" + json.dumps(rec) + "\n"
            + json.dumps(read) + "\n")
        old = gh / "tmp" / hashlib.sha256(legacy.encode()).hexdigest() / "chats"
        old.mkdir(parents=True)
        (old / "session-old.json").write_text(json.dumps(
            {"startTime": "2025-12-01T00:00:00Z", "messages": [rec | {"timestamp": "2025-12-01T00:00:00Z"}]}, indent=2))
        rows, since = usage.events({"alpha", "beta"}, self.home, gh)
        got = sorted((r[0], r[1], r[2], r[4]) for r in rows if r[3] == "gemini")
        self.assertEqual(got, [("alpha", "2025-12-01", legacy, "call"), ("alpha", "2026-04-02", proj, "call"),
                               ("beta", "2026-04-03", proj, "router")])
        self.assertEqual(usage.events({"alpha"}, self.home, None)[0], [])  # off: not read

    def test_model_reads_gemini_only_when_on(self):
        gh = self.home / ".gemini/tmp/p/chats"
        gh.mkdir(parents=True)
        (gh / "s.jsonl").write_text(json.dumps({"timestamp": "2026-04-02T00:00:00Z",
                                                "toolCalls": [{"name": "activate_skill", "args": {"name": "gamma"}}]}) + "\n")
        self.assertNotIn("gamma", self.m.usage())
        self.cfg.gemini = True
        m = Model(self.cfg)
        self.assertEqual(m.usage()["gamma"]["agents"], {"gemini": 1})


class GeminiCost(Env):
    def test_cost_column_when_on(self):
        self.cfg.gemini = True
        m = Model(self.cfg)
        g = self.home / ".gemini/skills/solo"
        g.mkdir(parents=True)
        (g / "SKILL.md").write_text("---\nname: solo\ndescription: one\n---\n")
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        c = insights.folder_cost(m, m.load(), self.proj)
        self.assertEqual(c["gemini"]["count"], 3)  # alpha, beta from .agents/skills, solo from ~/.gemini/skills
        self.assertEqual(c["claude"]["count"], 2)
        self.assertNotIn("gemini", insights.folder_cost(self.m, self.m.load(), self.proj))


if __name__ == "__main__":
    unittest.main()
