"""Where skills and MCP servers come from.

CcSwitchSource  - cc-switch owns the store (~/.cc-switch/skills) and the MCP
                  catalog (its SQLite database). Read-only here: installing,
                  global switches and MCP editing stay in cc-switch.
LocalSource     - standalone mode. This program owns:
                    <data>/store/skills/<dir>   skill folders
                    <data>/store/catalog.json   skill sources and MCP servers
                  and does what cc-switch would: install skills from git repos
                  (branch or release tag), update them, and enable skills and
                  MCP servers globally per client. Global skills are links from
                  the client's skill folder (~/.claude/skills, ...) into the
                  store, the same kind of link used inside projects.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from . import mcp, repo
from .osinfo import GLOBAL_SKILL_DIRS, restrict_file


class SourceError(Exception):
    pass


def read_frontmatter(skill_dir: Path) -> dict:
    try:
        return repo.frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf8", errors="replace"))
    except OSError:
        return {}


class CcSwitchSource:
    mode = "cc-switch"
    editable = False

    def __init__(self, store: Path, db: Path):
        self.store, self.db = store, db

    def skills(self) -> dict[str, dict]:
        rows = {}
        if self.db.exists():
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                cols = [r[1] for r in con.execute("pragma table_info(skills)")]
                for row in (con.execute("select * from skills").fetchall() if cols else []):
                    d = dict(zip(cols, row))
                    glob = [c for c in mcp.CLIENTS if d.get(f"enabled_{c}")]
                    rows[d["directory"]] = {"name": d["name"], "description": d.get("description") or "",
                                            "global": glob, "registered": True,
                                            "source": {"url": f"https://github.com/{d['repo_owner']}/{d['repo_name']}",
                                                       "ref": d.get("repo_branch")} if d.get("repo_owner") else None}
            finally:
                con.close()
        out = {}
        for p in sorted(self.store.iterdir()) if self.store.is_dir() else []:
            if (p / "SKILL.md").is_file():
                fm = read_frontmatter(p) if p.name not in rows else {}
                out[p.name] = rows.get(p.name) or {"name": fm.get("name") or p.name,
                                                   "description": fm.get("description", ""),
                                                   "global": [], "registered": False, "source": None}
        return out

    def mcp_catalog(self) -> mcp.Catalog:
        return mcp.Catalog.from_ccswitch(self.db)


class LocalSource:
    mode = "standalone"
    editable = True

    def __init__(self, root: Path, global_skill_dirs: dict[str, Path] | None = None,
                 global_mcp: dict | None = None):
        self.root = root
        self.store = root / "skills"
        self.catalog_file = root / "catalog.json"
        self.global_dirs = global_skill_dirs or GLOBAL_SKILL_DIRS
        self.global_mcp = global_mcp or {}
        self.store.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ catalog file

    def _load(self) -> dict:
        try:
            c = json.loads(self.catalog_file.read_text(encoding="utf8"))
        except (OSError, ValueError):
            c = {}
        c.setdefault("skills", {})
        c.setdefault("mcp", {})
        return c

    def _save(self, c: dict) -> None:
        tmp = self.catalog_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
        restrict_file(tmp)  # holds MCP env values
        os.replace(tmp, self.catalog_file)

    # ----------------------------------------------------------------- skills

    def _global_of(self, d: str) -> list[str]:
        from .core import classify  # local import: core imports this module
        out = []
        for client, gdir in self.global_dirs.items():
            e = gdir / d
            if os.path.lexists(e) and classify(e, self.store) == "managed":
                out.append(client)
        return out

    def skills(self) -> dict[str, dict]:
        meta = self._load()["skills"]
        out = {}
        for p in sorted(self.store.iterdir()):
            if (p / "SKILL.md").is_file():
                fm = read_frontmatter(p)
                m = meta.get(p.name, {})
                out[p.name] = {"name": fm.get("name") or p.name, "description": fm.get("description", ""),
                               "global": self._global_of(p.name), "registered": True,
                               "source": m.get("source"), "installed_at": m.get("installed_at"),
                               "updated_at": m.get("updated_at")}
        return out

    def set_skill_global(self, d: str, client: str, on: bool) -> str:
        from .core import classify, make_junction, safe_unlink
        if client not in self.global_dirs:
            raise SourceError(f"unknown client {client!r}")
        if not (self.store / d / "SKILL.md").is_file():
            raise SourceError(f"skill not in the store: {d!r}")
        e = self.global_dirs[client] / d
        if on:
            if os.path.lexists(e):
                if classify(e, self.store) == "managed":
                    return "already enabled"
                raise SourceError(f"{e} already exists and is not managed by this program; rename or remove it first")
            e.parent.mkdir(parents=True, exist_ok=True)
            make_junction(self.store / d, e)
            return f"linked {e}"
        if os.path.lexists(e):
            safe_unlink(e, self.store)
            return f"unlinked {e}"
        return "already disabled"

    def install(self, ws: repo.Workspace, cid: str, paths: list[str], replace: bool = False) -> list[str]:
        clone = ws.get(cid)
        by_path = {s["path"]: s for s in clone["skills"]}
        chosen = [by_path[p] for p in paths if p in by_path]
        if not chosen:
            raise SourceError("choose at least one skill")
        c = self._load()
        conflicts = []
        for s in chosen:
            if not re.match(r"^[A-Za-z0-9_.-]{1,80}$", s["dir"]) or s["dir"] in (".", ".."):
                raise SourceError(f"unsupported skill folder name {s['dir']!r}")
            existing = c["skills"].get(s["dir"], {}).get("source") or {}
            if (self.store / s["dir"]).exists() and not replace and \
                    (existing.get("url"), existing.get("path")) != (clone["url"], s["path"]):
                conflicts.append(s["dir"])
        if conflicts:
            raise SourceError("already in the store from another source: " + ", ".join(conflicts)
                              + ". Tick 'replace' to overwrite them.")
        log, now = [], int(time.time())
        for s in chosen:
            src = clone["path"] / s["path"] if s["path"] != "." else clone["path"]
            repo.copy_skill(src, self.store / s["dir"])
            prev = c["skills"].get(s["dir"], {})
            c["skills"][s["dir"]] = {"source": {"url": clone["url"], "ref": clone["ref"], "commit": clone["commit"],
                                                "path": s["path"]},
                                     "installed_at": prev.get("installed_at", now), "updated_at": now}
            log.append(f"installed {s['dir']} from {clone['url']}@{clone['ref']} ({clone['commit'][:7]})")
        self._save(c)
        ws.drop(cid)
        return log

    def import_folder(self, folder: str, replace: bool = False) -> str:
        src = Path(folder)
        if not (src / "SKILL.md").is_file():
            raise SourceError("that folder has no SKILL.md")
        d = src.name
        if (self.store / d).exists() and not replace:
            raise SourceError(f"{d!r} is already in the store")
        repo.copy_skill(src, self.store / d)
        c = self._load()
        now = int(time.time())
        c["skills"][d] = {"source": {"local": str(src)}, "installed_at": now, "updated_at": now}
        self._save(c)
        return f"imported {d} from {src}"

    def update(self, ws: repo.Workspace, d: str, ref: str | None = None) -> str:
        c = self._load()
        src = (c["skills"].get(d) or {}).get("source") or {}
        if not src.get("url"):
            raise SourceError(f"{d!r} was not installed from a repository")
        ref = ref or src["ref"]
        clone = ws.clone(src["url"], ref)
        try:
            match = next((s for s in clone["skills"] if s["path"] == src["path"]), None)
            if not match:
                raise SourceError(f"{src['path']} has no SKILL.md at {ref}")
            base = ws.get(clone["id"])["path"]
            folder = base if src["path"] == "." else base / src["path"]
            if clone["commit"] == src.get("commit") and ref == src.get("ref"):
                return f"{d} is up to date ({ref} {clone['commit'][:7]})"
            repo.copy_skill(folder, self.store / d)
        finally:
            ws.drop(clone["id"])
        c["skills"][d]["source"].update(ref=ref, commit=clone["commit"])
        c["skills"][d]["updated_at"] = int(time.time())
        self._save(c)
        return f"updated {d} to {ref} ({clone['commit'][:7]})"

    def check_updates(self) -> dict[str, dict]:
        meta = self._load()["skills"]
        cache: dict[str, dict] = {}
        out = {}
        for d, m in meta.items():
            src = m.get("source") or {}
            if not src.get("url"):
                continue
            try:
                info = cache.get(src["url"]) or repo.refs(src["url"])
            except repo.RepoError as e:
                out[d] = {"error": str(e)}
                continue
            cache[src["url"]] = info
            cur = next((r for r in info["refs"] if r["ref"] == src["ref"]), None)
            tags = [r["ref"] for r in info["refs"] if r["kind"] == "tag"]
            newer = []
            if src["ref"] in tags:
                newer = [t for t in tags if repo.version_key(t) > repo.version_key(src["ref"])]
            out[d] = {"ref": src["ref"], "commit": src.get("commit"),
                      "branch_moved": bool(cur and cur["kind"] == "branch" and cur["commit"] != src.get("commit")),
                      "newer_tags": newer[:10]}
        return out

    def remove_skill(self, d: str, packs_data: dict) -> str:
        used = [n for n, p in packs_data.get("packs", {}).items() if d in p.get("skills", [])]
        used += [f"folder {k}" for k, v in packs_data.get("folders", {}).items() if d in v.get("skills", [])]
        if used:
            raise SourceError(f"{d!r} is still used by: {', '.join(used)}")
        if self._global_of(d):
            raise SourceError(f"{d!r} is enabled globally; turn that off first")
        target = self.store / d
        if os.path.islink(target) or os.path.isjunction(target) or not target.is_dir():
            raise SourceError("not a skill folder in the store")
        shutil.rmtree(target)
        c = self._load()
        c["skills"].pop(d, None)
        self._save(c)
        return f"removed {d} from the store"

    # -------------------------------------------------------------------- MCP

    def mcp_catalog(self) -> mcp.Catalog:
        servers = {}
        globals_ = {c: a.read(None) for c, a in self.global_mcp.items()}
        for name, s in self._load()["mcp"].items():
            servers[name] = {"config": s["config"], "description": s.get("description", ""),
                             "global": [c for c, entries in globals_.items() if name in entries]}
        return mcp.Catalog(servers)

    @staticmethod
    def normalize(name: str, cfg: dict) -> dict:
        if not mcp.NAME.match(name or ""):
            raise SourceError("MCP names use letters, digits, '.', '_' and '-' (max 64)")
        if not isinstance(cfg, dict):
            raise SourceError(f"{name}: the server definition must be an object")
        out: dict = {}
        if cfg.get("url"):
            if not re.match(r"^https?://", str(cfg["url"])):
                raise SourceError(f"{name}: url must start with http:// or https://")
            out["type"] = "sse" if cfg.get("type") == "sse" else "http"
            out["url"] = str(cfg["url"])
            hdr = cfg.get("headers") or {}
            if not isinstance(hdr, dict):
                raise SourceError(f"{name}: headers must be an object")
            if hdr:
                out["headers"] = {str(k): str(v) for k, v in hdr.items()}
        elif cfg.get("command"):
            cmd = cfg["command"]
            args = cfg.get("args") or []
            if isinstance(cmd, list):  # OpenCode style ["cmd", "arg", ...]
                cmd, args = cmd[0], [*cmd[1:], *args]
            if not isinstance(args, list):
                raise SourceError(f"{name}: args must be a list")
            out["type"] = "stdio"
            out["command"] = str(cmd)
            out["args"] = [str(a) for a in args]
            env = cfg.get("env") or cfg.get("environment") or {}
            if not isinstance(env, dict):
                raise SourceError(f"{name}: env must be an object")
            if env:
                out["env"] = {str(k): str(v) for k, v in env.items()}
        else:
            raise SourceError(f"{name}: needs a command (stdio) or a url (http/sse)")
        return out

    @staticmethod
    def parse_import(text: str) -> dict[str, dict]:
        """Accepts {"mcpServers": {...}}, {"mcp": {...}} (OpenCode) or {name: cfg}."""
        try:
            j = json.loads(text)
        except ValueError as e:
            raise SourceError(f"not valid JSON: {e}") from e
        if not isinstance(j, dict):
            raise SourceError("expected a JSON object")
        servers = j.get("mcpServers") or j.get("mcp_servers") or j.get("mcp") or j
        if not isinstance(servers, dict) or not servers:
            raise SourceError("no servers found")
        return {name: LocalSource.normalize(name, cfg) for name, cfg in servers.items()}

    def save_mcp(self, name: str, cfg: dict, description: str = "", keep_env: list[str] | None = None,
                 keep_headers: list[str] | None = None, new: bool = True) -> str:
        c = self._load()
        if new and name in c["mcp"]:
            raise SourceError(f"{name!r} already exists")
        if not new and name not in c["mcp"]:
            raise SourceError(f"no MCP server {name!r}")
        cfg = self.normalize(name, cfg)
        if not new:  # keep secret values the UI never saw
            old = c["mcp"][name]["config"]
            for key, field in (("env", keep_env), ("headers", keep_headers)):
                for k in field or []:
                    if k in (old.get(key) or {}) and k not in (cfg.get(key) or {}):
                        cfg.setdefault(key, {})[k] = old[key][k]
        c["mcp"][name] = {"config": cfg, "description": description[:500]}
        self._save(c)
        return f"{'added' if new else 'saved'} MCP {name}"

    def delete_mcp(self, name: str, packs_data: dict) -> str:
        used = [n for n, p in packs_data.get("packs", {}).items() if name in p.get("mcp", [])]
        used += [f"folder {k}" for k, v in packs_data.get("folders", {}).items() if name in v.get("mcp", [])]
        if used:
            raise SourceError(f"{name!r} is still used by: {', '.join(used)}")
        if self.mcp_catalog().servers.get(name, {}).get("global"):
            raise SourceError(f"{name!r} is enabled globally; turn that off first")
        c = self._load()
        if c["mcp"].pop(name, None) is None:
            raise SourceError(f"no MCP server {name!r}")
        self._save(c)
        return f"deleted MCP {name}"

    def set_mcp_global(self, name: str, client: str, on: bool) -> str:
        c = self._load()
        if name not in c["mcp"]:
            raise SourceError(f"no MCP server {name!r}")
        a = self.global_mcp.get(client)
        if not a:
            raise SourceError(f"unknown client {client!r}")
        present = name in a.read(None)
        if on:
            why = a.supports(c["mcp"][name]["config"])
            if why:
                raise SourceError(why)
            if present:  # refresh the entry; `claude mcp add-json` refuses an existing name
                a.remove(None, name)
            a.add(None, name, c["mcp"][name]["config"])
            return f"MCP {name} enabled globally for {client}"
        if present:
            a.remove(None, name)
        return f"MCP {name} disabled globally for {client}"
