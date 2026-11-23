"""Standalone mode: skills from git repos, global links, the MCP catalog."""
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from spm import mcp, repo
from spm.core import is_link
from spm.sources import LocalSource, SourceError


SECRET = "token-never-in-api"


class FakeClaudeUser(mcp.ClaudeUser):
    """Edits top-level mcpServers the way `claude mcp add-json/remove --scope user` does."""

    def run_cli(self, folder, args):
        assert args[2:4] == ["--scope", "user"], args
        j = json.loads(self.claude_json.read_text()) if self.claude_json.exists() else {}
        servers = j.setdefault("mcpServers", {})
        if args[1] == "add-json":
            assert args[4] not in servers, "claude refuses an existing name"
            servers[args[4]] = json.loads(args[5])
        else:
            servers.pop(args[4], None)
        self.claude_json.write_text(json.dumps(j))
        self._cache = None


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.t = t
        # an upstream repo with two skills and release tags
        self.up = t / "upstream"
        (self.up / "skills/writer/references").mkdir(parents=True)
        (self.up / "skills/linter").mkdir(parents=True)
        self.write_skill("writer", "v1 text")
        (self.up / "skills/writer/references/style.md").write_text("style")
        (self.up / "skills/linter/SKILL.md").write_text("---\nname: linter\ndescription: lints\n---\n")
        git("init", "-q", "-b", "main", cwd=self.up)
        git("add", ".", cwd=self.up)
        git("commit", "-qm", "v1", cwd=self.up)
        for tag in ("v1.0.0", "v1.9.0", "v1.10.0"):
            git("tag", tag, cwd=self.up)
        git("tag", "-a", "v2.0.0-rc1", "-m", "rc", cwd=self.up)
        self.write_skill("writer", "v2 text")
        git("commit", "-qam", "v2", cwd=self.up)
        git("tag", "-a", "v2.0.0", "-m", "release", cwd=self.up)
        self.url = self.up.resolve().as_uri()

        self.home = t / "home"
        self.claude_json = t / "claude.json"
        gm = mcp.global_adapters(self.claude_json, t / "none", self.home / ".codex/config.toml",
                                 self.home / ".config/opencode")
        gm["claude"] = FakeClaudeUser(self.claude_json, t / "none")
        self.src = LocalSource(t / "store", global_skill_dirs={
            "claude": self.home / ".claude/skills", "opencode": self.home / ".config/opencode/skills",
            "codex": self.home / ".codex/skills"}, global_mcp=gm)
        self.ws = repo.Workspace(t / "tmp")

    def write_skill(self, name, body):
        (self.up / f"skills/{name}/SKILL.md").write_text(f"---\nname: {name}\ndescription: >\n  writes {body}\n---\n{body}\n")

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp.name):
            for d in dirs + files:
                p = Path(root) / d
                if is_link(p):
                    os.rmdir(p) if os.path.isjunction(p) else os.unlink(p)
        self.tmp.cleanup()

    def install(self, ref, paths=("skills/writer",), replace=False):
        info = self.ws.clone(self.url, ref)
        return self.src.install(self.ws, info["id"], list(paths), replace)


class Refs(RepoFixture):
    def test_default_branch_first_then_tags_newest_first(self):
        r = repo.refs(self.url)
        self.assertEqual([x["ref"] for x in r["refs"]], ["main", "v2.0.0", "v2.0.0-rc1", "v1.10.0", "v1.9.0", "v1.0.0"])
        self.assertTrue(r["refs"][0]["default"])

    def test_inspect_lists_every_skill_with_preview(self):
        info = self.ws.clone(self.url, "v1.0.0")
        self.assertEqual({s["path"]: s["name"] for s in info["skills"]}, {"skills/writer": "writer", "skills/linter": "linter"})
        writer = next(s for s in info["skills"] if s["dir"] == "writer")
        self.assertEqual(writer["description"], "writes v1 text")
        self.assertIn("v1 text", writer["preview"])

    def test_web_url(self):
        w = repo.web_url
        self.assertEqual(w("https://github.com/o/r.git", "v1.2.0", "skills/x"), "https://github.com/o/r/tree/v1.2.0/skills/x")
        self.assertEqual(w("git@github.com:o/r.git", "main", "."), "https://github.com/o/r/tree/main")
        self.assertEqual(w("ssh://git@github.com/o/r", "main", None), "https://github.com/o/r/tree/main")
        self.assertEqual(w("https://gitlab.com/g/r", "dev", "a"), "https://gitlab.com/g/r/-/tree/dev/a")
        self.assertEqual(w("https://git.example.com/r.git", "main", "a"), "https://git.example.com/r")
        self.assertIsNone(w("file:///tmp/r", "main", "a"))
        self.assertIsNone(w(None))

    def test_ref_kind_is_recorded(self):
        info = self.ws.clone(self.url, "v1.0.0")
        self.assertEqual(info["kind"], "tag")
        self.assertEqual(self.ws.clone(self.url, "main")["kind"], "branch")

    def test_rejects_dangerous_urls_and_refs(self):
        for bad in ("ext::sh -c touch% /tmp/x", "-uhttps://x", "https://ok/x y", ""):
            with self.assertRaises(repo.RepoError):
                repo.check_url(bad)
        with self.assertRaises(repo.RepoError):
            self.ws.clone(self.url, "--upload-pack=touch x")


class Install(RepoFixture):
    def test_install_update_and_check(self):
        self.install("v1.0.0")
        s = self.src.skills()["writer"]
        self.assertEqual((s["source"]["ref"], s["source"]["kind"]), ("v1.0.0", "tag"))
        self.assertIn("v1 text", (self.src.store / "writer/SKILL.md").read_text())
        self.assertTrue((self.src.store / "writer/references/style.md").exists())
        self.assertFalse((self.src.store / "writer/.git").exists())
        upd = self.src.check_updates()["writer"]
        self.assertEqual(upd["newer_tags"], ["v2.0.0", "v2.0.0-rc1", "v1.10.0", "v1.9.0"])
        self.src.update(self.ws, "writer", "v2.0.0")
        self.assertIn("v2 text", (self.src.store / "writer/SKILL.md").read_text())
        self.assertEqual(self.src.skills()["writer"]["source"]["ref"], "v2.0.0")
        self.src.update(self.ws, "writer", "main")
        self.assertEqual(self.src.skills()["writer"]["source"]["kind"], "branch")
        self.assertIn("up to date", self.src.update(self.ws, "writer"))

    def test_update_keeps_the_folder_so_links_survive(self):
        self.install("v1.0.0")
        self.src.set_skill_global("writer", "claude", True)
        link = self.home / ".claude/skills/writer"
        self.src.update(self.ws, "writer", "main")
        self.assertIn("v2 text", (link / "SKILL.md").read_text())

    def test_same_name_from_other_source_needs_replace(self):
        (self.src.store / "writer").mkdir()
        (self.src.store / "writer/SKILL.md").write_text("---\nname: writer\n---\nmine")
        with self.assertRaises(SourceError):
            self.install("main")
        self.install("main", replace=True)
        self.assertIn("v2 text", (self.src.store / "writer/SKILL.md").read_text())

    def test_import_local_folder(self):
        self.src.import_folder(str(self.up / "skills/linter"))
        self.assertEqual(self.src.skills()["linter"]["source"], {"local": str(self.up / "skills/linter")})


class GlobalSkills(RepoFixture):
    def test_enable_disable_and_conflict(self):
        self.install("main")
        self.src.set_skill_global("writer", "claude", True)
        self.src.set_skill_global("writer", "codex", True)
        self.assertEqual(self.src.skills()["writer"]["global"], ["claude", "codex"])
        self.src.set_skill_global("writer", "claude", False)
        self.assertFalse(os.path.lexists(self.home / ".claude/skills/writer"))
        self.assertTrue((self.src.store / "writer/SKILL.md").exists())
        own = self.home / ".config/opencode/skills/writer"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("user's own")
        with self.assertRaises(SourceError):
            self.src.set_skill_global("writer", "opencode", True)
        self.assertEqual((own / "SKILL.md").read_text(), "user's own")

    def test_remove_skill_guards(self):
        self.install("main")
        with self.assertRaises(SourceError):
            self.src.remove_skill("writer", {"packs": {"p": {"skills": ["writer"], "paths": []}}})
        self.src.set_skill_global("writer", "claude", True)
        with self.assertRaises(SourceError):
            self.src.remove_skill("writer", {"packs": {}})
        self.src.set_skill_global("writer", "claude", False)
        self.src.remove_skill("writer", {"packs": {}})
        self.assertNotIn("writer", self.src.skills())


class McpCatalog(RepoFixture):
    def test_import_formats(self):
        claude = LocalSource.parse_import(json.dumps({"mcpServers": {"a": {"command": "npx", "args": ["x"], "env": {"K": "v"}}}}))
        self.assertEqual(claude["a"], {"type": "stdio", "command": "npx", "args": ["x"], "env": {"K": "v"}})
        oc = LocalSource.parse_import(json.dumps({"mcp": {"b": {"type": "local", "command": ["node", "s.js"], "environment": {"E": "1"}}}}))
        self.assertEqual(oc["b"], {"type": "stdio", "command": "node", "args": ["s.js"], "env": {"E": "1"}})
        web = LocalSource.parse_import(json.dumps({"c": {"type": "sse", "url": "https://x/sse"}}))
        self.assertEqual(web["c"], {"type": "sse", "url": "https://x/sse"})
        for bad in ("[]", "{}", '{"d": {"url": "ftp://x"}}', '{"bad name": {"command": "x"}}', "nope"):
            with self.assertRaises(SourceError):
                LocalSource.parse_import(bad)

    def test_edit_keeps_secrets_the_ui_never_saw(self):
        self.src.save_mcp("srv", {"command": "node", "args": ["a"], "env": {"TOKEN": SECRET, "OLD": "x"}})
        self.src.save_mcp("srv", {"command": "node", "args": ["b"], "env": {"NEW": "1"}},
                          keep_env=["TOKEN"], new=False)
        cfg = self.src.mcp_catalog().config("srv")
        self.assertEqual(cfg["env"], {"NEW": "1", "TOKEN": SECRET})
        self.assertEqual(cfg["args"], ["b"])
        self.assertNotIn(SECRET, json.dumps(self.src.mcp_catalog().public("srv")))

    def test_global_mcp_on_off_in_all_clients(self):
        self.src.save_mcp("srv", {"command": "node", "args": ["a"], "env": {"TOKEN": SECRET}})
        for c in mcp.CLIENTS:
            self.src.set_mcp_global("srv", c, True)
        self.assertEqual(self.src.mcp_catalog().servers["srv"]["global"], ["claude", "opencode", "codex"])
        self.assertEqual(json.loads(self.claude_json.read_text())["mcpServers"]["srv"]["command"], "node")
        oc = json.loads((self.home / ".config/opencode/opencode.json").read_text())
        self.assertEqual(oc["mcp"]["srv"]["command"], ["node", "a"])
        cx = tomllib.loads((self.home / ".codex/config.toml").read_text())
        self.assertEqual(cx["mcp_servers"]["srv"]["env"], {"TOKEN": SECRET})
        self.src.set_mcp_global("srv", "claude", True)  # re-enable refreshes, no error
        with self.assertRaises(SourceError):
            self.src.delete_mcp("srv", {"packs": {}})
        for c in mcp.CLIENTS:
            self.src.set_mcp_global("srv", c, False)
        self.assertEqual(self.src.mcp_catalog().servers["srv"]["global"], [])
        self.src.delete_mcp("srv", {"packs": {}})

    def test_opencode_jsonc_is_never_rewritten(self):
        d = self.home / ".config/opencode"
        d.mkdir(parents=True)
        (d / "opencode.jsonc").write_text('{ // mine\n "theme": "x" }')
        self.src.save_mcp("srv", {"command": "x"})
        with self.assertRaises(mcp.McpError):
            self.src.set_mcp_global("srv", "opencode", True)
        self.assertEqual((d / "opencode.jsonc").read_text(), '{ // mine\n "theme": "x" }')
        self.assertFalse((d / "opencode.json").exists())


if __name__ == "__main__":
    unittest.main()
