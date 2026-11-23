"""Read-only analysis: what skills cost in context, what to clean up, and
which pack fits a project.

Context cost. At the start of a session an agent lists every skill it can see
with its name and description. The cost here is an estimate of those listings:
about four characters per token, the usual ratio for English text. There is
no tokenizer in the standard library, so treat the numbers as a guide.

Which folders each agent reads (from the agents' documentation):
  Claude Code  .claude/skills                          ~/.claude/skills
  OpenCode     .opencode/skills .claude/skills .agents/skills
               ~/.config/opencode/skills ~/.claude/skills ~/.agents/skills
  Codex        .agents/skills                          ~/.codex/skills ~/.agents/skills
  Gemini CLI   .gemini/skills .agents/skills           ~/.gemini/skills ~/.agents/skills  (when enabled)
A name found twice counts once; the project's copy wins. Skills from plugins
and built-in skills are not counted.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import date, timedelta
from pathlib import Path

from .repo import frontmatter

AGENTS = ("claude", "opencode", "codex", "gemini")
PROJECT_DIRS = {
    "claude": (".claude/skills",),
    "opencode": (".claude/skills", ".agents/skills", ".opencode/skills"),
    "codex": (".agents/skills",),
    "gemini": (".gemini/skills", ".agents/skills"),
}


def global_dirs(home: Path, opencode_dir: Path, codex_home: Path,
                gemini_home: Path | None = None) -> dict[str, tuple[Path, ...]]:
    return {
        "gemini": ((gemini_home or home / ".gemini") / "skills", home / ".agents" / "skills"),
        "claude": (home / ".claude" / "skills",),
        "opencode": (home / ".claude" / "skills", home / ".agents" / "skills", opencode_dir / "skills"),
        "codex": (codex_home / "skills", home / ".agents" / "skills"),
    }


def tokens(name: str, description: str) -> int:
    """Estimated tokens of one listing line: "- name: description"."""
    return math.ceil(len(f"- {name}: {description}\n") / 4)


class Reader:
    """Reads and caches each skill's listing (name, description, tokens)."""

    def __init__(self):
        self.cache: dict[str, dict | None] = {}

    def listing(self, skill_dir: Path) -> dict | None:
        """None when the folder has no SKILL.md or the skill is hidden from the
        model (disable-model-invocation: true)."""
        key = os.path.normcase(os.path.realpath(skill_dir))
        if key not in self.cache:
            try:
                fm = frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf8", errors="replace"))
            except OSError:
                self.cache[key] = None
                return None
            if str(fm.get("disable-model-invocation", "")).lower() == "true":
                self.cache[key] = None
                return None
            name, desc = fm.get("name") or skill_dir.name, fm.get("description", "")
            self.cache[key] = {"name": name, "description": desc, "tokens": tokens(name, desc)}
        return self.cache[key]

    def folder(self, d: Path) -> dict[str, dict]:
        """entry name -> listing, for every skill in one skills folder."""
        out: dict[str, dict] = {}
        try:
            entries = sorted(d.iterdir()) if d.is_dir() else []
        except OSError:
            return out
        for e in entries:
            if e.name.startswith("."):
                continue
            li = self.listing(e) if e.is_dir() else None
            if li:
                out[e.name] = li
        return out


def agent_cost(reader: Reader, gdirs: tuple[Path, ...], folder: Path | None, rels: tuple[str, ...]) -> dict:
    """One agent's listing in one folder (None: outside any project)."""
    seen: dict[str, dict] = {}
    for d in gdirs:
        for n, li in reader.folder(d).items():
            seen[n] = li | {"scope": "global"}
    for rel in rels if folder else ():
        for n, li in reader.folder(folder / rel).items():
            seen[n] = li | {"scope": "project"}
    skills = sorted(({"skill": n, **v} for n, v in seen.items()), key=lambda x: (-x["tokens"], x["skill"]))
    for s in skills:
        s.pop("description", None)
    return {"count": len(skills), "tokens": sum(s["tokens"] for s in skills),
            "global_tokens": sum(s["tokens"] for s in skills if s["scope"] == "global"), "skills": skills}


# ----------------------------------------------------------------- packs

def pack_cost(reader: Reader, store: Path, skills: list[str]) -> int:
    return sum((reader.listing(store / s) or {}).get("tokens", 0) for s in skills)


def router_savings(model, data: dict, reader: Reader, folder) -> list[dict]:
    """Per pack of the folder: tokens its router saves, or would save."""
    from .core import packs_of, router_name, router_of
    out = []
    for name in packs_of(data, folder):
        p = data["packs"][name]
        r = router_of(p)
        full = pack_cost(reader, model.store, p["skills"])
        rt = reader.listing(model.stores.routers / router_name(name)) if r else None
        router_tokens = rt["tokens"] if rt else _router_estimate(name, len(p["skills"]))
        instead = bool(r) and r.get("mode", "instead") == "instead"
        out.append({"pack": name, "skills": len(p["skills"]), "tokens": full, "router": bool(r),
                    "mode": r.get("mode", "instead") if r else None,
                    "saved": max(full - router_tokens, 0) if instead else 0,
                    "could_save": max(full - router_tokens, 0) if not instead else 0})
    return out


def _router_estimate(pack: str, n: int) -> int:
    # a template description names every topic; about 60 characters per skill
    return tokens(f"using-{pack}", "x" * min(1024, 180 + 60 * n))


def costs(model, data: dict) -> dict:
    """Context cost outside projects and in every folder in packs.json."""
    from .core import all_folders
    reader = Reader()
    cfg = model.cfg
    g = global_dirs(cfg.home, cfg.opencode_dir, cfg.codex_global.parent, cfg.gemini_home)
    agents = model.agents
    out = {"global": {a: agent_cost(reader, g[a], None, ()) for a in agents}, "folders": []}
    for path in all_folders(data):
        f = Path(path)
        if not f.is_dir():
            continue
        out["folders"].append({"folder": path,
                               "agents": {a: agent_cost(reader, g[a], f, PROJECT_DIRS[a]) for a in agents},
                               "packs": router_savings(model, data, reader, f)})
    return out


def folder_cost(model, data: dict, folder) -> dict:
    reader = Reader()
    cfg = model.cfg
    g = global_dirs(cfg.home, cfg.opencode_dir, cfg.codex_global.parent, cfg.gemini_home)
    f = Path(folder)
    return {a: {k: v for k, v in agent_cost(reader, g[a], f, PROJECT_DIRS[a]).items() if k != "skills"}
            for a in model.agents} if f.is_dir() else {}


# ------------------------------------------------------------ suggestions

LABEL = {"claude": "Claude Code", "opencode": "OpenCode", "codex": "Codex", "gemini": "Gemini CLI"}


def _names(clients) -> str:
    return ", ".join(LABEL.get(c, c) for c in clients)


def _short(folder: str) -> str:
    return Path(folder).name or folder


def _match_folder(cwd: str, folders: list[str]) -> str | None:
    """The managed folder a session ran in: the deepest one that contains cwd."""
    from .core import is_within
    best = None
    for f in folders:
        if cwd != "?" and is_within(cwd, f) and (best is None or len(f) > len(best)):
            best = f
    return best


def suggestions(model, data: dict, usage: dict, days: int = 60, today: date | None = None) -> list[dict]:
    """Cleanup ideas. Each has kind, text, tokens (saved per session, estimated),
    and an action the UI can run through its usual preview:
      {"op": name, "args": {...}} - a packs.json operation
      {"global_off": [clients]}   - standalone: turn a global skill off
      {"router": pack}            - open the router editor
    Usage-based ideas need a usage count; ideas about routers do not."""
    from .core import all_folders, extras_of, norm, router_of
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    reader = Reader()
    skills = model.source.skills()
    standalone = model.source.mode == "standalone"
    folders = all_folders(data)
    counted = bool(usage.get("_at"))
    out: list[dict] = []

    def recent(d: str, folder: str | None = None) -> bool:
        """Was skill d used since the cutoff, in folder (None: anywhere)?"""
        u = usage.get(d) or {}
        if folder is None:
            return any(day >= cutoff for day in (u.get("days") or {}))
        return any(len(e) > 2 and e[2] != "?" and e[2] >= cutoff and _match_folder(e[0], [folder]) == folder
                   for e in u.get("folders") or [])

    # folders where agents used any skill since the cutoff; elsewhere there is
    # no evidence either way (a pack attached yesterday, a project left alone)
    active = {norm(f) for f in folders
              if any(len(e) > 2 and e[2] != "?" and e[2] >= cutoff and _match_folder(e[0], [f]) == f
                     for k, u in usage.items() if not k.startswith("_") and isinstance(u, dict)
                     for e in u.get("folders") or [])}

    def tok(d: str) -> int:
        return (reader.listing(model.store / d) or {}).get("tokens", 0)

    since = usage.get("_since")
    window = f"in the last {days} days" if not since or since <= cutoff else f"since {since}, when the logs start"

    if counted:
        for d, info in sorted(skills.items()):
            glob = info.get("global") or []
            if not glob:
                continue
            u = usage.get(d) or {}
            if not recent(d):
                out.append({"kind": "unused_global", "skill": d, "clients": glob, "tokens": tok(d),
                            "text": f"{d} is on globally for {_names(glob)} but was not used {window}.",
                            "action": {"global_off": glob} if standalone else None,
                            "hint": None if standalone else "Turn it off in cc-switch."})
                continue
            places: dict[str, list] = {}
            for cwd, n, *_ in u.get("folders") or []:
                if cwd == "?":
                    continue
                f = _match_folder(cwd, folders) or cwd
                places.setdefault(norm(f), [f, 0])[1] += n
            if len(places) == 1:
                f, n = next(iter(places.values()))
                if Path(f).is_dir() and d not in extras_of(data, f):
                    out.append({"kind": "global_one_folder", "skill": d, "folder": f, "clients": glob,
                                "tokens": tok(d), "uses": n,
                                "text": f"{d} is global, but every use ({n}) was in {_short(f)}. Make it an extra skill "
                                        f"of that folder and turn it off globally.",
                                "action": {"op": "extra_add", "args": {"folder": f, "skill": d},
                                           "then_global_off": glob if standalone else []},
                                "hint": None if standalone else "Then turn it off in cc-switch."})

        for name, p in sorted(data["packs"].items()):
            paths = [x for x in p["paths"] if Path(x).is_dir() and norm(x) in active]
            if not paths:
                continue
            for d in p["skills"]:
                if any(recent(d, f) for f in paths):
                    continue
                r = router_of(p)
                hidden = bool(r) and r.get("mode", "instead") == "instead"
                out.append({"kind": "unused_in_pack", "skill": d, "pack": name, "tokens": 0 if hidden else tok(d),
                            "text": f"{d} is in pack {name} ({len(paths)} folder{'s' if len(paths) != 1 else ''}) "
                                    f"but was not used there {window}.",
                            "action": {"op": "pack_remove_skill", "args": {"pack": name, "skill": d}}})

        for f in folders:
            if not Path(f).is_dir() or norm(f) not in active:
                continue
            for d in extras_of(data, f):
                if not recent(d, f):
                    out.append({"kind": "unused_extra", "skill": d, "folder": f, "tokens": tok(d),
                                "text": f"{d} is an extra skill of {_short(f)} but was not used there {window}.",
                                "action": {"op": "extra_remove", "args": {"folder": f, "skill": d}}})

    for name, p in sorted(data["packs"].items()):
        if router_of(p) or len(p["skills"]) < 3 or not p["paths"]:
            continue
        saved = pack_cost(reader, model.store, p["skills"]) - _router_estimate(name, len(p["skills"]))
        if saved > 50:
            out.append({"kind": "create_router", "pack": name, "tokens": saved,
                        "text": f"Pack {name} lists {len(p['skills'])} skills in {len(p['paths'])} "
                                f"folder{'s' if len(p['paths']) != 1 else ''}. A router would list one.",
                        "action": {"router": name}})
    return sorted(out, key=lambda s: -s["tokens"])


# ------------------------------------------------------------ project kind

ENGINES = (("unity", lambda d: (d / "ProjectSettings" / "ProjectVersion.txt").is_file()),
           ("unreal", lambda d: any(x.suffix == ".uproject" for x in _files(d))),
           ("godot", lambda d: (d / "project.godot").is_file()))
NODE_TAGS = {"react": "react", "next": "next", "vue": "vue", "svelte": "svelte", "@angular/core": "angular",
             "expo": "expo", "react-native": "react-native", "electron": "electron", "astro": "astro",
             "@nestjs/core": "nestjs", "express": "express", "vite": "vite", "tailwindcss": "tailwind"}
PY_TAGS = ("django", "fastapi", "flask", "pytorch", "torch", "pandas", "streamlit")
# words in pack names that mean a tag fits
FAMILIES = {
    "game": {"unity", "unreal", "godot"}, "gamedev": {"unity", "unreal", "godot"},
    "web": {"react", "next", "vue", "svelte", "angular", "astro", "vite", "tailwind", "node"},
    "frontend": {"react", "next", "vue", "svelte", "angular", "astro", "tailwind"},
    "backend": {"nestjs", "express", "django", "fastapi", "flask", "go", "rust", "dotnet", "java"},
    "mobile": {"expo", "react-native", "flutter", "android", "ios"},
    "ml": {"pytorch", "torch", "pandas"}, "ai": {"pytorch", "torch"}, "data": {"pandas", "streamlit"},
    "infra": {"docker", "terraform"}, "devops": {"docker", "terraform"},
}


def _files(d: Path) -> list[Path]:
    try:
        return [x for x in d.iterdir() if x.is_file()]
    except OSError:
        return []


def _read(p: Path, limit: int = 200_000) -> str:
    try:
        with p.open(encoding="utf8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def tags_of(d: Path) -> list[str]:
    """What a folder holds: engines, languages and main frameworks, from
    marker files only (nothing is run)."""
    tags: list[str] = []
    add = lambda t: tags.append(t) if t not in tags else None  # noqa: E731
    for name, test in ENGINES:
        try:
            if test(d):
                add(name)
        except OSError:
            pass
    files = {f.name.lower() for f in _files(d)}
    if "package.json" in files:
        add("node")
        try:
            j = json.loads(_read(d / "package.json"))
            deps = {**(j.get("dependencies") or {}), **(j.get("devDependencies") or {})}
        except (ValueError, AttributeError):
            deps = {}
        for dep, t in NODE_TAGS.items():
            if dep in deps:
                add(t)
        if "typescript" in deps or "tsconfig.json" in files:
            add("typescript")
    py = [f for f in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pipfile") if f in files]
    if py:
        add("python")
        text = " ".join(_read(d / f).lower() for f in py)
        for t in PY_TAGS:
            if re.search(rf"(^|[^a-z0-9_-]){re.escape(t)}([^a-z0-9_-]|$)", text):
                add("pytorch" if t == "torch" else t)
    for marker, t in (("cargo.toml", "rust"), ("go.mod", "go"), ("pubspec.yaml", "flutter"),
                      ("pom.xml", "java"), ("build.gradle", "java"), ("build.gradle.kts", "java"),
                      ("dockerfile", "docker"), ("docker-compose.yml", "docker"), ("compose.yaml", "docker"),
                      ("podfile", "ios"), ("gemfile", "ruby"), ("composer.json", "php")):
        if marker in files:
            add(t)
    if any(f.endswith((".csproj", ".sln", ".fsproj")) for f in files) and "unity" not in tags:
        add("dotnet")
    if any(f.endswith(".tf") for f in files):
        add("terraform")
    if (d / "app" / "src" / "main" / "AndroidManifest.xml").is_file():
        add("android")
    return tags


def suggest_packs(tags: list[str], data: dict, tags_by_folder: dict[str, list[str]]) -> list[dict]:
    """Packs that fit a project with these tags, best first. A pack fits when
    its name names a tag or a family of the tag (web, game, mobile, ...), or
    when folders it is attached to share tags with this one."""
    from .core import norm
    if not tags:
        return []
    out = []
    for name, p in data["packs"].items():
        words = set(re.split(r"[-_]", name))
        score, why = 0, []
        named = [t for t in tags if t in words or t == name]
        fam = [t for w in words for t in FAMILIES.get(w, ()) if t in tags]
        if named:
            score += 3
            why.append(f"named after {named[0]}")
        elif fam:
            score += 2
            why.append(f"{name} fits {fam[0]}")
        shared = [x for x in p["paths"] if set(tags_by_folder.get(norm(x), [])) & set(tags)]
        if shared:
            score += min(len(shared), 3)
            why.append(f"used by {len(shared)} similar project{'s' if len(shared) != 1 else ''}")
        if score:
            out.append({"pack": name, "score": score, "why": "; ".join(why)})
    return sorted(out, key=lambda x: (-x["score"], x["pack"]))[:3]
