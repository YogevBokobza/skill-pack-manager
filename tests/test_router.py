"""Pack routers: generation, the two modes, staying in step with the pack."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from spm import router
from spm.core import HIDDEN_DIR, OpError, SKILL_DIRS, UnsafeRemoval, apply_plan, is_link
from spm.server import App
from tests.test_core import Env


class Template(unittest.TestCase):
    def test_description_fits_and_names_every_skill(self):
        skills = [{"name": f"skill-{i}", "description": "x" * 300 + ". More text."} for i in range(30)]
        d = router.template("big", skills)
        self.assertLessEqual(len(d["description"]), router.MAX_DESCRIPTION)
        for i in range(12):
            self.assertIn(f"skill-{i}", d["description"])
        self.assertEqual(len(d["routing"]), 30)

    def test_render_modes(self):
        r = {"a": "when A"}
        inst = router.render("web", "instead", "Routes web work.", r, ["a"])
        self.assertIn("name: web-pack", inst)
        self.assertIn("`.skill-packs/a/SKILL.md`", inst)
        along = router.render("web", "alongside", "Routes web work.", r, ["a"])
        self.assertIn("the `a` skill", along)
        self.assertNotIn(".skill-packs", along)

    def test_ai_answer_is_validated(self):
        skills = [{"name": "a", "description": "does a"}, {"name": "b", "description": "does b"}]
        raw = {"description": "Routes x. " * 200, "routing": {"a": "use for A", "zzz": "unknown", "b": ""}}
        v = router._validate(raw, "p", skills)
        self.assertLessEqual(len(v["description"]), router.MAX_DESCRIPTION)
        self.assertEqual(v["routing"]["a"], "use for A")
        self.assertEqual(v["routing"]["b"], "does b")  # empty answer -> template line
        self.assertNotIn("zzz", v["routing"])

    def test_cli_writers(self):
        skills = [{"name": "a", "description": "does a"}]
        answer = {"description": "Routes p tasks: a.", "routing": {"a": "for A"}}
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps({"result": "```json\n" + json.dumps(answer) + "\n```"}), stderr="")
        with mock.patch.object(router, "_run", return_value=fake) as run:
            d = router.write_with_ai("claude-cli", "p", skills, claude_cli=Path("claude"))
        cmd = run.call_args[0][0]
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")  # no tools for the writer
        self.assertEqual(d, answer)
        with self.assertRaises(router.RouterError):
            router.write_with_ai("anthropic", "p", skills, claude_cli=Path("claude"))  # no key


class PackRouter(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.m = self.app.model
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)

    def links(self, rel):
        d = self.proj / rel
        return sorted(e.name for e in d.iterdir() if is_link(e)) if d.is_dir() else []

    def test_instead_mode(self):
        pre = self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=True)
        self.assertEqual(pre["plans"][0]["missing"], [])  # the router counts as present in the preview
        self.assertFalse(self.m.routers.path("one").exists())
        self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=False)
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["one-pack"])
        self.assertEqual(self.links(HIDDEN_DIR), ["alpha", "beta"])
        text = (self.proj / ".claude/skills/one-pack/SKILL.md").read_text()
        self.assertIn("`.skill-packs/alpha/SKILL.md`", text)
        self.assertEqual((self.proj / HIDDEN_DIR / "alpha/references/deep.md").read_text(), "content that must survive")
        v = self.m.folder_view(self.m.load(), str(self.proj))
        st = {s["skill"]: s["status"] for s in v["skills"]}
        self.assertEqual(st, {"one-pack": "ok", "alpha": "routed", "beta": "routed"})
        self.assertTrue(v["in_sync"])
        self.assertIn(f"/{HIDDEN_DIR}/alpha", self.excludes())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj, capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "")

    def test_switch_to_alongside_and_remove(self):
        self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=False)
        self.m.run("router_set", {"pack": "one", "mode": "alongside"}, dry=False)
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["alpha", "beta", "one-pack"])
        self.assertEqual(self.links(HIDDEN_DIR), [])
        self.assertIn("the `alpha` skill", self.m.routers.read("one"))
        self.m.run("router_remove", {"pack": "one"}, dry=False)
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["alpha", "beta"])
        self.assertFalse(self.m.routers.path("one").exists())
        self.store_intact()

    def test_router_follows_pack_changes(self):
        self.m.run("router_set", {"pack": "one"}, dry=False)
        self.m.run("pack_add_skill", {"pack": "one", "skill": "delta"}, dry=False)
        self.assertIn("delta", self.links(HIDDEN_DIR))
        self.assertIn("`.skill-packs/delta/SKILL.md`", self.m.routers.read("one"))
        self.assertIn("delta", self.m.routers.meta("one")["description"])  # template: fully regenerated
        self.m.run("pack_remove_skill", {"pack": "one", "skill": "alpha"}, dry=False)
        self.assertNotIn("alpha", self.links(HIDDEN_DIR))
        self.assertNotIn("| alpha |", self.m.routers.read("one"))
        self.store_intact()

    def test_edited_router_keeps_text_and_turns_stale(self):
        self.m.run("router_set", {"pack": "one", "description": "My own words about alpha and beta.",
                                  "routing": {"alpha": "my line"}, "writer": "template", "edited": True}, dry=False)
        self.m.run("pack_add_skill", {"pack": "one", "skill": "delta"}, dry=False)
        meta = self.m.routers.meta("one")
        self.assertEqual(meta["description"], "My own words about alpha and beta.")
        self.assertEqual(meta["routing"]["alpha"], "my line")
        self.assertTrue(meta["stale"])
        info = {p["name"]: p for p in self.m.state()["packs"]}["one"]["router"]
        self.assertTrue(info["stale"] and info["edited"])

    def test_name_collision_and_empty_pack(self):
        (self.store / "one-pack").mkdir()
        (self.store / "one-pack/SKILL.md").write_text("---\nname: one-pack\n---\n")
        with self.assertRaises(OpError):
            self.m.run("router_set", {"pack": "one"})
        self.m.run("create_pack", {"pack": "empty"}, dry=False)
        with self.assertRaises(OpError):
            self.m.run("router_set", {"pack": "empty"})

    def test_links_only_into_known_roots(self):
        outside = self.other / "evil"
        outside.mkdir()
        (outside / "SKILL.md").write_text("x")
        p = {"folder": str(self.proj), "add": [[".claude/skills", "evil", str(outside)]], "remove": []}
        with self.assertRaises(UnsafeRemoval):
            apply_plan(p, self.m.stores)

    def test_draft_endpoint_and_secrets_stay_private(self):
        d = self.app.post("/api/router/draft", {"pack": "one", "writer": "template"})
        self.assertEqual(d["skills"], ["alpha", "beta"])
        self.app.post("/api/secret", {"name": "anthropic", "value": "sk-ant-secret-123"})
        st = json.dumps(self.app.state())
        self.assertNotIn("sk-ant-secret-123", st)
        self.assertTrue(self.app.state()["settings"]["ai"]["keys"]["anthropic"])


if __name__ == "__main__":
    unittest.main()
