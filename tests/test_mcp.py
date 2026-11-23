import json
import sqlite3
import subprocess
import tomllib
import unittest
from pathlib import Path

from spm import mcp
from spm.core import Model, OpError
from tests.test_core import Env, git

SECRET = "s3cr3t-value-never-in-api"


class FakeClaude(mcp.ClaudeLocal):
    """Edits the temp claude.json the way `claude mcp add-json/remove --scope local` does."""

    def run_cli(self, folder, args):
        j = json.loads(self.claude_json.read_text()) if self.claude_json.exists() else {}
        proj = j.setdefault("projects", {}).setdefault(str(folder).replace("\\", "/"), {})
        servers = proj.setdefault("mcpServers", {})
        if args[1] == "add-json":
            servers[args[4]] = json.loads(args[5])
        elif args[1] == "remove":
            servers.pop(args[4], None)
        self.claude_json.write_text(json.dumps(j))
        self._cache = None


class McpEnv(Env):
    def setUp(self):
        super().setUp()
        con = sqlite3.connect(self.cfg.db)
        con.execute("create table mcp_servers (id text, name text, server_config text, description text,"
                    " enabled_claude int default 0, enabled_codex int default 0, enabled_opencode int default 0)")
        rows = [
            ("alpha-mcp", {"type": "stdio", "command": "node", "args": ["a.js"], "env": {"KEY": SECRET}}, 0),
            ("web-mcp", {"type": "sse", "url": "https://example.test/sse", "headers": {"Authorization": SECRET}}, 0),
            ("http-mcp", {"type": "http", "url": "https://example.test/mcp"}, 0),
            ("glob-mcp", {"command": "glob"}, 1),
        ]
        for name, cfg, glob in rows:
            con.execute("insert into mcp_servers (id, name, server_config, enabled_claude) values (?,?,?,?)",
                        (name, name, json.dumps(cfg), glob))
        con.commit()
        con.close()
        data = json.loads(self.packs.read_text())
        data["packs"]["one"]["mcp"] = ["alpha-mcp"]
        data["packs"]["web"] = {"skills": [], "mcp": ["web-mcp", "http-mcp"], "paths": []}
        self.packs.write_text(json.dumps(data))
        self.m = Model(self.cfg)
        self.m.adapters["claude"] = FakeClaude(self.cfg.claude_json, self.cfg.claude_cli)

    def claude(self, folder=None):
        return self.m.adapters["claude"].read(folder or self.proj)

    def opencode(self, folder=None):
        f = (folder or self.proj) / ".opencode/opencode.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def codex(self, folder=None):
        f = (folder or self.proj) / ".codex/config.toml"
        return tomllib.loads(f.read_text()) if f.exists() else {}


class Attach(McpEnv):
    def test_attach_writes_all_three_clients(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(self.claude()["alpha-mcp"], {"type": "stdio", "command": "node", "args": ["a.js"], "env": {"KEY": SECRET}})
        oc = self.opencode()["mcp"]["alpha-mcp"]
        self.assertEqual(oc["command"], ["node", "a.js"])
        self.assertEqual(oc["environment"], {"KEY": SECRET})
        cx = self.codex()["mcp_servers"]["alpha-mcp"]
        self.assertEqual((cx["command"], cx["args"], cx["env"]), ("node", ["a.js"], {"KEY": SECRET}))
        trust = tomllib.loads(self.cfg.codex_global.read_text())["projects"]
        self.assertEqual(list(trust.values()), [{"trust_level": "trusted"}])
        ex = self.excludes()
        self.assertIn("/.opencode/opencode.json", ex)
        self.assertIn("/.codex/config.toml", ex)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj, capture_output=True, text=True)
        self.assertEqual(st.stdout.strip(), "")
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual([(x["name"], x["status"]) for x in v["mcp"]], [("alpha-mcp", "ok")])
        self.assertTrue(v["in_sync"])

    def test_url_servers(self):
        res = self.m.run("attach", {"pack": "web", "folder": str(self.proj)}, dry=False)
        self.assertEqual(self.claude()["web-mcp"]["type"], "sse")
        self.assertEqual(self.opencode()["mcp"]["web-mcp"]["type"], "remote")
        self.assertNotIn("web-mcp", self.codex()["mcp_servers"])  # Codex has no SSE
        self.assertEqual(self.codex()["mcp_servers"]["http-mcp"]["url"], "https://example.test/mcp")
        self.assertTrue(any("web-mcp skipped for codex" in w for w in res["plans"][0]["mcp_warnings"]))
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual({x["name"]: x["status"] for x in v["mcp"]}, {"web-mcp": "ok", "http-mcp": "ok"})

    def test_global_server_warns(self):
        self.m.run("create_pack", {"pack": "g"}, dry=False)
        self.m.run("pack_add_mcp", {"pack": "g", "mcp": "glob-mcp"}, dry=False)
        res = self.m.run("attach", {"pack": "g", "folder": str(self.proj)}, dry=True)
        self.assertTrue(any("already enabled globally for claude" in w for w in res["plans"][0]["mcp_warnings"]))

    def test_unknown_server_rejected(self):
        with self.assertRaises(OpError):
            self.m.run("pack_add_mcp", {"pack": "one", "mcp": "nope"})
        with self.assertRaises(OpError):
            self.m.run("mcp_extra_add", {"folder": str(self.proj), "mcp": "nope"})


class Detach(McpEnv):
    def test_detach_removes_only_managed_entries(self):
        # project's own servers in all three places, with names cc-switch does not know
        (self.proj / ".opencode").mkdir()
        (self.proj / ".opencode/opencode.json").write_text(json.dumps({"theme": "x", "mcp": {"mine": {"type": "local", "command": ["m"]}}}))
        (self.proj / ".codex").mkdir()
        (self.proj / ".codex/config.toml").write_text('model = "x"\n\n[mcp_servers.mine]\ncommand = "m"\n\n[mcp_servers.mine.env]\nA = "1"\n')
        self.m.adapters["claude"].run_cli(self.proj, ["mcp", "add-json", "--scope", "local", "mine", '{"command":"m"}'])
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        res = self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(sorted(res["plans"][0]["mcp_remove"]), [["claude", "alpha-mcp"], ["codex", "alpha-mcp"], ["opencode", "alpha-mcp"]])
        self.assertEqual(list(self.claude()), ["mine"])
        self.assertEqual(self.opencode(), {"theme": "x", "mcp": {"mine": {"type": "local", "command": ["m"]}}})
        cx = self.codex()
        self.assertEqual(cx["model"], "x")
        self.assertEqual(cx["mcp_servers"], {"mine": {"command": "m", "env": {"A": "1"}}})
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual([(x["name"], x["status"]) for x in v["mcp"]], [("mine", "own")])

    def test_git_tracked_file_is_never_edited(self):
        (self.proj / ".opencode").mkdir()
        tracked = json.dumps({"mcp": {"alpha-mcp": {"type": "local", "command": ["team-version"]}}})
        (self.proj / ".opencode/opencode.json").write_text(tracked)
        git("add", ".opencode/opencode.json", cwd=self.proj)
        res = self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertIn(["opencode", "alpha-mcp", "locked"], res["plans"][0]["mcp_conflicts"])
        self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual((self.proj / ".opencode/opencode.json").read_text(), tracked)
        self.assertNotIn("alpha-mcp", self.claude())

    def test_extra_mcp_and_pack_remove(self):
        self.m.run("mcp_extra_add", {"folder": str(self.proj), "mcp": "http-mcp"}, dry=False)
        self.assertIn("http-mcp", self.claude())
        self.assertEqual(json.loads(self.packs.read_text())["folders"][str(self.proj)], {"mcp": ["http-mcp"]})
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.m.run("pack_remove_mcp", {"pack": "one", "mcp": "alpha-mcp"}, dry=False)
        self.assertNotIn("alpha-mcp", self.claude())
        self.assertIn("http-mcp", self.claude())
        self.m.run("mcp_extra_remove", {"folder": str(self.proj), "mcp": "http-mcp"}, dry=False)
        self.assertEqual(self.claude(), {})
        self.assertNotIn("folders", json.loads(self.packs.read_text()))


class NoSecrets(McpEnv):
    def test_state_and_views_never_contain_secret_values(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.m.run("attach", {"pack": "web", "folder": str(self.proj)}, dry=False)
        blobs = [json.dumps(self.m.state()), json.dumps(self.m.folder_view(self.m.load(), str(self.proj))),
                 json.dumps(self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=True))]
        for b in blobs:
            self.assertNotIn(SECRET, b)
        srv = {s["name"]: s for s in self.m.state()["mcp"]}
        self.assertEqual(srv["alpha-mcp"]["env_keys"], ["KEY"])
        self.assertEqual(srv["web-mcp"]["header_keys"], ["Authorization"])


class Toml(unittest.TestCase):
    def test_drop_tables_keeps_other_content(self):
        text = ('a = 1\n[mcp_servers."x-y"]\ncommand = "c"\n[mcp_servers."x-y".env]\nK = "v"\n'
                "[mcp_servers.other]\ncommand = \"o\"\n[projects.'d:\\p']\ntrust_level = \"trusted\"\n")
        out = tomllib.loads(mcp.drop_toml_tables(text, "x-y"))
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["mcp_servers"], {"other": {"command": "o"}})
        self.assertIn("projects", out)

    def test_remove_rolls_back_when_entry_survives(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            folder = Path(t)
            (folder / ".codex").mkdir()
            original = '[mcp_servers]\nalpha.command = "dotted form"\n'
            (folder / ".codex/config.toml").write_text(original)
            with self.assertRaises(mcp.McpError):
                mcp.CodexFile(folder / "g.toml").remove(folder, "alpha")
            self.assertEqual((folder / ".codex/config.toml").read_text(), original)


if __name__ == "__main__":
    unittest.main()
