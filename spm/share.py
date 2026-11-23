"""Share packs as a file, and read a project's .skillpacks.json.

A pack file holds packs (with the packs they include), where each skill comes
from (repository, ref, path, commit) and each MCP server's definition. It never
holds secrets: env values and header values are written as "" and the reader
fills them in. The same format, plus "attach", is the manifest a repository
can commit as .skillpacks.json to say which packs its folder should get.

  {"format": "skill-pack-manager", "version": 1,
   "packs":  {"web": {"skills": [...], "mcp": [...], "includes": [...],
                      "router": {"mode": "instead"}}},
   "skills": {"frontend-design": {"url": ..., "ref": ..., "path": ..., "commit": ...}},
   "mcp":    {"browser": {"command": "npx", "args": [...], "env": {"TOKEN": ""}}},
   "attach": ["web"]}

Reading one never installs anything. The app shows what it would add, the
usual link preview follows, and missing skills go through the normal install
dialog with its safety check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import mcp, packset, repo

FORMAT, VERSION = "skill-pack-manager", 1
MANIFEST = ".skillpacks.json"
PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SKILL_DIR = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_BYTES = 512 * 1024


class ShareError(Exception):
    pass


# ----------------------------------------------------------------- export

def export(model, data: dict, packs: list[str], attach: list[str] | None = None) -> dict:
    for n in packs:
        if n not in data["packs"]:
            raise ShareError(f"no such pack: {n!r}")
    names = packset.expand(data, packs)
    info = model.source.skills()
    catalog = model.mcp_catalog()
    out = {"format": FORMAT, "version": VERSION, "packs": {}, "skills": {}, "mcp": {}}
    for n in names:
        p = data["packs"][n]
        entry = {"skills": list(p["skills"]), "mcp": list(p.get("mcp", []))}
        if packset.includes(data, n):
            entry["includes"] = packset.includes(data, n)
        r = p.get("router")
        if isinstance(r, dict) and r.get("enabled"):
            entry["router"] = {"mode": r.get("mode", "instead")}
        out["packs"][n] = entry
        for s in p["skills"]:
            src = (info.get(s) or {}).get("source") or {}
            out["skills"][s] = {k: src[k] for k in ("url", "ref", "kind", "path", "commit") if src.get(k)} \
                if src.get("url") else {}
        for m in p.get("mcp", []):
            if m in catalog:
                out["mcp"][m] = redact(catalog.config(m))
    if attach is not None:
        out["attach"] = [a for a in attach if a in out["packs"]]
    return out


def redact(cfg: dict) -> dict:
    c = json.loads(json.dumps(cfg))
    for key in ("env", "headers"):
        if isinstance(c.get(key), dict):
            c[key] = {k: "" for k in c[key]}
    return c


# ------------------------------------------------------------------ parse

def parse(obj) -> dict:
    """Check a pack file or manifest; returns it cleaned. Raises ShareError."""
    from .sources import LocalSource, SourceError
    if isinstance(obj, (str, bytes)):
        if len(obj) > MAX_BYTES:
            raise ShareError("the file is too large for a pack file")
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise ShareError(f"not valid JSON: {e}") from e
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise ShareError('not a pack file (expected "format": "skill-pack-manager")')
    if obj.get("version") != VERSION:
        raise ShareError(f"unsupported pack file version {obj.get('version')!r}")
    packs, skills, servers = {}, {}, {}
    for n, p in (obj.get("packs") or {}).items():
        if not PACK_NAME.match(str(n)) or not isinstance(p, dict):
            raise ShareError(f"invalid pack {n!r}")
        sk = [s for s in p.get("skills") or [] if isinstance(s, str)]
        bad = [s for s in sk if not SKILL_DIR.match(s) or s in (".", "..")]
        if bad:
            raise ShareError(f"pack {n}: invalid skill names {bad}")
        ms = [m for m in p.get("mcp") or [] if isinstance(m, str)]
        if any(not mcp.NAME.match(m) for m in ms):
            raise ShareError(f"pack {n}: invalid MCP server names")
        entry = {"skills": sk, "mcp": ms,
                 "includes": [i for i in p.get("includes") or [] if isinstance(i, str) and PACK_NAME.match(i)]}
        r = p.get("router")
        if isinstance(r, dict) and r.get("mode") in ("instead", "alongside"):
            entry["router"] = {"mode": r["mode"]}
        packs[n] = entry
    if not packs:
        raise ShareError("the file has no packs")
    for n, p in packs.items():
        missing = [i for i in p["includes"] if i not in packs]
        if missing:
            raise ShareError(f"pack {n} includes {', '.join(missing)}, which the file does not define")
    for d, src in (obj.get("skills") or {}).items():
        if not SKILL_DIR.match(str(d)) or not isinstance(src, dict):
            raise ShareError(f"invalid skill entry {d!r}")
        if src.get("url"):
            try:
                repo.check_url(src["url"])
            except repo.RepoError as e:
                raise ShareError(f"{d}: {e}") from e
            ref = str(src.get("ref") or "")
            if not ref or ref.startswith("-") or any(c in ref for c in " \t\n~^:?*[\\"):
                raise ShareError(f"{d}: invalid ref {ref!r}")
            path = str(src.get("path") or ".")
            if path.startswith(("/", "\\")) or ".." in Path(path).parts:
                raise ShareError(f"{d}: invalid path {path!r}")
            skills[d] = {"url": src["url"], "ref": ref, "path": path, "kind": src.get("kind"),
                         "commit": src.get("commit")}
        else:
            skills[d] = {}
    for n, cfg in (obj.get("mcp") or {}).items():
        try:
            servers[n] = LocalSource.normalize(str(n), cfg)
        except SourceError as e:
            raise ShareError(str(e)) from e
    attach = [a for a in obj.get("attach") or [] if isinstance(a, str)]
    unknown = [a for a in attach if a not in packs]
    if unknown:
        raise ShareError(f"attach names packs the file does not define: {', '.join(unknown)}")
    return {"format": FORMAT, "version": VERSION, "packs": packs, "skills": skills, "mcp": servers,
            "attach": attach}


# ---------------------------------------------------------------- preview

def preview(model, data: dict, b: dict, replace: bool = False) -> dict:
    """What importing b would do; changes nothing."""
    info = model.source.skills()
    catalog = model.mcp_catalog()
    standalone = model.source.mode == "standalone"
    packs = []
    for n, p in b["packs"].items():
        cur = data["packs"].get(n)
        if cur is None:
            status = "new"
        elif (sorted(cur["skills"]), sorted(cur.get("mcp", [])), sorted(cur.get("includes") or [])) == \
                (sorted(p["skills"]), sorted(p["mcp"]), sorted(p["includes"])):
            status = "same"
        else:
            status = "replace" if replace else "differs"
        packs.append({"name": n, "status": status, **p})
    needed = sorted({s for p in b["packs"].values() for s in p["skills"]})
    skills, repos = [], {}
    for d in needed:
        src = b["skills"].get(d) or {}
        have = info.get(d)
        if have:
            hs = have.get("source") or {}
            same = not src or (hs.get("url"), hs.get("path")) == (src.get("url"), src.get("path"))
            skills.append({"dir": d, "status": "in_store" if same else "in_store_other_source", "source": src})
        elif src and standalone:
            skills.append({"dir": d, "status": "installable", "source": src})
            r = repos.setdefault((src["url"], src["ref"]), {"url": src["url"], "ref": src["ref"], "paths": [], "dirs": []})
            r["paths"].append(src["path"])
            r["dirs"].append(d)
        else:
            skills.append({"dir": d, "status": "missing", "source": src})
    servers = []
    for n in sorted({m for p in b["packs"].values() for m in p["mcp"]}):
        cfg = b["mcp"].get(n)
        if n in catalog:
            status = "in_catalog"
        elif cfg and standalone:
            status = "add"
        else:
            status = "missing"
        public = mcp.Catalog({n: {"config": cfg, "global": [], "description": ""}}).public(n) if cfg else {"name": n}
        servers.append({**public, "status": status})
    warnings = []
    if not standalone and any(s["status"] == "missing" for s in skills):
        warnings.append("cc-switch mode: install the missing skills in cc-switch, then Sync.")
    if any(p["status"] == "differs" for p in packs):
        warnings.append("Some packs exist with other content. They are kept unless you choose to replace them.")
    return {"packs": packs, "skills": skills, "repos": list(repos.values()), "mcp": servers,
            "attach": b["attach"], "warnings": warnings}


def apply_packs(data: dict, b: dict, replace: bool = False, folder: str | None = None) -> list[str]:
    """Write b's packs into data (packs.json content). Existing packs keep their
    folders; their content changes only with replace. With folder, the packs in
    b["attach"] get that folder. Returns the folders whose links may change."""
    touched: list[str] = []
    for n, p in b["packs"].items():
        cur = data["packs"].get(n)
        if cur is not None and not replace:
            continue
        entry = {"skills": list(p["skills"]), "mcp": list(p["mcp"]), "paths": list(cur["paths"]) if cur else []}
        if p["includes"]:
            entry["includes"] = list(p["includes"])
        if cur and cur.get("router"):
            entry["router"] = cur["router"]
        elif p.get("router"):
            entry["router"] = {"enabled": True, "mode": p["router"]["mode"]}
        data["packs"][n] = entry
        touched += entry["paths"]
    for n in b["packs"]:  # a cycle could come in through replace
        if n in packset.expand(data, packset.includes(data, n)):
            raise ShareError(f"pack {n} would include itself")
    if folder:
        for n in b["attach"]:
            paths = data["packs"][n]["paths"]
            if not any(packset._norm(x) == packset._norm(folder) for x in paths):
                paths.append(folder)
        touched.append(folder)
    out = []
    for n in b["packs"]:
        out += packset.folders_using(data, n)
    return list(dict.fromkeys(touched + out))


# ---------------------------------------------------------------- manifest

def read_manifest(folder) -> dict | None:
    """The folder's .skillpacks.json, parsed; {"error": ...} when unreadable;
    None when there is none."""
    f = Path(folder) / MANIFEST
    if not f.is_file():
        return None
    try:
        return parse(f.read_text(encoding="utf8"))
    except (OSError, ShareError) as e:
        return {"error": str(e)}


def manifest_summary(data: dict, folder) -> dict | None:
    m = read_manifest(folder)
    if m is None:
        return None
    if "error" in m:
        return {"error": m["error"]}
    attached = set(packset.direct(data, folder))
    pending = [a for a in m["attach"] if a not in attached]
    undefined = [n for n in m["packs"] if n not in data["packs"]]
    return {"attach": m["attach"], "packs": sorted(m["packs"]), "pending": pending, "undefined": undefined,
            "done": not pending and not undefined}
