"""Router tests: cases, the judge prompt, scoring, storage, endpoints, CLI."""
import io
import json
import re
import unittest
from unittest import mock

from spm import cli, evaluate, router
from spm.server import App
from tests.test_core import Env


def judge(plan):
    """A fake AI: plan maps request text -> (open, skill). Records the prompts."""
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        reqs = re.findall(r"^(\d+)\. (.+)$", prompt, re.M)
        return json.dumps({"answers": [{"id": int(i), "open": plan.get(r, (False, None))[0],
                                        "skill": plan.get(r, (False, None))[1]} for i, r in reqs]})
    return ask, prompts


class Cases(unittest.TestCase):
    def test_parse_and_format(self):
        c = evaluate.parse_cases("# comment\nalpha: fix the hero\nnone: rename a var\n\n", ["alpha", "beta"])
        self.assertEqual(c, [{"request": "fix the hero", "skill": "alpha"}, {"request": "rename a var", "skill": None}])
        self.assertEqual(evaluate.format_cases(c), "alpha: fix the hero\nnone: rename a var")
        for bad in ("", "gamma: x", "no colon here", "alpha:   "):
            with self.subTest(bad=bad), self.assertRaises(router.RouterError):
                evaluate.parse_cases(bad, ["alpha"])
        with self.assertRaises(router.RouterError):
            evaluate.parse_cases("\n".join(f"alpha: r{i}" for i in range(41)), ["alpha"])

    def test_written_cases_are_filtered(self):
        ans = {"cases": [{"request": "a real one", "skill": "alpha"}, {"request": "made up", "skill": "zzz"},
                         {"request": "off topic", "skill": "none"}, {"request": "", "skill": "alpha"}, "junk"]}
        cases = evaluate.write_cases(lambda p: "```json\n" + json.dumps(ans) + "\n```", "p",
                                     [{"name": "alpha", "description": "does a"}])
        self.assertEqual(cases, [{"request": "a real one", "skill": "alpha"}, {"request": "off topic", "skill": None}])


class Scoring(unittest.TestCase):
    ROUTER = router.render("web", "instead", "Index of the web pack.", [("alpha", "does a"), ("beta", "does b")])

    def test_scores_and_confusions(self):
        cases = [{"request": "do a", "skill": "alpha"}, {"request": "do b", "skill": "beta"},
                 {"request": "do b again", "skill": "beta"}, {"request": "unrelated", "skill": None},
                 {"request": "also unrelated", "skill": None}]
        ask, prompts = judge({"do a": (True, "alpha"), "do b": (True, "alpha"), "do b again": (False, None),
                              "unrelated": (False, None), "also unrelated": (True, "beta")})
        r = evaluate.run(ask, self.ROUTER, cases)
        self.assertIn("description: Index of the web pack.", prompts[0])
        self.assertIn("alpha: does a", prompts[0])  # the index, read from the router text
        s = r["summary"]
        self.assertEqual((s["ok"], s["cases"]), (2, 5))
        self.assertEqual((s["opened"], s["should_open"], s["picked"]), (2, 3, 1))
        self.assertEqual((s["stayed_out"], s["off_topic"]), (1, 2))
        self.assertEqual(s["missed"], ["do b again"])
        self.assertEqual(s["confusions"], {"beta": {"alpha": 1}})

    def test_missing_answers_count_as_wrong(self):
        r = evaluate.run(lambda p: '{"answers": []}', self.ROUTER, [{"request": "do a", "skill": "alpha"}])
        self.assertEqual(r["results"][0], {"request": "do a", "expected": "alpha", "opened": False, "chosen": None,
                                           "ok": False, "answered": False})

    def test_explicit_index_wins(self):
        ask, prompts = judge({})
        evaluate.run(ask, self.ROUTER, [{"request": "x", "skill": None}], [("alpha", "custom line")])
        self.assertIn("alpha: custom line", prompts[0])
        self.assertNotIn("beta: does b", prompts[0])


class Endpoints(Env):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.m = self.app.model
        self.m.run("router_set", {"pack": "one", "mode": "instead"}, dry=False)

    def test_run_saves_and_summarizes(self):
        ask, _ = judge({"use alpha": (True, "alpha"), "use beta": (True, "alpha")})
        with mock.patch.object(self.app, "asker", return_value=("claude-cli", ask)):
            r = self.app.router_test("test", {"pack": "one", "text": "alpha: use alpha\nbeta: use beta"})
        self.assertEqual((r["summary"]["ok"], r["writer"]), (1, "claude-cli"))
        last = self.app.router_test("test-cases", {"pack": "one", "saved": True})
        self.assertEqual(last["text"], "alpha: use alpha\nbeta: use beta")
        t = self.m.router_info(self.m.load(), "one")["test"]
        self.assertEqual((t["ok"], t["cases"]), (1, 2))

    def test_template_writer_cannot_judge(self):
        with self.assertRaises(router.RouterError):
            self.app.router_test("test", {"pack": "one", "text": "alpha: x"})  # default writer is the template

    def test_pack_without_router(self):
        from spm.core import OpError
        with self.assertRaises(OpError):
            self.app.router_test("test", {"pack": "two", "text": "beta: x"})

    def test_cli(self):
        ask, _ = judge({"use alpha": (True, "alpha")})
        f = self.proj.parent / "cases.txt"
        f.write_text("alpha: use alpha\nnone: something else\n")
        out = io.StringIO()
        with mock.patch.object(self.app, "asker", return_value=("codex-cli", ask)):
            code = cli.run(["router-test", "one", "--cases", str(f)], app=self.app, out=out)
        self.assertEqual(code, 0)
        self.assertIn("2/2 right", out.getvalue())
        bad, _ = judge({})
        with mock.patch.object(self.app, "asker", return_value=("codex-cli", bad)):
            self.assertEqual(cli.run(["router-test", "one"], app=self.app, out=io.StringIO()), 3)  # saved cases


class Ask(unittest.TestCase):
    def test_ask_refuses_the_template(self):
        from pathlib import Path
        with self.assertRaises(router.RouterError):
            router.ask("template", "hi", claude_cli=Path("claude"))


if __name__ == "__main__":
    unittest.main()
