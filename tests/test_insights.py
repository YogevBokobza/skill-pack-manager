import json
import os
import sqlite3
import unittest
from datetime import date
from pathlib import Path

from spm import core, insights, mcp, usage
from spm.core import Model
from tests.test_core import Env, git


class FakeSource:
    """The store of Env, with chosen global switches."""
    mode = "standalone"
    editable = True

    def __init__(self, store, glob=None):
        self.store, self.glob = store, glob or {}

    def skills(self):
        out = {}
        for p in sorted(self.store.iterdir()):
            fm = insights.frontmatter((p / "SKILL.md").read_text())
            out[p.name] = {"name": fm["name"], "description": fm["description"], "global": self.glob.get(p.name, []),
                           "registered": True, "source": None}
        return out

    def mcp_catalog(self):
        return mcp.Catalog({})


def jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")


class Usage(Env):
    def test_router_reads_and_agents_are_counted(self):
        ref = r"D:\data\routers\using-web\references\alpha\alpha.md"
        jsonl(self.home / ".claude/projects/p/s.jsonl", [
            {"cwd": "/work/a", "timestamp": "2026-03-01T00:00:00Z", "name": "Skill", "input": {"skill": "beta"}},
            {"timestamp": "2026-03-04T00:00:00Z", "type": "tool_use", "name": "Read", "input": {"file_path": ref}},
            {"timestamp": "2026-03-05T00:00:00Z", "type": "tool_use", "name": "Read",
             "input": {"file_path": "/x/references/other/notes.md"}},  # not a router file
        ])
        jsonl(self.home / ".codex/sessions/2026/03/09/r.jsonl", [
            {"cwd": "/work/b"}, {"type": "function_call", "arguments": "cat /h/.agents/skills/alpha/SKILL.md"}])
        u = self.m.usage()
        self.assertEqual(u["alpha"]["uses"], 2)
        self.assertEqual(u["alpha"]["router"], 1)
        self.assertEqual(u["alpha"]["agents"], {"claude": 1, "codex": 1})
        self.assertEqual(u["alpha"]["days"], {"2026-03-04": 1, "2026-03-09": 1})
        self.assertEqual(u["beta"]["folders"], [["/work/a", 1, "2026-03-01"]])
        self.assertEqual(u["beta"]["where"], [["/work/a", 1]])
        self.assertTrue(u["_since"])

    def test_opencode_v2_skill_id(self):
        db = self.home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("create table session (id text, directory text, time_created integer)")
        con.execute("create table part (id text, session_id text, time_created integer, data text)")
        con.execute("insert into session values ('s', '/w', 1767225600000)")
        for i, inp in enumerate(({"name": "alpha"}, {"id": "gamma"})):
            con.execute("insert into part values (?, 's', 1767225600000, ?)",
                        (str(i), json.dumps({"tool": "skill", "state": {"input": inp}}, separators=(",", ":"))))
        con.commit()
        con.close()
        rows, since = usage.events({"alpha", "gamma"}, self.home)
        self.assertEqual(sorted(r[0] for r in rows), ["alpha", "gamma"])
        self.assertEqual({r[3] for r in rows}, {"opencode"})
        self.assertEqual(since, "2026-01-01")


class Costs(Env):
    def test_tokens_estimate(self):
        self.assertEqual(insights.tokens("ab", "cd"), 3)  # "- ab: cd\n" is 9 chars
        self.assertEqual(insights.tokens("abc", "def"), 3)  # 11 chars

    def test_folder_cost_counts_global_and_project_once(self):
        g = self.home / ".claude/skills"
        g.mkdir(parents=True)
        core.make_junction(self.store / "alpha", g / "alpha")
        hidden = self.home / ".agents/skills/secret"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text("---\nname: secret\ndescription: x\ndisable-model-invocation: true\n---\n")
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)  # alpha, beta
        c = insights.folder_cost(self.m, self.m.load(), self.proj)
        two = insights.tokens("alpha", "alpha skill") + insights.tokens("beta", "beta skill")
        self.assertEqual(c["claude"]["count"], 2)  # alpha global and in the project: once
        self.assertEqual(c["claude"]["tokens"], two)
        self.assertEqual(c["codex"]["count"], 2)  # .agents/skills; the hidden one is not listed
        self.assertEqual(c["opencode"]["count"], 2)
        all_ = insights.costs(self.m, self.m.load())
        self.assertEqual(all_["global"]["claude"]["count"], 1)
        self.assertEqual(all_["global"]["codex"]["count"], 0)
        self.assertEqual([f["folder"] for f in all_["folders"]], [str(self.proj)])
        self.assertEqual(self.m.folder_view(self.m.load(), str(self.proj))["cost"]["claude"]["count"], 2)

    def test_router_savings(self):
        data = self.m.load()
        data["packs"]["one"]["skills"] = ["alpha", "beta", "gamma"]
        data["packs"]["one"]["paths"] = [str(self.proj)]
        self.m.save(data)
        s = insights.router_savings(self.m, self.m.load(), insights.Reader(), self.proj)
        self.assertEqual(s[0]["saved"], 0)
        self.assertGreaterEqual(s[0]["could_save"], 0)
        kinds = [x["kind"] for x in insights.suggestions(self.m, self.m.load(), {})]
        self.assertEqual(kinds, [])  # descriptions this short cost less than a router


class Suggestions(Env):
    def model(self, glob):
        return Model(self.cfg, FakeSource(self.store, glob))

    def test_cleanup_ideas(self):
        other = self.proj.parent / "proj2"
        other.mkdir()
        m = self.model({"alpha": ["claude"], "delta": ["claude", "codex"]})
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)  # alpha, beta
        m.run("extra_add", {"folder": str(other), "skill": "gamma"}, dry=False)
        today = date(2026, 6, 1)
        u = {"_at": 1, "_since": "2026-01-01",
             "alpha": {"uses": 3, "days": {"2026-05-20": 3},
                       "folders": [[str(self.proj / "sub"), 3, "2026-05-20"]]},
             "beta": {"uses": 2, "days": {"2026-01-10": 1, "2026-05-25": 1},
                      "folders": [[str(self.proj), 1, "2026-01-10"], [str(other), 1, "2026-05-25"]]},
             "gamma": {"uses": 2, "days": {"2026-05-30": 2}, "folders": [[str(self.proj), 2, "2026-05-30"]]}}
        got = {(s["kind"], s.get("skill")): s for s in insights.suggestions(m, m.load(), u, 60, today)}
        self.assertIn(("unused_global", "delta"), got)
        self.assertEqual(got[("unused_global", "delta")]["action"], {"global_off": ["claude", "codex"]})
        move = got[("global_one_folder", "alpha")]  # used only below proj
        self.assertEqual(move["folder"], str(self.proj))
        self.assertEqual(move["action"]["op"], "extra_add")
        self.assertIn(("unused_in_pack", "beta"), got)  # last use in proj is too old
        self.assertNotIn(("unused_in_pack", "alpha"), got)
        self.assertIn(("unused_extra", "gamma"), got)  # used, but in proj, not in proj2
        self.assertNotIn(("unused_global", "alpha"), got)

    def test_folders_without_recent_activity_are_not_judged(self):
        m = self.model({})
        m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)  # attached today, no sessions yet
        m.run("extra_add", {"folder": str(self.proj), "skill": "gamma"}, dry=False)
        u = {"_at": 1, "alpha": {"uses": 1, "days": {"2026-01-02": 1}, "folders": [[str(self.proj), 1, "2026-01-02"]]}}
        self.assertEqual(insights.suggestions(m, m.load(), u, 60, date(2026, 6, 1)), [])

    def test_no_usage_ideas_before_a_count(self):
        m = self.model({"delta": ["claude"]})
        self.assertEqual(insights.suggestions(m, m.load(), {}), [])

    def test_cc_switch_mode_gives_a_hint_instead_of_an_action(self):
        m = self.model({"delta": ["claude"]})
        m.source.mode = "cc-switch"
        s = insights.suggestions(m, m.load(), {"_at": 1}, 60, date(2026, 6, 1))
        self.assertEqual(s[0]["kind"], "unused_global")
        self.assertIsNone(s[0]["action"])
        self.assertIn("cc-switch", s[0]["hint"])

    def test_router_idea_for_a_big_pack(self):
        for s in ("alpha", "beta", "gamma", "delta"):
            (self.store / s / "SKILL.md").write_text(f"---\nname: {s}\ndescription: {'long words ' * 40}\n---\n")
        data = self.m.load()
        data["packs"]["one"]["skills"] = ["alpha", "beta", "gamma", "delta"]
        data["packs"]["one"]["paths"] = [str(self.proj)]
        self.m.save(data)
        s = insights.suggestions(self.m, self.m.load(), {})
        self.assertEqual([(x["kind"], x["pack"]) for x in s], [("create_router", "one")])
        self.assertEqual(s[0]["action"], {"router": "one"})
        self.assertGreater(s[0]["tokens"], 100)


class Stack(Env):
    def test_tags_from_marker_files(self):
        d = self.proj
        (d / "package.json").write_text(json.dumps({"dependencies": {"react": "1", "next": "1"},
                                                    "devDependencies": {"typescript": "5"}}))
        (d / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.1", "flaskish"]\n')
        (d / "Dockerfile").write_text("FROM x")
        self.assertEqual(insights.tags_of(d), ["node", "react", "next", "typescript", "python", "fastapi", "docker"])

    def test_suggest_packs(self):
        data = {"packs": {"web": {"paths": [], "skills": []}, "unity": {"paths": [], "skills": []},
                          "tools": {"paths": ["/x/a"], "skills": []}}}
        s = insights.suggest_packs(["unity"], data, {})
        self.assertEqual(s[0]["pack"], "unity")
        s = insights.suggest_packs(["node", "react"], data, {core.norm("/x/a"): ["react"]})
        self.assertEqual([x["pack"] for x in s], ["web", "tools"])
        self.assertIn("similar", s[1]["why"])
        self.assertEqual(insights.suggest_packs([], data, {}), [])

    def test_scan_finds_repos_with_tags_and_suggestions(self):
        app = self.proj.parent / "shop"
        app.mkdir()
        git("init", "-q", cwd=app)
        (app / "web").mkdir()
        (app / "web" / "package.json").write_text(json.dumps({"dependencies": {"vue": "3"}}))
        data = self.m.load()
        data["packs"]["web"] = {"skills": [], "mcp": [], "paths": []}
        self.m.save(data)
        found = {Path(x["folder"]).name: x for x in self.m.scan()}
        self.assertEqual(found["shop"]["tags"], ["node", "vue"])
        self.assertEqual(found["shop"]["kind"], "node")
        self.assertEqual(found["shop"]["suggested"][0]["pack"], "web")
        self.assertIn("proj", found)  # a plain git repository


class Endpoint(Env):
    def test_insights_endpoint(self):
        from spm.server import App, serve
        import urllib.request
        app = App(self.cfg)
        srv, port = serve(app)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/insights?days=30")
            req.add_header("X-SPM-Token", app.token)
            with urllib.request.urlopen(req) as r:
                j = json.loads(r.read())
            self.assertEqual(j["days"], 30)
            self.assertIn("claude", j["costs"]["global"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
