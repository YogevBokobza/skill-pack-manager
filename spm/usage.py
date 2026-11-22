"""Count how often each skill was used in past agent sessions. Read-only.

  Claude Code  ~/.claude/projects/**/*.jsonl   Skill tool calls and /slash commands
  OpenCode     ~/.local/share/opencode/opencode.db   parts with tool "skill"
  Codex        ~/.codex/sessions/**/*.jsonl    reads of .../skills/<name>/SKILL.md
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SKILL_CALL = re.compile(r'"name":"Skill","input":\{"skill":"([^"]+)"')
SLASH = re.compile(r"<command-name>/([A-Za-z0-9_:.-]+)</command-name>")
CODEX_READ = re.compile(r"skills[\\/]+([A-Za-z0-9_.-]+)[\\/]+SKILL\.md")
CWD = re.compile(r'"cwd":"([^"]+)"')


def _first_cwd(line: str, cwd):
    if cwd is None and '"cwd":"' in line:
        m = CWD.search(line)
        return m.group(1).replace("\\\\", "\\") if m else None
    return cwd


def collect(names: set[str], home: Path | None = None) -> dict[str, dict]:
    """names: skill folder names and skill names to count. Returns
    {name: {"uses": n, "last": "YYYY-MM-DD", "where": [[folder, count], ...]}}."""
    home = home or Path.home()
    hits: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)

    def add(skill: str, day: str, cwd):
        skill = skill.split(":")[-1]  # plugin:skill
        if skill in names:
            hits[skill].append((day, cwd or "?"))

    for f in (home / ".claude" / "projects").rglob("*.jsonl"):
        cwd = None
        try:
            with f.open(encoding="utf8", errors="ignore") as fh:
                for line in fh:
                    cwd = _first_cwd(line, cwd)
                    if '"name":"Skill"' in line or "<command-name>/" in line:
                        ts = re.search(r'"timestamp":"([^"]{10})', line)
                        for m in [*SKILL_CALL.finditer(line), *SLASH.finditer(line)]:
                            add(m.group(1), ts.group(1) if ts else "?", cwd)
        except OSError:
            pass

    oc = home / ".local" / "share" / "opencode" / "opencode.db"
    if oc.exists():
        try:
            con = sqlite3.connect(f"file:{oc}?mode=ro", uri=True)
            q = ("select p.time_created, p.data, s.directory from part p left join session s "
                 "on s.id = p.session_id where p.data like '%\"tool\":\"skill\"%'")
            for t, data, d in con.execute(q):
                try:
                    nm = json.loads(data)["state"]["input"]["name"]
                except (KeyError, ValueError, TypeError):
                    continue
                add(nm, datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d"), d)
            con.close()
        except sqlite3.Error:
            pass

    for f in (home / ".codex" / "sessions").rglob("*.jsonl"):
        cwd, seen = None, set()
        day = "-".join(p.name for p in (f.parent.parent.parent, f.parent.parent, f.parent))
        try:
            with f.open(encoding="utf8", errors="ignore") as fh:
                for line in fh:
                    cwd = _first_cwd(line, cwd)
                    if '"type":"function_call"' in line or '"type":"custom_tool_call"' in line:
                        seen.update(CODEX_READ.findall(line))
        except OSError:
            pass
        for s in seen:
            add(s, day, cwd)

    out = {}
    for name, rows in hits.items():
        days = [d for d, _ in rows if d != "?"]
        where = collections.Counter(c for _, c in rows).most_common(3)
        out[name] = {"uses": len(rows), "last": max(days) if days else "?", "where": [list(w) for w in where]}
    return out
