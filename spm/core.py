"""Model and file operations for skill packs.

packs.json holds the desired state:
  packs.<name>.skills  - skill folder names in the store
  packs.<name>.mcp     - MCP server names from the catalog (see spm/mcp.py)
  packs.<name>.paths   - project folders that get the pack
  folders.<path>.skills - extra single skills for one folder
  folders.<path>.mcp    - extra single MCP servers for one folder

The store and the MCP catalog come from a source (spm/sources.py): cc-switch,
or this program's own store in standalone mode.

The actual state is the set of links in <folder>/.claude/skills,
<folder>/.opencode/skills and <folder>/.agents/skills (junctions on Windows,
symlinks elsewhere). A link whose target is a direct child of the store is
"managed": this tool may create or remove it. Real folders and links that point
elsewhere belong to the project and are never touched.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import mcp, usage
from .osinfo import APP_ROOT, HOME, NO_WINDOW, PROJECT_SKILL_DIRS as SKILL_DIRS, claude_cli, data_dir, default_scan_roots
from .sources import CcSwitchSource

EXTRA = "+extra"
PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class UnsafeRemoval(Exception):
    """Raised when a path is not a managed junction and must not be removed."""


class OpError(Exception):
    """Raised when an operation's arguments are invalid."""


@dataclass
class Config:
    store: Path       # cc-switch skill store (used in cc-switch mode)
    packs: Path
    db: Path          # cc-switch database (used in cc-switch mode)
    data_dir: Path
    scan_roots: list[Path] = field(default_factory=list)
    claude_json: Path = HOME / ".claude.json"
    claude_cli: Path = field(default_factory=claude_cli)
    codex_global: Path = HOME / ".codex" / "config.toml"
    opencode_dir: Path = HOME / ".config" / "opencode"
    home: Path = HOME  # where agent session logs are read for usage counts

    @classmethod
    def default(cls, scan_roots: list[str] | None = None) -> "Config":
        env = os.environ.get
        if env("SPM_SCAN_ROOTS"):
            scan_roots = env("SPM_SCAN_ROOTS").split(os.pathsep)
        # SPM_HOME points every agent folder at a sandbox (tests, demos). Claude
        # entries still go through the real `claude` CLI.
        home = Path(env("SPM_HOME", HOME))
        return cls(
            store=Path(env("SPM_STORE", home / ".cc-switch" / "skills")),
            packs=Path(env("SPM_PACKS", APP_ROOT / "packs.json")),
            db=Path(env("SPM_DB", home / ".cc-switch" / "cc-switch.db")),
            data_dir=data_dir(),
            scan_roots=[Path(r) for r in (scan_roots if scan_roots is not None else default_scan_roots()) if r.strip()],
            claude_json=Path(env("SPM_CLAUDE_JSON", home / ".claude.json")),
            claude_cli=claude_cli(),
            codex_global=Path(env("SPM_CODEX_GLOBAL", home / ".codex" / "config.toml")),
            opencode_dir=Path(env("SPM_OPENCODE_DIR", home / ".config" / "opencode")),
            home=home,
        )


# ---------------------------------------------------------------- path helpers

def norm(p) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def is_within(child, parent) -> bool:
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def is_link(p: Path) -> bool:
    return os.path.isjunction(p) or os.path.islink(p)


def classify(entry: Path, store: Path) -> str:
    """managed | foreign | own"""
    if is_link(entry):
        target = os.path.realpath(entry)
        if is_within(target, store) and norm(Path(target).parent) == norm(store):
            return "managed"
        return "foreign"
    return "own"


def make_junction(src: Path, dst: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
    else:  # used only by tests on other systems
        os.symlink(src, dst, target_is_directory=True)


def safe_unlink(entry: Path, store: Path) -> None:
    """Remove a managed junction without touching its target.

    Refuses anything that is not a link into the store. os.rmdir removes a
    junction's reparse point only; on a real non-empty folder it fails, which is
    a second guard against deleting skill content.
    """
    if not is_link(entry):
        raise UnsafeRemoval(f"not a link: {entry}")
    if classify(entry, store) != "managed":
        raise UnsafeRemoval(f"link does not point into the store: {entry}")
    if os.path.isjunction(entry):
        os.rmdir(entry)
    else:
        os.unlink(entry)


# ------------------------------------------------------------- git excludes

def exclude_file(folder: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=folder,
                           capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else folder / p


def update_excludes(folder: Path, add: set[str], remove: set[str]) -> None:
    ex = exclude_file(folder)
    if ex is None:
        return
    lines = ex.read_text(encoding="utf8").splitlines() if ex.exists() else []
    have = set(lines)
    lines = [ln for ln in lines if ln not in remove]
    new = sorted(add - have)
    if new:
        lines += ["", "# skill-pack-manager"] + new
    if new or remove & have:
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text("\n".join(lines) + "\n", encoding="utf8")


# ------------------------------------------------------------- packs.json data

def pack_folders(data: dict, pack: str) -> list[str]:
    return list(data["packs"][pack]["paths"])


def all_folders(data: dict) -> list[str]:
    seen: dict[str, str] = {}
    for p in data["packs"].values():
        for path in p["paths"]:
            seen.setdefault(norm(path), path)
    for path in data.get("folders", {}):
        seen.setdefault(norm(path), path)
    return sorted(seen.values(), key=str.lower)


def packs_of(data: dict, folder) -> list[str]:
    nf = norm(folder)
    return [n for n, p in data["packs"].items() if any(norm(x) == nf for x in p["paths"])]


def extras_of(data: dict, folder) -> list[str]:
    nf = norm(folder)
    for path, v in data.get("folders", {}).items():
        if norm(path) == nf:
            return list(v.get("skills", []))
    return []


def mcp_extras_of(data: dict, folder) -> list[str]:
    nf = norm(folder)
    for path, v in data.get("folders", {}).items():
        if norm(path) == nf:
            return list(v.get("mcp", []))
    return []


def desired(data: dict, folder) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name in packs_of(data, folder):
        for s in data["packs"][name]["skills"]:
            out.setdefault(s, []).append(name)
    for s in extras_of(data, folder):
        out.setdefault(s, []).append(EXTRA)
    return out


def actual(folder: Path, store: Path) -> dict[str, dict[str, str]]:
    """skill -> {skill dir: managed|foreign|own}"""
    out: dict[str, dict[str, str]] = {}
    for rel in SKILL_DIRS:
        d = folder / rel
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            if entry.is_dir() or is_link(entry):
                out.setdefault(entry.name, {})[rel] = classify(entry, store)
    return out


def plan(data: dict, folder, store: Path) -> dict:
    """Junctions to add and remove so the folder matches packs.json."""
    folder = Path(folder)
    want = desired(data, folder)
    res = {"folder": str(folder), "add": [], "remove": [], "conflicts": [], "missing": [], "warnings": []}
    if not folder.is_dir():
        res["warnings"].append("folder does not exist; nothing will be linked")
        return res
    have = actual(folder, store)
    for s in sorted(want):
        if not (store / s / "SKILL.md").is_file():
            res["missing"].append(s)
            continue
        for rel in SKILL_DIRS:
            kind = have.get(s, {}).get(rel)
            if kind is None:
                res["add"].append([rel, s])
            elif kind != "managed":
                res["conflicts"].append([rel, s, kind])
    for s, places in sorted(have.items()):
        if s in want:
            continue
        for rel, kind in places.items():
            if kind == "managed":
                res["remove"].append([rel, s])
    return res


def apply_plan(p: dict, store: Path) -> list[str]:
    folder = Path(p["folder"])
    log, add_ex, rm_ex = [], set(), set()
    for rel, s in p["add"]:
        (folder / rel).mkdir(parents=True, exist_ok=True)
        dst = folder / rel / s
        if dst.exists() or is_link(dst):
            continue
        make_junction(store / s, dst)
        add_ex.add(f"/{rel}/{s}")
        log.append(f"linked {rel}/{s}")
    for rel, s in p["remove"]:
        dst = folder / rel / s
        if not is_link(dst):
            continue
        safe_unlink(dst, store)
        rm_ex.add(f"/{rel}/{s}")
        log.append(f"unlinked {rel}/{s}")
    if add_ex or rm_ex:
        update_excludes(folder, add_ex, rm_ex)
    return log


# ------------------------------------------------------------------ operations

def _check_skill(store: Path, skill: str) -> None:
    if not skill or "/" in skill or "\\" in skill or not (store / skill / "SKILL.md").is_file():
        raise OpError(f"skill not in the store: {skill!r}")


def _check_pack(data: dict, pack: str) -> dict:
    if pack not in data["packs"]:
        raise OpError(f"no such pack: {pack!r}")
    return data["packs"][pack]


def _check_folder(folder: str) -> str:
    if not folder or not Path(folder).is_dir():
        raise OpError(f"folder does not exist: {folder!r}")
    return os.path.normpath(folder)


def _drop_path(paths: list[str], folder) -> bool:
    nf = norm(folder)
    keep = [p for p in paths if norm(p) != nf]
    changed = len(keep) != len(paths)
    paths[:] = keep
    return changed


def _folder_entry(data: dict, folder) -> str | None:
    return next((k for k in data.get("folders", {}) if norm(k) == norm(folder)), None)


def _prune_folder(data: dict, key: str) -> None:
    entry = data["folders"][key]
    for kind in ("skills", "mcp"):
        if kind in entry and not entry[kind]:
            del entry[kind]
    if not entry:
        del data["folders"][key]


def mutate(data: dict, store: Path, op: str, a: dict, catalog=None) -> list[str]:
    """Change data in place; return the folders whose links may change."""
    packs = data["packs"]
    if op in ("pack_add_mcp", "pack_remove_mcp", "mcp_extra_add", "mcp_extra_remove"):
        name = a.get("mcp", "")
        if op in ("pack_add_mcp", "mcp_extra_add") and (catalog is None or name not in catalog):
            raise OpError(f"MCP server not in cc-switch: {name!r}")
        if op.startswith("pack_"):
            p = _check_pack(data, a["pack"])
            lst = p.setdefault("mcp", [])
            if op == "pack_add_mcp":
                if name not in lst:
                    lst.append(name)
            else:
                if name not in lst:
                    raise OpError(f"{name!r} is not in pack {a['pack']!r}")
                lst.remove(name)
            return list(p["paths"])
        folders = data.setdefault("folders", {})
        key = _folder_entry(data, a["folder"])
        if op == "mcp_extra_add":
            folder = _check_folder(a["folder"])
            key = key or folder
            lst = folders.setdefault(key, {}).setdefault("mcp", [])
            if name not in lst:
                lst.append(name)
            return [key]
        if key is None or name not in folders[key].get("mcp", []):
            raise OpError(f"{name!r} is not an extra MCP server of that folder")
        folders[key]["mcp"].remove(name)
        _prune_folder(data, key)
        return [a["folder"]]
    if op == "create_pack":
        name = a.get("pack", "")
        if not PACK_NAME.match(name):
            raise OpError("pack names use lowercase letters, digits and dashes")
        if name in packs:
            raise OpError(f"pack {name!r} already exists")
        packs[name] = {"skills": [], "mcp": [], "paths": []}
        return []
    if op == "delete_pack":
        p = _check_pack(data, a["pack"])
        folders = list(p["paths"])
        del packs[a["pack"]]
        return folders
    if op in ("pack_add_skill", "pack_remove_skill"):
        p = _check_pack(data, a["pack"])
        s = a["skill"]
        if op == "pack_add_skill":
            _check_skill(store, s)
            if s not in p["skills"]:
                p["skills"].append(s)
        else:
            if s not in p["skills"]:
                raise OpError(f"{s!r} is not in pack {a['pack']!r}")
            p["skills"].remove(s)
        return list(p["paths"])
    if op == "attach":
        p = _check_pack(data, a["pack"])
        folder = _check_folder(a["folder"])
        if not any(norm(x) == norm(folder) for x in p["paths"]):
            p["paths"].append(folder)
        return [folder]
    if op == "detach":
        p = _check_pack(data, a["pack"])
        if not _drop_path(p["paths"], a["folder"]):
            raise OpError("that folder is not attached to the pack")
        return [a["folder"]]
    if op in ("extra_add", "extra_remove"):
        s = a["skill"]
        folders = data.setdefault("folders", {})
        key = _folder_entry(data, a["folder"])
        if op == "extra_add":
            _check_skill(store, s)
            folder = _check_folder(a["folder"])
            key = key or folder
            lst = folders.setdefault(key, {}).setdefault("skills", [])
            if s not in lst:
                lst.append(s)
            return [key]
        if key is None or s not in folders[key].get("skills", []):
            raise OpError(f"{s!r} is not an extra skill of that folder")
        folders[key]["skills"].remove(s)
        _prune_folder(data, key)
        return [a["folder"]]
    if op == "sync":
        return [a["folder"]]
    raise OpError(f"unknown operation {op!r}")


# ----------------------------------------------------------------------- model

class Model:
    def __init__(self, cfg: Config, source=None):
        self.cfg = cfg
        self.source = source or CcSwitchSource(cfg.store, cfg.db)
        self.store = self.source.store
        self.lock = threading.Lock()
        self.adapters = mcp.default_adapters(cfg.claude_json, cfg.claude_cli, cfg.codex_global)

    def load(self) -> dict:
        try:
            data = json.loads(self.cfg.packs.read_text(encoding="utf8"))
        except FileNotFoundError:
            data = {}
        data.setdefault("packs", {})
        # "~/Projects/x" works in packs.json; it is stored expanded after the next save
        for p in data["packs"].values():
            p["paths"] = [os.path.expanduser(x) for x in p.get("paths", [])]
            p.setdefault("skills", [])
        if "folders" in data:
            data["folders"] = {os.path.expanduser(k): v for k, v in data["folders"].items()}
        return data

    def save(self, data: dict) -> None:
        if not data.get("folders"):
            data.pop("folders", None)
        if self.cfg.packs.exists():
            bk = self.cfg.data_dir / "backups"
            bk.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.cfg.packs, bk / f"packs-{time.strftime('%Y%m%d-%H%M%S')}.json")
            for old in sorted(bk.glob("packs-*.json"))[:-50]:
                old.unlink()
        self.cfg.packs.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.packs.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
        os.replace(tmp, self.cfg.packs)

    def mcp_catalog(self) -> mcp.Catalog:
        return self.source.mcp_catalog()

    def full_plan(self, data: dict, folder, catalog: mcp.Catalog) -> dict:
        return plan(data, folder, self.store) | mcp.plan(data, folder, catalog, self.adapters)

    def apply_full(self, p: dict, catalog: mcp.Catalog) -> list[str]:
        folder = Path(p["folder"])
        log = apply_plan(p, self.store)
        if p["mcp_add"] or p["mcp_remove"]:
            mlog, created = mcp.apply(p, folder, catalog, self.adapters)
            log += mlog
            if created:
                update_excludes(folder, created, set())
        return log

    def run(self, op: str, args: dict, dry: bool = True) -> dict:
        with self.lock:
            catalog = self.mcp_catalog()
            before = self.load()
            after = copy.deepcopy(before)
            touched = mutate(after, self.store, op, args, catalog)
            seen, folders = set(), []
            for f in touched:
                if norm(f) not in seen:
                    seen.add(norm(f))
                    folders.append(f)
            plans = [self.full_plan(after, f, catalog) for f in folders]
            res = {"op": op, "dry": dry, "plans": plans, "warnings": [], "log": [], "errors": []}
            if dry:
                return res
            if after != before:
                self.save(after)
            for p in plans:
                try:
                    res["log"] += [f"{p['folder']}: {line}" for line in self.apply_full(p, catalog)]
                except (mcp.McpError, OSError) as e:
                    # packs.json is saved; a later Sync retries what is missing
                    res["errors"].append(f"{p['folder']}: {e}")
            return res

    # ---------------------------------------------------------------- views

    def catalog(self, data: dict) -> list[dict]:
        try:
            rows = self.source.skills()
        except sqlite3.Error:
            rows = {}
        in_packs: dict[str, list[str]] = {}
        for name, p in data["packs"].items():
            for s in p["skills"]:
                in_packs.setdefault(s, []).append(name)
        extra_count: dict[str, int] = {}
        for v in data.get("folders", {}).values():
            for s in v.get("skills", []):
                extra_count[s] = extra_count.get(s, 0) + 1
        out = []
        for d, r in sorted(rows.items()):
            out.append({"dir": d, "name": r["name"], "description": (r.get("description") or "")[:400],
                        "core": bool(r.get("global")), "global": r.get("global", []),
                        "source": r.get("source"), "updated_at": r.get("updated_at"),
                        "packs": in_packs.get(d, []), "extra_folders": extra_count.get(d, 0),
                        "registered": r.get("registered", True)})
        return out

    def folder_view(self, data: dict, folder: str, catalog: mcp.Catalog | None = None) -> dict:
        f = Path(folder)
        want = desired(data, f)
        have = actual(f, self.store) if f.is_dir() else {}
        skills = []
        for s in sorted(set(want) | set(have)):
            places = have.get(s, {})
            managed = [r for r, k in places.items() if k == "managed"]
            if s in want:
                status = "ok" if len(managed) == len(SKILL_DIRS) else "partial" if managed else "missing"
                if any(k != "managed" for k in places.values()):
                    status = "conflict"
            elif managed:
                status = "stray"
            else:
                status = "own"
            skills.append({"skill": s, "sources": want.get(s, []), "status": status,
                           "dirs": places})
        catalog = catalog or self.mcp_catalog()
        p = self.full_plan(data, f, catalog)
        return {"folder": folder, "exists": f.is_dir(), "packs": packs_of(data, f),
                "extras": extras_of(data, f), "mcp_extras": mcp_extras_of(data, f), "skills": skills,
                "mcp": self.mcp_view(data, f, catalog, p),
                "in_sync": not (p["add"] or p["remove"] or p["mcp_add"] or p["mcp_remove"]), "plan": p}

    def mcp_view(self, data: dict, f: Path, catalog: mcp.Catalog, p: dict) -> list[dict]:
        if not f.is_dir():
            return []
        want = mcp.desired(data, f)
        have = mcp.actual(f, catalog, self.adapters)
        skipped = {(w.split(" skipped for ")[0], w.split(" skipped for ")[1].split(":")[0])
                   for w in p["mcp_warnings"] if " skipped for " in w}
        out = []
        for name in sorted(set(want) | set(have)):
            clients = have.get(name, {})
            managed = [c for c, k in clients.items() if k == "managed"]
            expected = [c for c in mcp.CLIENTS if (name, c) not in skipped]
            if name in want:
                if name not in catalog:
                    status = "unknown"
                elif any(k != "managed" for k in clients.values()):
                    status = "conflict"
                else:
                    status = "ok" if set(expected) <= set(managed) else "partial" if managed else "missing"
            elif managed:
                status = "stray"
            else:
                status = "own"
            out.append({"name": name, "sources": want.get(name, []), "status": status, "clients": clients,
                        "global": catalog.servers[name]["global"] if name in catalog else []})
        return out

    def state(self) -> dict:
        data = self.load()
        catalog = self.mcp_catalog()
        folders = []
        for path in all_folders(data):
            v = self.folder_view(data, path, catalog)
            counts: dict[str, int] = {}
            for s in v["skills"] + v["mcp"]:
                counts[s["status"]] = counts.get(s["status"], 0) + 1
            folders.append({"folder": path, "exists": v["exists"], "packs": v["packs"],
                            "extras": v["extras"], "mcp_extras": v["mcp_extras"],
                            "mcp": [m["name"] for m in v["mcp"] if m["sources"]],
                            "in_sync": v["in_sync"], "counts": counts})
        packs = [{"name": n, "skills": p["skills"], "mcp": p.get("mcp", []), "paths": p["paths"]}
                 for n, p in data["packs"].items()]
        in_packs: dict[str, list[str]] = {}
        for n, p in data["packs"].items():
            for m in p.get("mcp", []):
                in_packs.setdefault(m, []).append(n)
        extra_count: dict[str, int] = {}
        for v in data.get("folders", {}).values():
            for m in v.get("mcp", []):
                extra_count[m] = extra_count.get(m, 0) + 1
        servers = [catalog.public(n) | {"packs": in_packs.get(n, []), "extra_folders": extra_count.get(n, 0)}
                   for n in sorted(catalog.servers, key=str.lower)]
        return {"packs": packs, "folders": folders, "skills": self.catalog(data), "mcp": servers,
                "mode": self.source.mode, "editable": self.source.editable,
                "config": {"packs": str(self.cfg.packs), "store": str(self.store),
                           "data": str(self.cfg.data_dir),
                           "scan_roots": [str(r) for r in self.cfg.scan_roots]}}

    # ----------------------------------------------------------------- scan

    def scan(self, max_depth: int = 3) -> list[dict]:
        data = self.load()
        skip = {"node_modules", "Library", "Temp", "Logs", "obj", "bin", "Build", "Builds",
                "Intermediate", "Saved", "DerivedDataCache", "Binaries", "Packages", "venv"}
        found, seen = [], set()

        def kind_of(d: Path) -> str | None:
            if (d / "ProjectSettings" / "ProjectVersion.txt").is_file():
                return "unity"
            try:
                if any(x.suffix == ".uproject" for x in d.iterdir() if x.is_file()):
                    return "unreal"
            except OSError:
                return None
            return None

        def has_links(d: Path) -> bool:
            for rel in SKILL_DIRS:
                sd = d / rel
                if sd.is_dir():
                    try:
                        if any(classify(e, self.store) == "managed" for e in sd.iterdir()):
                            return True
                    except OSError:
                        pass
            return False

        def walk(d: Path, depth: int):
            try:
                children = [c for c in d.iterdir() if c.is_dir() and not is_link(c)]
            except OSError:
                return
            for c in children:
                if c.name.startswith(".") or c.name in skip:
                    continue
                k = kind_of(c)
                links = has_links(c)
                if k or links:
                    # a Unity project in <repo>\client belongs to the repo root,
                    # where agents start
                    root = c if (c / ".git").exists() or not (c.parent / ".git").exists() else c.parent
                    if norm(root) in seen:
                        continue
                    seen.add(norm(root))
                    found.append({"folder": str(root), "kind": k or "other",
                                  "subdir": c.name if root != c else None,
                                  "worktree": (root / ".git").is_file(),
                                  "packs": packs_of(data, root), "extras": extras_of(data, root),
                                  "has_links": links or has_links(root)})
                if not k and depth < max_depth:
                    walk(c, depth + 1)

        for root in self.cfg.scan_roots:
            if root.is_dir():
                walk(root, 1)
        return sorted(found, key=lambda x: x["folder"].lower())

    # ---------------------------------------------------------------- usage

    def usage(self) -> dict:
        """Count skill use in past Claude Code, OpenCode and Codex sessions."""
        skills = self.source.skills()
        by_name = {r["name"]: d for d, r in skills.items()}
        names = set(skills) | set(by_name)
        raw = usage.collect(names, self.cfg.home)
        out: dict[str, dict] = {}
        for name, u in raw.items():
            d = name if name in skills else by_name.get(name)
            if not d:
                continue
            cur = out.setdefault(d, {"uses": 0, "last": "", "where": []})
            cur["uses"] += u["uses"]
            cur["last"] = max(cur["last"], u["last"])
            cur["where"] = (cur["where"] + u["where"])[:3]
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.data_dir / "usage.json").write_text(json.dumps(out), encoding="utf8")
        return out

    def cached_usage(self) -> dict:
        f = self.cfg.data_dir / "usage.json"
        try:
            return json.loads(f.read_text(encoding="utf8")) | {"_at": f.stat().st_mtime}
        except (OSError, ValueError):
            return {}
