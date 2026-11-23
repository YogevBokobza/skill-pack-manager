"""Count how often each skill was used in past agent sessions. Read-only.

  Claude Code  ~/.claude/projects/**/*.jsonl   Skill tool calls and /slash commands
  OpenCode     ~/.local/share/opencode/opencode.db   parts with tool "skill"
  Codex        ~/.codex/sessions/**/*.jsonl    reads of .../skills/<name>/SKILL.md

Skills behind a pack router are not called by name: the agent reads
using-<pack>/references/<name>/<name>.md. Those reads count too, with
via "router".
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
SLASH = re.compile(r"<command-name>/([A-Za-z0-9_:.-]+)</command-name>")
CODEX_READ = re.compile(r"skills[\\/]+([A-Za-z0-9_.-]+)[\\/]+SKILL\.md")
# using-<pack>/references/<name>/<name>.md, with / or (JSON-escaped) \ separators
ROUTER_READ = re.compile(r"using-[a-z0-9-]+[\\/]+references[\\/]+([A-Za-z0-9_.-]+)[\\/]+\1\.md")
CWD = re.compile(r'"cwd":"([^"]+)"')
AGENTS = ("claude", "opencode", "codex")


def _first_cwd(line: str, cwd):
    if cwd is None and '"cwd":"' in line:
        m = CWD.search(line)
        return m.group(1).replace("\\\\", "\\") if m else None
    return cwd


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def events(names: set[str], home: Path | None = None) -> tuple[list[tuple], str | None]:
    """Every use of a skill in names: (skill, day, cwd, agent, via), via being
    "call" or "router". Also returns the earliest day the logs cover, or None."""
    home = home or Path.home()
    out: list[tuple] = []
    first: list[str] = []

    def add(skill: str, day: str, cwd, agent: str, via: str = "call"):
        skill = skill.split(":")[-1]  # plugin:skill
        if skill in names:
            out.append((skill, day, cwd or "?", agent, via))

    def oldest(files):
        try:
            first.append(_day(min(f.stat().st_mtime for f in files)))
        except (ValueError, OSError):
            pass

    claude = list((home / ".claude" / "projects").rglob("*.jsonl"))
    oldest(claude)
    for f in claude:
        cwd = None
        try:
            with f.open(encoding="utf8", errors="ignore") as fh:
                for line in fh:
                    cwd = _first_cwd(line, cwd)
                    skill, read = '"name":"Skill"' in line or "<command-name>/" in line, "references" in line
                    if not (skill or read):
                        continue
                    m = re.search(r'"timestamp":"([^"]{10})', line)
                    day = m.group(1) if m else "?"
                    if skill:
                        for m in [*SKILL_CALL.finditer(line), *SLASH.finditer(line)]:
                            add(m.group(1), day, cwd, "claude")
                    if read and '"type":"tool_use"' in line:
                        for s in set(ROUTER_READ.findall(line)):
                            add(s, day, cwd, "claude", "router")
        except OSError:
            pass

    oc = home / ".local" / "share" / "opencode" / "opencode.db"
    if oc.exists():
        try:
            con = sqlite3.connect(f"file:{oc}?mode=ro", uri=True)
            try:
                q = ("select p.time_created, p.data, s.directory from part p left join session s "
                     "on s.id = p.session_id where p.data like '%\"tool\":\"skill\"%' "
                     "or (p.data like '%\"tool\":\"read\"%' and p.data like '%references%')")
                for t, data, d in con.execute(q):
                    try:
                        j = json.loads(data)
                        inp = j["state"]["input"]
                    except (KeyError, ValueError, TypeError):
                        continue
                    day = _day(t / 1000)
                    if j.get("tool") == "skill":
                        nm = inp.get("name") or inp.get("id")  # V2 calls it id
                        if isinstance(nm, str):
                            add(nm, day, d, "opencode")
                    else:
                        for s in set(ROUTER_READ.findall(json.dumps(inp))):
                            add(s, day, d, "opencode", "router")
                row = con.execute("select min(time_created) from session").fetchone()
                if row and row[0]:
                    first.append(_day(row[0] / 1000))
            finally:
                con.close()
        except sqlite3.Error:
            pass

    codex = list((home / ".codex" / "sessions").rglob("*.jsonl"))
    oldest(codex)
    for f in codex:
        cwd, seen, routed = None, set(), set()
        day = "-".join(p.name for p in (f.parent.parent.parent, f.parent.parent, f.parent))
        try:
            with f.open(encoding="utf8", errors="ignore") as fh:
                for line in fh:
                    cwd = _first_cwd(line, cwd)
                    if '"type":"function_call"' in line or '"type":"custom_tool_call"' in line:
                        seen.update(CODEX_READ.findall(line))
                        routed.update(ROUTER_READ.findall(line))
        except OSError:
            pass
        for s in seen:
            add(s, day, cwd, "codex")
        for s in routed - seen:
            add(s, day, cwd, "codex", "router")

    first += [r[1] for r in out if r[1] != "?"]
    return out, (min(first) if first else None)


def summarize(rows: list[tuple]) -> dict:
    """Totals for one skill's events."""
    days = [d for _, d, _, _, _ in rows if d != "?"]
    last_in: dict[str, str] = {}
    for _, d, c, _, _ in rows:
        if d != "?" and d > last_in.get(c, ""):
            last_in[c] = d
    folders = [[c, n, last_in.get(c, "?")] for c, n in collections.Counter(c for _, _, c, _, _ in rows).most_common()]
    agents = collections.Counter(a for _, _, _, a, _ in rows)
    by_day = collections.Counter(days)
    return {"uses": len(rows), "last": max(days) if days else "?",
            "where": [f[:2] for f in folders[:3]], "folders": folders,
            "agents": dict(agents), "router": sum(1 for r in rows if r[4] == "router"),
            "days": dict(sorted(by_day.items()))}


def collect(names: set[str], home: Path | None = None) -> dict[str, dict]:
    """names: skill folder names and skill names to count. Returns
    {name: {"uses", "last": "YYYY-MM-DD", "where": top 3 [folder, count],
    "folders": all [folder, count, last day], "agents": {agent: n}, "router": n,
    "days": {day: n}}} plus "_since": the earliest day the logs cover."""
    rows, since = events(names, home)
    by: dict[str, list[tuple]] = collections.defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    out: dict = {name: summarize(rs) for name, rs in by.items()}
    out["_since"] = since
    out["_counted"] = int(time.time())
    return out
