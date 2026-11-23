import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from spm import core
from spm.core import Config, Model, OpError, UnsafeRemoval


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.store = t / "store"
        for s in ("alpha", "beta", "gamma", "delta"):
            (self.store / s / "references").mkdir(parents=True)
            (self.store / s / "SKILL.md").write_text(f"---\nname: {s}\ndescription: {s} skill\n---\nbody\n")
            (self.store / s / "references" / "deep.md").write_text("content that must survive")
        self.proj = t / "proj"
        self.proj.mkdir()
        git("init", "-q", cwd=self.proj)
        self.other = t / "other"
        self.other.mkdir()
        self.packs = t / "packs.json"
        self.packs.write_text(json.dumps({"packs": {
            "one": {"skills": ["alpha", "beta"], "mcp": [], "paths": []},
            "two": {"skills": ["beta", "gamma"], "mcp": [], "paths": []},
        }}))
        self.home = t / "home"
        self.cfg = Config(store=self.store, packs=self.packs, db=t / "none.db",
                          data_dir=t / "data", scan_roots=[t],
                          claude_json=t / "claude.json", claude_cli=t / "no-claude.exe",
                          codex_global=t / "codex-global.toml", opencode_dir=t / "home/.config/opencode",
                          home=self.home)
        self.m = Model(self.cfg)

    def tearDown(self):
        # remove junctions first so cleanup never recurses into the store
        for root, dirs, _ in os.walk(self.tmp.name):
            for d in dirs:
                p = Path(root) / d
                if core.is_link(p):
                    os.rmdir(p) if os.path.isjunction(p) else os.unlink(p)
        self.tmp.cleanup()

    def store_intact(self):
        for s in ("alpha", "beta", "gamma", "delta"):
            self.assertEqual((self.store / s / "references" / "deep.md").read_text(), "content that must survive")

    def linked(self, folder, skill):
        return [rel for rel in core.SKILL_DIRS if core.is_link(folder / rel / skill)]

    def excludes(self, folder=None):
        return (folder or self.proj).joinpath(".git/info/exclude").read_text()


class SafeUnlink(Env):
    def test_refuses_real_folder_and_keeps_its_content(self):
        real = self.proj / ".claude/skills/mine"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("project skill")
        with self.assertRaises(UnsafeRemoval):
            core.safe_unlink(real, self.store)
        self.assertEqual((real / "SKILL.md").read_text(), "project skill")

    def test_refuses_junction_outside_store(self):
        elsewhere = self.other / "thing"
        elsewhere.mkdir()
        (elsewhere / "f").write_text("x")
        link = self.proj / "link"
        core.make_junction(elsewhere, link)
        with self.assertRaises(UnsafeRemoval):
            core.safe_unlink(link, self.store)
        self.assertTrue((elsewhere / "f").exists())

    def test_removes_only_the_link(self):
        link = self.proj / "alpha"
        core.make_junction(self.store / "alpha", link)
        core.safe_unlink(link, self.store)
        self.assertFalse(os.path.lexists(link))
        self.store_intact()


class Operations(Env):
    def test_dry_run_changes_nothing(self):
        before = self.packs.read_text()
        res = self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=True)
        self.assertEqual(len(res["plans"][0]["add"]), 6)
        self.assertEqual(self.packs.read_text(), before)
        self.assertFalse((self.proj / ".claude").exists())

    def test_attach_links_three_dirs_and_writes_excludes(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        self.assertEqual(len(self.linked(self.proj, "beta")), 3)
        self.assertIn("/.claude/skills/alpha", self.excludes())
        self.assertIn(str(self.proj), json.loads(self.packs.read_text())["packs"]["one"]["paths"])
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj, capture_output=True, text=True)
        self.assertEqual(st.stdout.strip(), "")

    def test_detach_keeps_skill_still_needed_by_other_pack(self):
        for p in ("one", "two"):
            self.m.run("attach", {"pack": p, "folder": str(self.proj)}, dry=False)
        self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(self.linked(self.proj, "alpha"), [])
        self.assertEqual(len(self.linked(self.proj, "beta")), 3)
        self.assertEqual(len(self.linked(self.proj, "gamma")), 3)
        self.assertNotIn("/.claude/skills/alpha", self.excludes())
        self.store_intact()

    def test_detach_never_touches_project_own_skills(self):
        own = self.proj / ".claude/skills/alpha"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("own version")
        res = self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertIn([".claude/skills", "alpha", "own"], res["plans"][0]["conflicts"])
        self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual((own / "SKILL.md").read_text(), "own version")
        self.store_intact()

    def test_removing_skill_from_pack_unlinks_it_everywhere(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.m.run("attach", {"pack": "one", "folder": str(self.other)}, dry=False)
        res = self.m.run("pack_remove_skill", {"pack": "one", "skill": "alpha"}, dry=False)
        self.assertEqual(len(res["plans"]), 2)
        self.assertEqual(self.linked(self.proj, "alpha"), [])
        self.assertEqual(self.linked(self.other, "alpha"), [])
        self.assertEqual(len(self.linked(self.other, "beta")), 3)
        self.store_intact()

    def test_bulk_add_and_remove_skills(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        pre = self.m.run("pack_add_skills", {"pack": "one", "skills": ["alpha", "gamma", "delta", "gamma"]}, dry=True)
        self.assertEqual(sorted({x[1] for x in pre["plans"][0]["add"]}), ["delta", "gamma"])  # alpha already in
        self.m.run("pack_add_skills", {"pack": "one", "skills": ["alpha", "gamma", "delta"]}, dry=False)
        self.assertEqual(json.loads(self.packs.read_text())["packs"]["one"]["skills"], ["alpha", "beta", "gamma", "delta"])
        self.assertEqual(len(self.linked(self.proj, "delta")), 3)
        self.m.run("pack_remove_skills", {"pack": "one", "skills": ["beta", "delta", "not-in-pack"]}, dry=False)
        self.assertEqual(json.loads(self.packs.read_text())["packs"]["one"]["skills"], ["alpha", "gamma"])
        self.assertEqual(self.linked(self.proj, "delta"), [])
        self.store_intact()
        with self.assertRaises(OpError):
            self.m.run("pack_add_skills", {"pack": "one", "skills": ["alpha"]})  # nothing new
        with self.assertRaises(OpError):
            self.m.run("pack_remove_skills", {"pack": "one", "skills": ["delta"]})  # none of them in the pack
        with self.assertRaises(OpError):
            self.m.run("pack_add_skills", {"pack": "one", "skills": ["nope"]})
        with self.assertRaises(OpError):
            self.m.run("pack_add_skills", {"pack": "one", "skills": []})

    def test_extra_skill_add_and_remove(self):
        self.m.run("extra_add", {"folder": str(self.proj), "skill": "delta"}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "delta")), 3)
        data = json.loads(self.packs.read_text())
        self.assertEqual(list(data["folders"].values())[0]["skills"], ["delta"])
        self.m.run("extra_remove", {"folder": str(self.proj), "skill": "delta"}, dry=False)
        self.assertEqual(self.linked(self.proj, "delta"), [])
        self.assertNotIn("folders", json.loads(self.packs.read_text()))

    def test_extra_that_is_also_in_pack_survives_pack_detach(self):
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.m.run("extra_add", {"folder": str(self.proj), "skill": "alpha"}, dry=False)
        self.m.run("detach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        self.assertEqual(self.linked(self.proj, "beta"), [])

    def test_delete_pack_unlinks_its_folders(self):
        self.m.run("attach", {"pack": "two", "folder": str(self.proj)}, dry=False)
        self.m.run("delete_pack", {"pack": "two"}, dry=False)
        self.assertEqual(self.linked(self.proj, "gamma"), [])
        self.assertNotIn("two", json.loads(self.packs.read_text())["packs"])
        self.store_intact()

    def test_sync_removes_stray_managed_link(self):
        (self.proj / ".claude/skills").mkdir(parents=True)
        core.make_junction(self.store / "delta", self.proj / ".claude/skills/delta")
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual(v["skills"][0]["status"], "stray")
        self.m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertEqual(self.linked(self.proj, "delta"), [])
        self.store_intact()

    def test_validation(self):
        with self.assertRaises(OpError):
            self.m.run("create_pack", {"pack": "Bad Name"})
        with self.assertRaises(OpError):
            self.m.run("pack_add_skill", {"pack": "one", "skill": "..\\x"})
        with self.assertRaises(OpError):
            self.m.run("attach", {"pack": "one", "folder": str(self.proj / "nope")})

    def test_tilde_paths_and_missing_file(self):
        self.packs.write_text(json.dumps({"packs": {"p": {"skills": [], "paths": ["~/x"]}},
                                          "folders": {"~/y": {"skills": ["alpha"]}}}))
        d = self.m.load()
        self.assertEqual(d["packs"]["p"]["paths"], [os.path.expanduser("~/x")])
        self.assertIn(os.path.expanduser("~/y"), d["folders"])
        self.packs.unlink()
        self.assertEqual(self.m.load(), {"packs": {}})
        self.m.run("create_pack", {"pack": "first"}, dry=False)  # creates the file
        self.assertIn("first", json.loads(self.packs.read_text())["packs"])

    def test_example_file_is_valid(self):
        from spm.osinfo import APP_ROOT
        data = json.loads((APP_ROOT / "packs.example.json").read_text())
        self.assertTrue(data["packs"])

    def test_save_keeps_backup(self):
        self.m.run("create_pack", {"pack": "new"}, dry=False)
        self.assertEqual(len(list((self.cfg.data_dir / "backups").glob("packs-*.json"))), 1)

    def test_worktree_excludes_go_to_common_dir(self):
        (self.proj / "f").write_text("x")
        git("add", ".", cwd=self.proj)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=self.proj)
        wt = Path(self.tmp.name) / "wt"
        git("worktree", "add", "-q", str(wt), cwd=self.proj)
        self.m.run("attach", {"pack": "one", "folder": str(wt)}, dry=False)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True)
        self.assertEqual(st.stdout.strip(), "")


class Views(Env):
    def test_scan_finds_unity_project_without_pack(self):
        u = self.other / "game"
        (u / "ProjectSettings").mkdir(parents=True)
        (u / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000")
        found = {Path(x["folder"]).name: x for x in self.m.scan()}
        self.assertEqual(found["game"]["kind"], "unity")
        self.assertEqual(found["game"]["packs"], [])

    def test_scan_maps_unity_subfolder_to_repo_root(self):
        repo = self.other / "mono"
        (repo / "client/ProjectSettings").mkdir(parents=True)
        (repo / "client/ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000")
        git("init", "-q", cwd=repo)
        found = {Path(x["folder"]).name: x for x in self.m.scan()}
        self.assertIn("mono", found)
        self.assertEqual(found["mono"]["subdir"], "client")
        self.assertNotIn("client", found)

    def test_state_lists_store_skills(self):
        st = self.m.state()
        self.assertEqual([s["dir"] for s in st["skills"]], ["alpha", "beta", "delta", "gamma"])
        self.assertEqual(st["skills"][0]["description"], "alpha skill")

    def test_usage_counts_claude_skill_calls(self):
        log = self.home / ".claude/projects/p/s.jsonl"
        log.parent.mkdir(parents=True)
        lines = [{"cwd": "/work", "timestamp": "2026-01-02T00:00:00Z", "name": "Skill", "input": {"skill": "alpha"}},
                 {"timestamp": "2026-01-05T00:00:00Z", "name": "Skill", "input": {"skill": "plugin:beta"}}]
        log.write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
        u = self.m.usage()
        self.assertEqual(u["alpha"]["uses"], 1)
        self.assertEqual(u["alpha"]["where"], [["/work", 1]])
        self.assertEqual(u["beta"]["last"], "2026-01-05")
        self.assertEqual(self.m.cached_usage()["alpha"]["uses"], 1)


if __name__ == "__main__":
    unittest.main()
