"""Pack routers: generation, layout (index skill with references/), modes,
staying in step with the pack and the store."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from spm import router
from spm.core import HIDDEN_DIR, OpError, SKILL_DIRS, UnsafeRemoval, apply_plan, is_link, make_junction
from spm.server import App
from tests.test_core import Env


class Template(unittest.TestCase):
    def test_description_pattern_and_limit(self):
        d = router.template("web", [{"name": "webapp-testing"}, {"name": "frontend-design"}])["description"]
        self.assertTrue(d.startswith("Index of the web pack's skills, with every skill's description and file."))
        self.assertIn("Make sure to use this skill for any web task, from frontend design to webapp testing", d)
        self.assertTrue(d.endswith("including advice questions, before writing code or answering."))
        many = [{"name": f"very-long-skill-name-number-{i}"} for i in range(60)]
        big = router.template("big", many)["description"]
        self.assertLessEqual(len(big), router.MAX_DESCRIPTION)
        self.assertIn("to more,", big)

    def test_render_index(self):
        entries = [("zeta", "Does zeta"), ("alpha", "Does alpha.")]
        inst = router.render("web", "instead", "Index of the web pack.", entries)
        self.assertIn("name: web-pack", inst)
        self.assertIn("this index is the only way to reach them", inst)
        self.assertIn("only `web-pack` appears in your list of skills", inst)
        self.assertIn("## Index (2 skills, A to Z)", inst)
        self.assertLess(inst.index("**alpha**"), inst.index("**zeta**"))  # A to Z
        self.assertIn("- **alpha**: Does alpha. File: `references/alpha/alpha.md`", inst)
        self.assertIn("- **zeta**: Does zeta. File: `references/zeta/zeta.md`", inst)
        self.assertIn("`<SKILL_DIR>`", inst)
        along = router.render("web", "alongside", "Index of the web pack.", entries)
        self.assertIn("also listed on their own", along)
        self.assertNotIn("only `web-pack` appears", along)

    def test_ai_answer_is_validated(self):
        skills = [{"name": "a", "description": "does a"}]
        v = router._validate({"description": "Index of p. " * 200, "routing": {"a": "ignored"}}, "p", skills)
        self.assertLessEqual(len(v["description"]), router.MAX_DESCRIPTION)
        self.assertEqual(v["routing"], {})  # the index keeps each skill's own description
        self.assertTrue(router._validate({}, "p", skills)["description"].startswith("Index of the p pack"))

    def test_cli_writers(self):
        skills = [{"name": "a", "description": "does a"}]
        answer = {"description": "Index of the p pack's skills. Make sure to use this skill for any p task."}
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps({"result": "```json\n" + json.dumps(answer) + "\n```"}), stderr="")
        with mock.patch.object(router, "_run", return_value=fake) as run:
            d = router.write_with_ai("claude-cli", "p", skills, claude_cli=Path("claude"))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")  # no tools for the writer
        self.assertEqual(d, {"description": answer["description"], "routing": {}})
        with self.assertRaises(router.RouterError):
            router.write_with_ai("anthropic", "p", skills, claude_cli=Path("claude"))  # no key


class PackRouter(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.m = self.app.model
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.rdir = self.m.routers.path("one")

    def links(self, rel):
        d = self.proj / rel
        return sorted(e.name for e in d.iterdir() if is_link(e)) if d.is_dir() else []

    def test_instead_mode_layout(self):
        pre = self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=True)
        self.assertEqual(pre["plans"][0]["missing"], [])  # the router counts as present in the preview
        self.assertFalse(self.rdir.exists())
        self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=False)
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["one-pack"])
        self.assertFalse((self.proj / HIDDEN_DIR).exists())
        # the skills live inside the router, reachable through the project's link
        via = self.proj / ".claude/skills/one-pack/references"
        self.assertEqual((via / "alpha/alpha.md").read_text(), (self.store / "alpha/SKILL.md").read_text())
        self.assertEqual((via / "alpha/references/deep.md").read_text(), "content that must survive")
        self.assertTrue(is_link(self.rdir / "references/alpha/references"))
        # no SKILL.md below the router's own, so recursive scanners list one skill only
        self.assertEqual([p.relative_to(self.rdir).as_posix() for p in self.rdir.rglob("SKILL.md")], ["SKILL.md"])
        text = (self.proj / ".claude/skills/one-pack/SKILL.md").read_text()
        self.assertIn("- **alpha**: alpha skill. File: `references/alpha/alpha.md`", text)
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual({s["skill"]: s["status"] for s in v["skills"]},
                         {"one-pack": "ok", "alpha": "routed", "beta": "routed"})
        self.assertTrue(v["in_sync"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj, capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "")

    def test_removing_the_router_never_deletes_skill_content(self):
        self.m.run("router_set", {"pack": "one"}, dry=False)
        self.m.run("router_remove", {"pack": "one"}, dry=False)
        self.assertFalse(self.rdir.exists())
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["alpha", "beta"])
        self.store_intact()

    def test_switch_to_alongside(self):
        self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=False)
        self.m.run("router_set", {"pack": "one", "mode": "alongside"}, dry=False)
        for rel in SKILL_DIRS:
            self.assertEqual(self.links(rel), ["alpha", "beta", "one-pack"])
        self.assertIn("also listed on their own", self.m.routers.read("one"))

    def test_router_follows_pack_and_store(self):
        self.m.run("router_set", {"pack": "one"}, dry=False)
        self.m.run("pack_add_skill", {"pack": "one", "skill": "delta"}, dry=False)
        self.assertTrue((self.rdir / "references/delta/delta.md").exists())
        self.assertIn("**delta**", self.m.routers.read("one"))
        self.assertIn("delta", self.m.routers.meta("one")["description"])  # template: regenerated
        self.m.run("pack_remove_skill", {"pack": "one", "skill": "alpha"}, dry=False)
        self.assertFalse((self.rdir / "references/alpha").exists())
        self.assertNotIn("**alpha**", self.m.routers.read("one"))
        self.store_intact()
        # a skill updated in the store reaches the router on the next refresh
        (self.store / "beta/SKILL.md").write_text("---\nname: beta\ndescription: beta, now better\n---\nnew body\n")
        self.m.state()
        self.assertIn("new body", (self.rdir / "references/beta/beta.md").read_text())
        self.assertIn("- **beta**: beta, now better.", self.m.routers.read("one"))

    def test_overrides_and_stale(self):
        self.m.run("router_set", {"pack": "one", "description": "Index of my own words.",
                                  "routing": {"alpha": "my line for alpha", "beta": "beta skill"},
                                  "writer": "template", "edited": True}, dry=False)
        meta = self.m.routers.meta("one")
        self.assertEqual(meta["routing"], {"alpha": "my line for alpha"})  # equal to the description: dropped
        self.assertIn("- **alpha**: my line for alpha.", self.m.routers.read("one"))
        self.m.run("pack_add_skill", {"pack": "one", "skill": "delta"}, dry=False)
        meta = self.m.routers.meta("one")
        self.assertEqual(meta["description"], "Index of my own words.")
        self.assertTrue(meta["stale"])

    def test_old_skill_packs_links_are_cleaned_up(self):
        (self.proj / HIDDEN_DIR).mkdir()
        make_junction(self.store / "alpha", self.proj / HIDDEN_DIR / "alpha")
        self.m.run("sync", {"folder": str(self.proj)}, dry=False)
        self.assertEqual(self.links(HIDDEN_DIR), [])
        self.store_intact()

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
        self.assertEqual(d["descriptions"]["alpha"], "alpha skill")
        self.app.post("/api/secret", {"name": "anthropic", "value": "sk-ant-secret-123"})
        st = json.dumps(self.app.state())
        self.assertNotIn("sk-ant-secret-123", st)
        self.assertTrue(self.app.state()["settings"]["ai"]["keys"]["anthropic"])


if __name__ == "__main__":
    unittest.main()
