"""Command line: the same operations as the window, for scripts, git hooks
and agents.

  spm status [FOLDER]            folders in packs.json, or one folder in detail
  spm sync [FOLDER | --all]      make folders match packs.json
  spm attach PACK [FOLDER]       attach a pack (FOLDER defaults to .)
  spm detach PACK [FOLDER]
  spm extra add|remove SKILL [FOLDER]
  spm pack add|remove PACK SKILL...
  spm packs                      packs with their skills, MCP servers, folders
  spm skills [QUERY]             skills in the store
  spm tokens [FOLDER]            estimated context cost per agent
  spm usage [--count]            skill use from past sessions
  spm suggest [--days N]         cleanup suggestions
  spm scan                       projects under the scan roots, with pack suggestions
  spm check-updates              standalone mode: newer commits or tags

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
            "scan", "check-updates")
LABEL = {"claude": "Claude Code", "opencode": "OpenCode", "codex": "Codex"}


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
    p = cmd("pack", "add skills to a pack or remove them", change=True)
    p.add_argument("action", choices=("add", "remove"))
    p.add_argument("pack")
    p.add_argument("skills", nargs="+")
    cmd("packs", "list packs")
    cmd("skills", "list skills in the store").add_argument("query", nargs="?", default="")
    cmd("tokens", "estimated context cost per agent", folder=True)
    cmd("usage", "skill use from past sessions").add_argument("--count", action="store_true",
                                                              help="read the session logs again first")
    cmd("suggest", "cleanup suggestions").add_argument("--days", type=int, default=60)
    cmd("scan", "projects under the scan roots")
    cmd("check-updates", "newer commits or tags for installed skills (standalone)")
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
                self.p(f"{state:<12} {r['folder']}  [{', '.join(r['packs']) or 'no pack'}]"
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
