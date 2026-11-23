"""Packs that include other packs.

packs.<name>.includes lists other packs. Attaching a pack attaches the packs it
includes too, transitively: a folder with `game-dev`, which includes `docs`,
gets both packs' skills and MCP servers, and each skill still shows the pack it
comes from. Cycles are refused when an include is added, and ignored when
reading a packs.json edited by hand.
"""
from __future__ import annotations

import os


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def includes(data: dict, pack: str) -> list[str]:
    p = data["packs"].get(pack) or {}
    return [x for x in p.get("includes") or [] if x in data["packs"] and x != pack]


def expand(data: dict, names) -> list[str]:
    """names plus every pack they include, transitively, without repeats."""
    out: list[str] = []
    stack = list(reversed(list(names)))
    while stack:
        n = stack.pop()
        if n in out or n not in data["packs"]:
            continue
        out.append(n)
        stack.extend(reversed(includes(data, n)))
    return out


def direct(data: dict, folder) -> list[str]:
    nf = _norm(folder)
    return [n for n, p in data["packs"].items() if any(_norm(x) == nf for x in p.get("paths", []))]


def active(data: dict, folder) -> list[str]:
    """Packs a folder gets: the ones attached to it and the ones they include."""
    return expand(data, direct(data, folder))


def including(data: dict, pack: str) -> list[str]:
    """pack and every pack that includes it, directly or through others."""
    return [n for n in data["packs"] if pack in expand(data, [n])]


def folders_using(data: dict, pack: str) -> list[str]:
    """Folders whose links change when pack changes."""
    seen: dict[str, str] = {}
    for n in including(data, pack):
        for path in data["packs"][n].get("paths", []):
            seen.setdefault(_norm(path), path)
    return list(seen.values())


def derived_folders(data: dict, pack: str) -> list[dict]:
    """Folders that get pack only because another attached pack includes it:
    [{"folder": path, "via": [attached packs that bring it]}]. Folders where
    pack is attached directly are not listed."""
    own = {_norm(x) for x in data["packs"].get(pack, {}).get("paths", [])}
    out: dict[str, dict] = {}
    for n in including(data, pack):
        if n == pack:
            continue
        for path in data["packs"][n].get("paths", []):
            key = _norm(path)
            if key in own:
                continue
            out.setdefault(key, {"folder": path, "via": []})["via"].append(n)
    return sorted(out.values(), key=lambda d: d["folder"].lower())


def would_cycle(data: dict, pack: str, include: str) -> bool:
    return pack == include or pack in expand(data, [include])
