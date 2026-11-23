"""Packs that include packs, pack files, and .skillpacks.json."""
import io
import json
import unittest
from unittest import mock

from spm import cli, packset, share
from spm.core import OpError
from spm.server import App
from tests.test_mcp import FakeClaude, McpEnv, SECRET


class Includes(McpEnv):
    def test_attach_brings_included_packs(self):
        self.m.run("pack_include", {"pack": "one", "include": "web"}, dry=False)
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        self.assertIn("web-mcp", self.opencode()["mcp"])  # web's MCP server came along
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual((v["packs"], v["via"]), (["one"], ["web"]))
        sources = {m["name"]: m["sources"] for m in v["mcp"]}
        self.assertEqual(sources["web-mcp"], ["web"])

    def test_changing_an_included_pack_updates_folders_of_the_outer_pack(self):
        self.m.run("pack_include", {"pack": "one", "include": "two"}, dry=False)
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "gamma")), 3)  # from two
        self.m.run("pack_add_skill", {"pack": "two", "skill": "delta"}, dry=False)
        self.assertEqual(len(self.linked(self.proj, "delta")), 3)
        self.m.run("pack_exclude", {"pack": "one", "include": "two"}, dry=False)
        self.assertEqual(self.linked(self.proj, "gamma"), [])
        self.store_intact()

    def test_cycles_and_deleting_an_included_pack_are_refused(self):
        self.m.run("pack_include", {"pack": "one", "include": "two"}, dry=False)
        self.m.run("pack_include", {"pack": "two", "include": "web"}, dry=False)
        with self.assertRaises(OpError):
            self.m.run("pack_include", {"pack": "web", "include": "one"}, dry=True)
        with self.assertRaises(OpError):
            self.m.run("pack_include", {"pack": "one", "include": "one"}, dry=True)
        with self.assertRaises(OpError):
            self.m.run("delete_pack", {"pack": "two"}, dry=True)
        self.assertEqual(packset.expand(self.m.load(), ["one"]), ["one", "two", "web"])

    def test_included_pack_lists_the_folders_it_reaches(self):
        self.m.run("pack_include", {"pack": "one", "include": "two"}, dry=False)
        self.m.run("pack_include", {"pack": "two", "include": "web"}, dry=False)
        self.m.run("attach", {"pack": "one", "folder": str(self.proj)}, dry=False)
        self.m.run("attach", {"pack": "two", "folder": str(self.other)}, dry=False)
        packs = {p["name"]: p for p in self.m.state()["packs"]}
        self.assertEqual(packs["one"]["derived_paths"], [])
        self.assertEqual(packs["two"]["derived_paths"], [{"folder": str(self.proj), "via": ["one"]}])
        web = sorted(packs["web"]["derived_paths"], key=lambda d: d["folder"])
        self.assertEqual(web, sorted([{"folder": str(self.proj), "via": ["one"]},
                                      {"folder": str(self.other), "via": ["two"]}], key=lambda d: d["folder"]))
        # attached directly as well: listed once, under the pack's own folders
        self.m.run("attach", {"pack": "two", "folder": str(self.proj)}, dry=False)
        packs = {p["name"]: p for p in self.m.state()["packs"]}
        self.assertEqual(packs["two"]["derived_paths"], [])
        out = io.StringIO()
        cli.run(["packs"], app=mock.Mock(model=self.m), out=out)
        self.assertIn(f"folder: {self.other} (via two)", out.getvalue())

    def test_hand_edited_cycle_does_not_loop(self):
        data = self.m.load()
        data["packs"]["one"]["includes"] = ["two"]
        data["packs"]["two"]["includes"] = ["one"]
        self.assertEqual(packset.expand(data, ["one"]), ["one", "two"])


class Files(McpEnv):
    def setUp(self):
        super().setUp()
        skills = self.m.source.skills()
        for d in skills.values():
            d["source"] = None
        skills["alpha"]["source"] = {"url": "https://github.com/o/r", "ref": "v1", "kind": "tag", "path": "skills/alpha",
                                     "commit": "abc"}
        self.m.source.skills = lambda: skills
        self.m.run("pack_include", {"pack": "one", "include": "web"}, dry=False)

    def test_export_holds_sources_and_no_secrets(self):
        b = share.export(self.m, self.m.load(), ["one"])
        self.assertEqual(sorted(b["packs"]), ["one", "web"])
        self.assertEqual(b["packs"]["one"]["includes"], ["web"])
        self.assertEqual(b["skills"]["alpha"]["url"], "https://github.com/o/r")
        self.assertEqual(b["skills"]["beta"], {})
        self.assertEqual(b["mcp"]["alpha-mcp"]["env"], {"KEY": ""})
        self.assertEqual(b["mcp"]["web-mcp"]["headers"], {"Authorization": ""})
        self.assertNotIn(SECRET, json.dumps(b))
        self.assertEqual(share.parse(json.dumps(b))["packs"]["one"]["skills"], ["alpha", "beta"])

    def test_parse_rejects_bad_files(self):
        ok = {"format": "skill-pack-manager", "version": 1, "packs": {"p": {"skills": ["a"]}}}
        share.parse(ok)
        bad = [
            {**ok, "format": "x"}, {**ok, "version": 2}, {**ok, "packs": {}},
            {**ok, "packs": {"Bad Name": {}}}, {**ok, "packs": {"p": {"skills": ["../x"]}}},
            {**ok, "packs": {"p": {"includes": ["q"]}}}, {**ok, "attach": ["q"]},
            {**ok, "skills": {"a": {"url": "ext::sh -c x", "ref": "main"}}},
            {**ok, "skills": {"a": {"url": "https://h/r", "ref": "--upload-pack=x"}}},
            {**ok, "skills": {"a": {"url": "https://h/r", "ref": "main", "path": "../../etc"}}},
            {**ok, "mcp": {"m": {"nothing": 1}}},
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(share.ShareError):
                share.parse(b)
        with self.assertRaises(share.ShareError):
            share.parse("not json")

    def test_preview_and_import(self):
        b = share.parse(share.export(self.m, self.m.load(), ["one"]))
        b["packs"]["new-pack"] = {"skills": ["gamma", "zeta"], "mcp": ["alpha-mcp"], "includes": []}
        b["skills"]["zeta"] = {"url": "https://github.com/o/z", "ref": "main", "path": "."}
        b["packs"]["web"]["skills"] = ["delta"]  # differs from the local web pack
        pv = share.preview(self.m, self.m.load(), b)
        st = {p["name"]: p["status"] for p in pv["packs"]}
        self.assertEqual(st, {"one": "same", "web": "differs", "new-pack": "new"})
        sk = {s["dir"]: s["status"] for s in pv["skills"]}
        self.assertEqual(sk["zeta"], "missing")  # cc-switch mode cannot install
        self.assertTrue(pv["warnings"])
        self.m.source.mode = "standalone"
        pv = share.preview(self.m, self.m.load(), b)
        self.assertEqual(pv["repos"], [{"url": "https://github.com/o/z", "ref": "main", "paths": ["."], "dirs": ["zeta"]}])
        b["attach"] = ["new-pack"]
        self.m.run("import_packs", {"bundle": b, "folder": str(self.proj)}, dry=False)
        data = self.m.load()
        self.assertEqual(data["packs"]["web"]["skills"], [])  # kept: replace was off
        self.assertEqual(data["packs"]["new-pack"]["paths"], [str(self.proj)])
        self.assertEqual(len(self.linked(self.proj, "gamma")), 3)
        self.m.run("import_packs", {"bundle": b, "replace": True}, dry=False)
        self.assertEqual(self.m.load()["packs"]["web"]["skills"], ["delta"])


class Manifest(McpEnv):
    def setUp(self):
        super().setUp()
        self.app = App(self.cfg)
        self.app.model.adapters["claude"] = FakeClaude(self.cfg.claude_json, self.cfg.claude_cli)
        self.m = self.app.model

    def write(self, attach=("one",)):
        b = share.export(self.m, self.m.load(), list(attach), list(attach))
        (self.proj / share.MANIFEST).write_text(json.dumps(b))

    def test_summary_and_apply(self):
        self.write()
        v = self.m.folder_view(self.m.load(), str(self.proj))
        self.assertEqual(v["manifest"]["pending"], ["one"])
        self.assertFalse(v["manifest"]["done"])
        b = self.app.bundle({"folder": str(self.proj)})
        self.m.run("import_packs", {"bundle": b, "folder": str(self.proj)}, dry=False)
        self.assertTrue(self.m.folder_view(self.m.load(), str(self.proj))["manifest"]["done"])
        self.assertEqual(len(self.linked(self.proj, "alpha")), 3)
        self.assertNotIn(share.MANIFEST, self.excludes())  # meant to be committed

    def test_broken_manifest_is_reported_not_applied(self):
        (self.proj / share.MANIFEST).write_text('{"format": "skill-pack-manager", "version": 1, "packs": {"x": {"skills": ["../a"]}}}')
        self.assertIn("error", self.m.folder_view(self.m.load(), str(self.proj))["manifest"])

    def test_scan_marks_projects_with_a_manifest(self):
        self.write()
        found = {p["folder"]: p for p in self.m.scan()}
        self.assertTrue(found[str(self.proj)]["manifest"])

    def test_cli_manifest_write_and_apply(self):
        out = io.StringIO()
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cli.run(["manifest", str(self.proj), "--write", "one"], app=self.app, out=out), 2)
            self.assertFalse((self.proj / share.MANIFEST).exists())
            self.assertEqual(cli.run(["manifest", str(self.proj), "--write", "one", "-y"], app=self.app, out=out), 0)
            self.assertNotIn(SECRET, (self.proj / share.MANIFEST).read_text())
            out = io.StringIO()
            self.assertEqual(cli.run(["manifest", str(self.proj)], app=self.app, out=out), 0)
            self.assertIn("not applied yet", out.getvalue())
            self.assertEqual(cli.run(["manifest", str(self.proj), "--apply", "-y"], app=self.app, out=io.StringIO()), 0)
        self.assertEqual(len(self.linked(self.proj, "beta")), 3)

    def test_cli_export_import_and_include(self):
        f = self.proj.parent / "one.skillpack.json"
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cli.run(["pack", "include", "one", "two", "-y"], app=self.app, out=io.StringIO()), 0)
            self.assertEqual(cli.run(["export", "one", "-o", str(f)], app=self.app, out=io.StringIO()), 0)
            data = self.m.load()
            del data["packs"]["one"], data["packs"]["two"]
            self.m.save(data)
            out = io.StringIO()
            self.assertEqual(cli.run(["import", str(f), "--dry-run"], app=self.app, out=out), 0)
            self.assertIn("pack one: new", out.getvalue())
            self.assertNotIn("one", self.m.load()["packs"])
            self.assertEqual(cli.run(["import", str(f), "-y"], app=self.app, out=io.StringIO()), 0)
        self.assertEqual(self.m.load()["packs"]["one"]["includes"], ["two"])


if __name__ == "__main__":
    unittest.main()
