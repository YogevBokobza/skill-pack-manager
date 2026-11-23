"""Command line: the same operations as the window, for scripts, git hooks
and agents.

  spm status [FOLDER]            folders in packs.json, or one folder in detail
  spm sync [FOLDER | --all]      make folders match packs.json
  spm attach PACK [FOLDER]       attach a pack (FOLDER defaults to .)
  spm detach PACK [FOLDER]
  spm extra add|remove SKILL [FOLDER]
  spm pack add|remove PACK SKILL...
  spm pack include|exclude PACK OTHER   a pack that brings another pack along
  spm export PACK... [-o FILE]   a pack file: packs, skill sources, MCP servers without secrets
  spm import FILE [FOLDER]       preview a pack file, then add its packs (and attach them)
  spm manifest [FOLDER]          the folder's .skillpacks.json; --write PACK... creates it
  spm agents [AGENT on|off|auto] which agents are managed; switching one off, then
                                 spm sync --all removes what was added for it
  spm router-test PACK           test a pack router with the AI writer (--cases FILE, --write-cases)
  spm packs                      packs with their skills, MCP servers, folders
  spm skills [QUERY]             skills in the store
  spm tokens [FOLDER]            estimated context cost per agent
  spm usage [--count]            skill use from past sessions
  spm suggest [--days N]         cleanup suggestions
  spm scan                       projects under the scan roots, with pack suggestions
  spm check-updates              standalone mode: newer commits or tags
  spm update SKILL [--ref REF]   standalone: show what changes, then install
  spm rollback SKILL [VERSION]   standalone: list earlier versions, or restore one

Commands that change something print the plan first. In a terminal they ask
before applying; elsewhere (scripts, agents) they need --yes. --dry-run only
prints the plan. --json prints machine-readable output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core import OpError, UnsafeRemoval
from .mcp import McpError
from .sources import SourceError

COMMANDS = ("status", "sync", "attach", "detach", "extra", "pack", "packs", "skills", "tokens", "usage", "suggest",
            "scan", "check-updates", "update", "rollback", "export", "import", "manifest", "router-test", "agents")
LABEL = {"claude": "Claude Code", "opencode": "OpenCode", "codex": "Codex", "gemini": "Gemini CLI"}


class CliError(Exception):
    pass


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="spm", description="Skill Pack Manager on the command line.",
                                 epilog="Without a command, spm opens the app window.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name, help_, folder=False, change=False):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if folder:
            p.add_argument("folder", nargs="?", default=None, help="project folder (default: the current folder)")
        if change:
            p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
            p.add_argument("-y", "--yes", action="store_true", help="apply without asking")
        return p

    cmd("status", "folders in packs.json, or one folder in detail").add_argument("folder", nargs="?")
    p = cmd("sync", "make folders match packs.json", change=True)
    p.add_argument("folder", nargs="?")
    p.add_argument("--all", action="store_true", help="every folder in packs.json")
    for name in ("attach", "detach"):
        p = cmd(name, f"{name} a pack", change=True)
        p.add_argument("pack")
        p.add_argument("folder", nargs="?")
    p = cmd("extra", "add or remove a single skill for one folder", change=True)
    p.add_argument("action", choices=("add", "remove"))
    p.add_argument("skill")
    p.add_argument("folder", nargs="?")
    p = cmd("pack", "add or remove skills, or include another pack", change=True)
    p.add_argument("action", choices=("add", "remove", "include", "exclude"))
    p.add_argument("pack")
    p.add_argument("skills", nargs="+", help="skills (add, remove) or one pack (include, exclude)")
    p = cmd("export", "write a pack file")
    p.add_argument("packs", nargs="+")
    p.add_argument("-o", "--output", help="file to write (default: print it)")
    p = cmd("import", "add the packs of a pack file", change=True)
    p.add_argument("file")
    p.add_argument("folder", nargs="?", help="attach the file's \"attach\" packs to this folder")
    p.add_argument("--replace", action="store_true", help="replace packs that exist with other content")
    p = cmd("agents", "show or switch the agents this program manages")
    p.add_argument("agent", nargs="?", choices=("claude", "opencode", "codex", "gemini"))
    p.add_argument("state", nargs="?", choices=("on", "off", "auto"))
    p = cmd("router-test", "test whether agents open a pack router and pick the right skill")
    p.add_argument("pack")
    p.add_argument("--cases", help="file with 'skill: request' lines (default: the last test's requests)")
    p.add_argument("--write-cases", action="store_true", help="have the AI write new requests first")
    p.add_argument("--writer", help="AI writer (default: the one in Settings)")
    p = cmd("manifest", "show or write the folder's .skillpacks.json", change=True)
    p.add_argument("folder", nargs="?")
    p.add_argument("--write", nargs="+", metavar="PACK", help="write .skillpacks.json asking for these packs")
    p.add_argument("--apply", action="store_true", help="add and attach the packs it asks for")
    cmd("packs", "list packs")
    cmd("skills", "list skills in the store").add_argument("query", nargs="?", default="")
    cmd("tokens", "estimated context cost per agent", folder=True)
    cmd("usage", "skill use from past sessions").add_argument("--count", action="store_true",
                                                              help="read the session logs again first")
    cmd("suggest", "cleanup suggestions").add_argument("--days", type=int, default=60)
    cmd("scan", "projects under the scan roots")
    cmd("check-updates", "newer commits or tags for installed skills (standalone)")
    p = cmd("update", "review and install another version of a skill (standalone)", change=True)
    p.add_argument("skill")
    p.add_argument("--ref", help="branch or tag (default: the installed one, newest commit)")
    p.add_argument("--diff", action="store_true", help="print the full diff")
    p = cmd("rollback", "restore an earlier version of a skill (standalone)", change=True)
    p.add_argument("skill")
    p.add_argument("version", nargs="?", help="a version id from the list; without it, list them")
    return ap


def run(argv: list[str], app=None, out=None, stdin=None) -> int:
    """Run one command. Returns the exit code: 0 done, 1 error, 2 not applied
    (changes need --yes, or the answer was no)."""
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    a = parser().parse_args(argv)
    if app is None:
        from .server import App
        app = App()
    try:
        return Cli(app, a, out, stdin).dispatch()
    except KeyError as e:  # str(KeyError) quotes the key
        print(f"error: missing {e}", file=sys.stderr)
        return 1
    except (CliError, OpError, UnsafeRemoval, McpError, SourceError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


class Cli:
    def __init__(self, app, a, out, stdin):
        self.app, self.m, self.a, self.out, self.stdin = app, app.model, a, out, stdin

    def p(self, *parts):
        print(*parts, file=self.out)

    def emit(self, obj, text) -> int:
        if self.a.json:
            self.p(json.dumps(obj, indent=2, ensure_ascii=False))
        else:
            text()
        return 0

    def folder(self) -> str:
        f = getattr(self.a, "folder", None) or os.getcwd()
        return str(Path(f).expanduser().resolve())

    def known(self, f: str) -> str:
        """The spelling packs.json uses for f, if it has one."""
        from .core import all_folders, norm
        return next((x for x in all_folders(self.m.load()) if norm(x) == norm(f)), f)

    def dispatch(self) -> int:
        return getattr(self, "c_" + self.a.cmd.replace("-", "_"))()

    # --------------------------------------------------------------- changes

    def change(self, op: str, args: dict, title: str) -> int:
        pre = self.m.run(op, args, dry=True)
        n = changes(pre)
        if self.a.json and (self.a.dry_run or not self.a.yes):
            self.p(json.dumps(pre | {"applied": False}, indent=2, ensure_ascii=False))
        elif not self.a.json:
            self.p(f"{title}:")
            for line in plan_lines(pre):
                self.p("  " + line)
        if self.a.dry_run:
            return 0
        if not self.a.yes:
            if not self.stdin.isatty():
                print("not applied: add --yes to apply", file=sys.stderr)
                return 2
            print("Apply? [y/N] ", end="", file=self.out, flush=True)
            if self.stdin.readline().strip().lower() not in ("y", "yes"):
                self.p("not applied")
                return 2
        res = self.m.run(op, args, dry=False)
        if self.a.json:
            self.p(json.dumps(res | {"applied": True}, indent=2, ensure_ascii=False))
        else:
            for line in res["log"]:
                self.p("  " + line)
            self.p(f"done, {n} change{'s' if n != 1 else ''}" if not res["errors"] else "saved, but some changes failed:")
        for e in res["errors"]:
            print(f"  {e}", file=sys.stderr)
        return 1 if res["errors"] else 0

    def c_attach(self):
        f = self.folder()
        return self.change("attach", {"pack": self.a.pack, "folder": f}, f"Attach {self.a.pack} to {f}")

    def c_detach(self):
        f = self.known(self.folder())
        return self.change("detach", {"pack": self.a.pack, "folder": f}, f"Detach {self.a.pack} from {f}")

    def c_extra(self):
        f = self.known(self.folder())
        op = "extra_add" if self.a.action == "add" else "extra_remove"
        return self.change(op, {"folder": f, "skill": self.a.skill}, f"{self.a.action.title()} {self.a.skill} in {f}")

    def c_pack(self):
        if self.a.action in ("include", "exclude"):
            if len(self.a.skills) != 1:
                raise CliError(f"pack {self.a.action} takes one pack")
            other = self.a.skills[0]
            return self.change(f"pack_{self.a.action}", {"pack": self.a.pack, "include": other},
                               f"{self.a.pack} {'includes' if self.a.action == 'include' else 'stops including'} {other}")
        op = "pack_add_skills" if self.a.action == "add" else "pack_remove_skills"
        verb = "Add to" if self.a.action == "add" else "Remove from"
        return self.change(op, {"pack": self.a.pack, "skills": self.a.skills}, f"{verb} {self.a.pack}: {', '.join(self.a.skills)}")

    def c_sync(self):
        from .core import all_folders
        if self.a.all:
            folders = all_folders(self.m.load())
        else:
            folders = [self.known(self.folder())]
        code = 0
        for f in folders:
            if not Path(f).is_dir():
                self.p(f"{f}: folder missing, skipped")
                continue
            code = max(code, self.change("sync", {"folder": f}, f"Sync {f}"))
        return code

    # ------------------------------------------------------------------ views

    def c_status(self):
        data = self.m.load()
        if self.a.folder:
            f = self.known(self.folder())
            v = self.m.folder_view(data, f)
            v.pop("plan", None)

            def text():
                self.p(f)
                self.p(f"  packs: {', '.join(v['packs']) or 'none'}   extras: {', '.join(v['extras']) or 'none'}")
                self.p(f"  {'in sync' if v['in_sync'] else 'out of sync: run spm sync'}")
                for s in v["skills"]:
                    self.p(f"  {STATUS.get(s['status'], s['status']):<26} {s['skill']}"
                           + (f"  ({', '.join(x.replace('+extra', 'extra') for x in s['sources'])})" if s["sources"] else ""))
                for s in v["mcp"]:
                    self.p(f"  {'MCP ' + STATUS.get(s['status'], s['status']):<26} {s['name']}")
            return self.emit(v, text)
        st = self.m.state()
        rows = st["folders"]

        def text():
            if not rows:
                self.p("no folders in packs.json")
            for r in rows:
                state = "missing" if not r["exists"] else "in sync" if r["in_sync"] else "out of sync"
                via = [f"{p} via {', '.join(r['via_from'].get(p, []))}" for p in r.get("via") or []]
                self.p(f"{state:<12} {r['folder']}  [{', '.join(r['packs'] + via) or 'no pack'}]"
                       + (f" +{len(r['extras'])} extra" if r["extras"] else ""))
        return self.emit(rows, text)

    def c_packs(self):
        st = self.m.state()

        def text():
            for p in st["packs"]:
                r = p["router"]
                self.p(f"{p['name']}: {len(p['skills'])} skills, {len(p['mcp'])} MCP, {len(p['paths'])} folders"
                       + (f", router {r['name']} ({r['mode']})" if r["enabled"] else ""))
                if p["skills"]:
                    self.p(f"  skills: {', '.join(p['skills'])}")
                if p["mcp"]:
                    self.p(f"  mcp:    {', '.join(p['mcp'])}")
                for f in p["paths"]:
                    self.p(f"  folder: {f}")
                for d in p.get("derived_paths") or []:
                    self.p(f"  folder: {d['folder']} (via {', '.join(d['via'])})")
        return self.emit(st["packs"], text)

    def c_skills(self):
        q = self.a.query.lower()
        rows = [s for s in self.m.catalog(self.m.load())
                if not q or q in s["dir"].lower() or q in s["description"].lower()
                or q in json.dumps(s.get("source") or {}).lower()]

        def text():
            for s in rows:
                where = ", ".join(s["packs"]) or ("global" if s["global"] else "unassigned")
                self.p(f"{s['dir']:<32} ~{s['tokens']:>4} tok  {where}")
        return self.emit(rows, text)

    def c_tokens(self):
        from .core import all_folders
        data = self.m.load()
        if self.a.folder or self.known(self.folder()) in all_folders(data):
            f = self.known(self.folder())
            c = self.m.folder_view(data, f)["cost"]

            def text():
                self.p(f"{f} (estimated tokens of skill listings per session)")
                for a, v in c.items():
                    self.p(f"  {LABEL[a]:<12} ~{v['tokens']:>6}  {v['count']} skills (global ~{v['global_tokens']})")
            return self.emit(c, text)
        c = self.m.insights()["costs"]

        def text():
            self.p("estimated tokens of skill listings per session")
            self.p(f"  {'outside projects':<50} " + "  ".join(f"{LABEL[a]} ~{v['tokens']}" for a, v in c["global"].items()))
            for f in c["folders"]:
                self.p(f"  {f['folder']:<50} " + "  ".join(f"{LABEL[a]} ~{v['tokens']}" for a, v in f["agents"].items()))
        return self.emit(c, text)

    def c_usage(self):
        u = self.m.usage() if self.a.count else self.m.cached_usage()
        if not u.get("_at") and not self.a.count:
            raise CliError("usage was never counted; run: spm usage --count")
        rows = sorted(((k, v) for k, v in u.items() if not k.startswith("_")), key=lambda kv: -kv[1]["uses"])

        def text():
            if u.get("_since"):
                self.p(f"logs go back to {u['_since']}")
            for k, v in rows:
                agents = ", ".join(f"{LABEL.get(a, a)} {n}" for a, n in (v.get("agents") or {}).items())
                self.p(f"{v['uses']:>6}  {k:<32} last {v['last']}  {agents}")
        return self.emit(dict(rows) | {"_since": u.get("_since")}, text)

    def c_suggest(self):
        s = self.m.insights(self.a.days)["suggestions"]

        def text():
            if not self.m.cached_usage().get("_at"):
                self.p("(no usage count yet: run spm usage --count for ideas based on use)")
            if not s:
                self.p("nothing to suggest")
            for x in s:
                self.p(f"- {x['text']}" + (f"  (~{x['tokens']} tokens per session)" if x["tokens"] else ""))
                a = x.get("action") or {}
                if a.get("op") == "pack_remove_skill":
                    self.p(f"    spm pack remove {a['args']['pack']} {a['args']['skill']}")
                elif a.get("op") == "extra_remove":
                    self.p(f"    spm extra remove {a['args']['skill']} \"{a['args']['folder']}\"")
                elif a.get("op") == "extra_add":
                    self.p(f"    spm extra add {a['args']['skill']} \"{a['args']['folder']}\"")
                if x.get("hint"):
                    self.p(f"    {x['hint']}")
        return self.emit(s, text)

    def c_scan(self):
        rows = self.m.scan()

        def text():
            for r in rows:
                sug = r.get("suggested") or []
                self.p(f"{r['folder']}  [{', '.join(r['tags']) or r['kind']}]  packs: {', '.join(r['packs']) or 'none'}"
                       + (f"  suggested: {sug[0]['pack']} ({sug[0]['why']})" if sug and not r["packs"] else ""))
        return self.emit(rows, text)

    def c_check_updates(self):
        u = self.app.local().check_updates()

        def text():
            for d, v in sorted(u.items()):
                if v.get("error"):
                    self.p(f"{d}: {v['error']}")
                elif v["branch_moved"]:
                    self.p(f"{d}: new commits on {v['ref']}")
                elif v["newer_tags"]:
                    self.p(f"{d}: newer tag {v['newer_tags'][0]} (installed {v['ref']})")
            if not any(v.get("branch_moved") or v.get("newer_tags") or v.get("error") for v in u.values()):
                self.p("all skills are up to date")
        return self.emit(u, text)


    # ---------------------------------------------------------------- sharing

    def c_export(self):
        from . import share
        b = share.export(self.m, self.m.load(), self.a.packs)
        text = json.dumps(b, indent=2, ensure_ascii=False) + "\n"
        if self.a.output:
            Path(self.a.output).write_text(text, encoding="utf8")
            if not self.a.json:
                self.p(f"wrote {self.a.output}: {len(b['packs'])} packs, {len(b['skills'])} skills, {len(b['mcp'])} MCP servers")
            return 0
        self.out.write(text)
        return 0

    def show_import(self, pv: dict) -> None:
        if self.a.json:
            self.p(json.dumps({k: v for k, v in pv.items() if k != "bundle"}, indent=2, ensure_ascii=False))
            return
        for w in pv["warnings"]:
            self.p(f"! {w}")
        for p in pv["packs"]:
            self.p(f"pack {p['name']}: {p['status']} ({len(p['skills'])} skills, {len(p['mcp'])} MCP"
                   + (f", includes {', '.join(p['includes'])}" if p["includes"] else "") + ")")
        for s in pv["skills"]:
            src = s["source"] or {}
            self.p(f"  skill {s['dir']:<30} {s['status']}" + (f"  {src['url']} @ {src['ref']}" if src.get("url") else ""))
        for m in pv["mcp"]:
            self.p(f"  mcp   {m['name']:<30} {m['status']}")

    def run_import(self, bundle: dict, folder: str | None) -> int:
        from . import share
        pv = share.preview(self.m, self.m.load(), bundle, self.a.replace if hasattr(self.a, "replace") else False)
        self.show_import(pv)
        stop = self.confirm()
        if stop is not None:
            return stop
        for line in self.app.add_shared_mcp(bundle):
            self.p(line)
        self.a.yes = True  # already confirmed; the link plan prints next
        code = self.change("import_packs", {"bundle": bundle, "replace": getattr(self.a, "replace", False),
                                            "folder": folder}, "Import")
        repos = [r for r in pv["repos"]]
        if repos:
            self.p("missing skills to install (the app shows each SKILL.md and the safety check):")
            for r in repos:
                self.p(f"  {r['url']} @ {r['ref']}: {', '.join(r['dirs'])}")
        return code

    def c_import(self):
        from . import share
        bundle = share.parse(Path(self.a.file).read_text(encoding="utf8"))
        folder = str(Path(self.a.folder).resolve()) if self.a.folder else None
        return self.run_import(bundle, folder)

    def c_manifest(self):
        from . import share
        f = self.known(self.folder())
        if self.a.write:
            target = Path(f) / share.MANIFEST
            b = share.export(self.m, self.m.load(), self.a.write, self.a.write)
            self.p(f"write {target}" + (" (replaces the existing file)" if target.exists() else ""))
            stop = self.confirm()
            if stop is not None:
                return stop
            target.write_text(json.dumps(b, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
            self.p("done; commit it to share the packs")
            return 0
        m = share.read_manifest(f)
        if m is None:
            raise CliError(f"no {share.MANIFEST} in {f}")
        if "error" in m:
            raise CliError(m["error"])
        if self.a.apply:
            self.a.replace = False
            return self.run_import(m, f)
        summary = share.manifest_summary(self.m.load(), f)

        def text():
            self.p(f"{Path(f) / share.MANIFEST} asks for: {', '.join(m['attach']) or 'no packs'}")
            self.p("matches packs.json" if summary["done"] else "not applied yet: spm manifest --apply")
            self.show_import(share.preview(self.m, self.m.load(), m))
        return self.emit(summary | {"manifest": m}, text)

    def c_agents(self):
        if self.a.agent:
            if not self.a.state:
                raise CliError("say on, off or auto")
            if self.a.state == "auto" and self.a.agent != "gemini":
                raise CliError("auto exists for gemini only (on when ~/.gemini exists)")
            v = None if self.a.state == "auto" else self.a.state == "on"
            self.app.settings.update({"agents": {self.a.agent: v}})
            self.app.build()
            self.m = self.app.model
        on = list(self.m.agents)
        out = {a: a in on for a in ("claude", "opencode", "codex", "gemini")}

        def text():
            for a, v in out.items():
                self.p(f"{LABEL[a]:<12} {'on' if v else 'off'}")
            if self.a.agent and self.a.state == "off":
                self.p("run spm sync --all to remove what was added for it (it shows the plan first)")
        return self.emit(out, text)

    def c_router_test(self):
        if self.a.cases:
            text = Path(self.a.cases).read_text(encoding="utf8")
        elif self.a.write_cases:
            text = self.app.router_test("test-cases", {"pack": self.a.pack, "writer": self.a.writer})["text"]
            if not self.a.json:
                self.p("requests written by the AI:")
                for line in text.splitlines():
                    self.p("  " + line)
        else:
            text = self.app.router_test("test-cases", {"pack": self.a.pack, "saved": True})["text"]
            if not text:
                raise CliError("no saved requests; use --write-cases or --cases FILE")
        r = self.app.router_test("test", {"pack": self.a.pack, "text": text, "writer": self.a.writer})
        sm = r["summary"]

        def out():
            self.p(f"{sm['ok']}/{sm['cases']} right: opened {sm['opened']}/{sm['should_open']}, "
                   f"right skill {sm['picked']}/{sm['opened']}, stayed out {sm['stayed_out']}/{sm['off_topic']}")
            for x in r["results"]:
                went = "no answer" if not x["answered"] else x["chosen"] or ("opened, no skill" if x["opened"] else "did not open")
                self.p(f"  {'ok ' if x['ok'] else 'BAD'} {x['request'][:70]:<70}  {x['expected'] or 'stay out'} -> {went}")
        self.emit(r, out)
        return 0 if sm["ok"] == sm["cases"] else 3

    def confirm(self) -> int | None:
        """None to go on; an exit code to stop with."""
        if self.a.dry_run:
            return 0
        if self.a.yes:
            return None
        if not self.stdin.isatty():
            print("not applied: add --yes to apply", file=sys.stderr)
            return 2
        print("Apply? [y/N] ", end="", file=self.out, flush=True)
        if self.stdin.readline().strip().lower() not in ("y", "yes"):
            self.p("not applied")
            return 2
        return None

    def c_update(self):
        src, ws = self.app.local(), self.app.ws
        r = src.update_preview(ws, self.a.skill, self.a.ref)
        try:
            if self.a.json:
                self.p(json.dumps(r, indent=2, ensure_ascii=False))
            elif r["up_to_date"]:
                self.p(f"{self.a.skill} is up to date ({r['to']['ref']} {r['to']['commit'][:7]})")
            else:
                f, t = r["from"], r["to"]
                self.p(f"{self.a.skill}: {f['ref']} {(f['commit'] or '')[:7]} -> {t['ref']} {t['commit'][:7]}")
                for x in r["files"]:
                    size = "binary" if x["binary"] else f"+{x['lines'][0]} -{x['lines'][1]}"
                    self.p(f"  {x['status']:<8} {x['path']}  ({size})")
                    if self.a.diff and x["diff"]:
                        for line in x["diff"].splitlines():
                            self.p("      " + line)
                flagged = [x for x in r["new_findings"] if x["level"] != "info"]
                self.p(f"  new in this version: {len(flagged)} flagged line(s)" if flagged
                       else "  the check found nothing new (a pattern check, not a guarantee)")
                for x in flagged:
                    self.p(f"    [{x['level']}] {x['rule']}: {x['file']}:{x['line']}  {x['text'][:120]}")
            if r["up_to_date"]:
                return 0
            stop = self.confirm()
            if stop is not None:
                return stop
            self.p(src.update(ws, self.a.skill, cid=r["id"]))
            r["id"] = None  # used up
            return 0
        finally:
            if r.get("id"):
                ws.drop(r["id"])

    def c_rollback(self):
        src = self.app.local()
        versions = src.history(self.a.skill)
        if not self.a.version:
            def text():
                if not versions:
                    self.p(f"no earlier versions of {self.a.skill} are kept")
                for v in versions:
                    self.p(f"{v['id']}  {v['ref'] or v['local'] or '?'} {(v['commit'] or '')[:7]}")
            return self.emit(versions, text)
        if not any(v["id"] == self.a.version for v in versions):
            raise CliError(f"no version {self.a.version!r}; run: spm rollback {self.a.skill}")
        self.p(f"restore {self.a.skill} to {self.a.version} (the current version is kept and can be restored)")
        stop = self.confirm()
        if stop is not None:
            return stop
        self.p(src.rollback(self.a.skill, self.a.version))
        return 0


STATUS = {"ok": "linked", "missing": "not linked yet", "partial": "partly linked", "conflict": "name conflict",
          "stray": "linked, no pack wants it", "own": "project's own", "routed": "behind the router",
          "unknown": "not in the catalog"}


def changes(res: dict) -> int:
    return sum(len(p["add"]) + len(p["remove"]) + len(p["mcp_add"]) + len(p["mcp_remove"]) for p in res["plans"])


def plan_lines(res: dict) -> list[str]:
    out = list(res["warnings"])
    for p in res["plans"]:
        lines = []
        add = sorted({s for _, s, *_ in p["add"]})
        rem = sorted({s for _, s in p["remove"]})
        if add:
            lines.append(f"link {', '.join(add)}")
        if rem:
            lines.append(f"unlink {', '.join(rem)}")
        madd = sorted({f"{n} ({c})" for c, n in p["mcp_add"]})
        mrem = sorted({f"{n} ({c})" for c, n in p["mcp_remove"]})
        if madd:
            lines.append(f"add MCP {', '.join(madd)}")
        if mrem:
            lines.append(f"remove MCP {', '.join(mrem)}")
        lines += [f"{rel}/{s}: the project has its own {kind}, left as is" for rel, s, kind in p["conflicts"]]
        if p["missing"]:
            lines.append(f"not in the store, skipped: {', '.join(p['missing'])}")
        lines += p["warnings"] + p["mcp_warnings"]
        out.append(f"{p['folder']}: " + ("; ".join(lines) if lines else "no changes"))
    return out
