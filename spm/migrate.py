"""Migrate from cc-switch to standalone mode.

Steps (each one previewed first):
  1. skills   copy every skill folder from the cc-switch store into this
              program's store, keeping its source (owner/repo, branch, path)
              so updates keep working
  2. mcp      copy every MCP server definition into the catalog
  3. relink   project links that point into the cc-switch store are re-pointed
              to the new store, so packs keep working
  4. global   skills cc-switch enabled for Claude, OpenCode or Codex: its copies
              in the agents' skill folders are moved to a backup folder and
              replaced with links into the new store

Global MCP entries need no step: cc-switch already wrote them into the agents'
configs under the same names, and the catalog recognizes them by name.
Agents cc-switch manages that this program does not (Gemini, Cursor, ...) are
left alone. cc-switch must not be running: it rewrites the agents' folders.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import repo
from .core import SKILL_DIRS, all_folders, classify, is_link, make_junction, norm, safe_unlink
from .mcp import CLIENTS
from .osinfo import process_running
from .sources import CcSwitchSource, LocalSource, SourceError, ccswitch_source

CCSWITCH_PROCESSES = ("cc-switch", "cc-switch.exe", "CC Switch")


def _ccswitch_rows(db: Path) -> tuple[dict, list[str]]:
    """skill dir -> {source, enabled: [clients]}; plus other agent columns in use."""
    rows, others = {}, set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in con.execute("pragma table_info(skills)")]
        for r in (con.execute("select * from skills").fetchall() if cols else []):
            d = dict(zip(cols, r))
            src = ccswitch_source(d)
            enabled = [c for c in CLIENTS if d.get(f"enabled_{c}")]
            others |= {k[8:] for k, v in d.items() if k.startswith("enabled_") and v and k[8:] not in CLIENTS}
            rows[d["directory"]] = {"source": src, "enabled": enabled}
    finally:
        con.close()
    return rows, sorted(others)


def plan(cc: CcSwitchSource, local: LocalSource, packs_data: dict, replace: bool = False) -> dict:
    if not cc.db.exists():
        raise SourceError("cc-switch was not found on this computer")
    rows, others = _ccswitch_rows(cc.db)
    meta = local._load()
    p = {"skills_copy": [], "skills_replace": [], "skills_skip": [], "skills_done": [],
         "mcp_copy": [], "mcp_skip": [], "mcp_invalid": [], "relink": [], "global": [],
         "other_agents": others, "ccswitch_running": process_running(CCSWITCH_PROCESSES), "warnings": []}

    store_dirs = sorted(x.name for x in cc.store.iterdir() if (x / "SKILL.md").is_file()) if cc.store.is_dir() else []
    for d in store_dirs:
        if (local.store / d).exists():
            if (meta["skills"].get(d) or {}).get("migrated_from") == "cc-switch":
                p["skills_done"].append(d)
            elif replace:
                p["skills_replace"].append(d)
            else:
                p["skills_skip"].append(d)
        else:
            p["skills_copy"].append(d)

    for name, s in cc.mcp_catalog().servers.items():
        if name in meta["mcp"]:
            p["mcp_skip"].append(name)
            continue
        try:
            LocalSource.normalize(name, s["config"])
            p["mcp_copy"].append(name)
        except SourceError as e:
            p["mcp_invalid"].append(f"{name}: {e}")

    # skipped names keep pointing at cc-switch: their store copy is a different skill
    will_have = set(p["skills_copy"]) | set(p["skills_replace"]) | set(p["skills_done"])
    for folder in all_folders(packs_data):
        f = Path(folder)
        for rel in SKILL_DIRS:
            sd = f / rel
            if not sd.is_dir():
                continue
            for e in sd.iterdir():
                if is_link(e) and classify(e, cc.store) == "managed" and e.name in will_have:
                    p["relink"].append([folder, rel, e.name])

    for client, gdir in local.global_dirs.items():
        for d, r in rows.items():
            if client not in r["enabled"] or d not in will_have:
                continue
            e = gdir / d
            if is_link(e):
                kind = "already" if classify(e, local.store) == "managed" else \
                    "link" if classify(e, cc.store) == "managed" else "foreign"
            elif e.is_dir():
                kind = "copy"
            else:
                kind = "missing"
            if kind != "already":
                p["global"].append([client, d, kind])
    if p["ccswitch_running"]:
        p["warnings"].append("cc-switch is running. Quit it first: it rewrites the agents' skill folders.")
    if others:
        p["warnings"].append(f"cc-switch also manages {', '.join(others)}; those are not changed here.")
    return p


def apply(p: dict, cc: CcSwitchSource, local: LocalSource, data_dir: Path,
          relink: bool = True, take_over_global: bool = True) -> dict:
    if process_running(CCSWITCH_PROCESSES):
        raise SourceError("cc-switch is running. Quit it first, then migrate.")
    rows, _ = _ccswitch_rows(cc.db)
    log, errors = [], []
    backup = data_dir / "migration-backup" / time.strftime("%Y%m%d-%H%M%S")
    c = local._load()
    now = int(time.time())

    for d in p["skills_copy"] + p["skills_replace"]:
        try:
            repo.copy_skill(cc.store / d, local.store / d)
            src = rows.get(d, {}).get("source") or {"local": str(cc.store / d)}
            c["skills"][d] = {"source": src, "installed_at": now, "updated_at": now, "migrated_from": "cc-switch"}
            log.append(f"skill {d} copied")
        except (OSError, repo.RepoError) as e:
            errors.append(f"skill {d}: {e}")
    local._save(c)

    servers = cc.mcp_catalog().servers
    for name in p["mcp_copy"]:
        try:
            local.save_mcp(name, servers[name]["config"], servers[name].get("description", ""))
            log.append(f"MCP {name} copied")
        except SourceError as e:
            errors.append(f"MCP {name}: {e}")

    if relink:
        for folder, rel, name in p["relink"]:
            e = Path(folder) / rel / name
            try:
                if is_link(e) and classify(e, cc.store) == "managed" and (local.store / name / "SKILL.md").is_file():
                    safe_unlink(e, cc.store)
                    make_junction(local.store / name, e)
                    log.append(f"relinked {folder} {rel}/{name}")
            except OSError as ex:
                errors.append(f"relink {e}: {ex}")

    if take_over_global:
        for client, d, kind in p["global"]:
            e = local.global_dirs[client] / d
            try:
                if not (local.store / d / "SKILL.md").is_file():
                    continue
                if kind == "copy" and e.is_dir() and not is_link(e):
                    dst = backup / client / d
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(e), str(dst))
                elif kind == "link" and is_link(e):
                    safe_unlink(e, cc.store)
                elif kind == "foreign":
                    continue  # a link the user made elsewhere: leave it
                if os.path.lexists(e):
                    continue
                e.parent.mkdir(parents=True, exist_ok=True)
                make_junction(local.store / d, e)
                log.append(f"global {client}: {d} now links to the store")
            except OSError as ex:
                errors.append(f"global {client}/{d}: {ex}")

    if backup.exists():
        log.append(f"cc-switch's copies were moved to {backup}")
    (data_dir / "migration-log.json").write_text(json.dumps({"log": log, "errors": errors}, indent=2), encoding="utf8")
    return {"log": log, "errors": errors, "backup": str(backup) if backup.exists() else None}
