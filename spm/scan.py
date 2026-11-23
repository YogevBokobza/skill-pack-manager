"""Heuristic checks of a skill folder before it is installed or updated.

A skill is text that goes into an agent's context, often with scripts the
agent runs. These checks look for patterns that deserve a human look: piping
downloads into a shell, decoding and running hidden code, reading credentials,
destructive commands, persistence, switching off an agent's permission checks,
text that tries to steer the agent away from the user, and invisible Unicode
that hides text from the reader.

The checks only flag lines. A clean result does not mean a skill is safe, and
a flagged line can be harmless (a skill that documents `curl | sh` to warn
against it). Nothing here runs or imports the files.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_FILE = 1024 * 1024
SCRIPT_EXT = {".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".py", ".js", ".mjs", ".cjs", ".ts",
              ".rb", ".pl", ".php", ".lua", ".go", ".rs"}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".jar", ".msi", ".com", ".scr", ".node", ".wasm"}

# (level, rule id, what it means, pattern). Levels: high, medium, info.
RULES: list[tuple[str, str, str, re.Pattern]] = [(lvl, rid, why, re.compile(p, re.I)) for lvl, rid, why, p in [
    ("high", "pipe-to-shell", "downloads code and runs it at once",
     r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|"
     r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(iex|invoke-expression|python3?|node|perl|ruby)\b"),
    ("high", "decode-and-run", "decodes hidden code and runs it",
     r"base64\s+(-d|--decode)[^\n]*\|\s*(ba|z)?sh|eval\s*\(\s*atob\s*\(|exec\s*\(\s*(base64\.)?b64decode|"
     r"FromBase64String[^\n]*(iex|invoke-expression)|(iex|invoke-expression)[^\n]*FromBase64String|"
     r"eval\s*\(\s*(compile\s*\()?\s*(zlib|base64|codecs)\."),
    ("high", "destructive", "deletes or overwrites far outside the project",
     r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(--no-preserve-root\s+)?(/|~|\$HOME)(\s|$|/\*)|"
     r"remove-item\b[^\n]*-recurse[^\n]*(\s|\")(c:\\|~|\$env:userprofile|\$home)(\s|\"|\\?$)|"
     r"\bmkfs(\.\w+)?\s|\bdd\s+if=[^\n]*of=/dev/|\bformat\s+[a-z]:|:\(\)\s*\{\s*:\|:&\s*\};:"),
    ("medium", "credentials", "reads credentials or secrets",
     r"(~|\$HOME|%USERPROFILE%|\$env:USERPROFILE)[/\\]\.(ssh|aws|gnupg|kube|docker)\b|\bid_(rsa|ed25519|ecdsa)\b|"
     r"\.aws[/\\]credentials|\.netrc\b|\.git-credentials|security\s+find-(generic|internet)-password|"
     r"\b(login\s+data|cookies\.sqlite|key4\.db)\b|/etc/shadow\b"),
    ("medium", "exfiltration", "sends data out with a POST or upload",
     r"\bcurl\b[^\n]*\s(-d|--data(-binary|-raw)?|-F|--form|-T|--upload-file)\s|"
     r"requests\.(post|put)\s*\(|invoke-(webrequest|restmethod)\b[^\n]*-method\s+(post|put)|"
     r"\bnc\s+(-e|-c)\s|/dev/tcp/"),
    ("medium", "persistence", "sets up something that runs again later",
     r"\bcrontab\s+(-e|-l|[^\s-])|\bschtasks\b[^\n]*/create|\blaunchctl\s+(load|bootstrap)|"
     r"reg(\.exe)?\s+add\s+[^\n]*\\(run|runonce)\b|>>\s*~?/?\.?(bashrc|zshrc|profile|bash_profile)\b|"
     r"start\s*menu\\programs\\startup|systemctl\s+(--user\s+)?enable\b"),
    ("medium", "no-permission-checks", "switches off the agent's or the system's safety checks",
     r"--dangerously-skip-permissions|--dangerously-bypass-approvals|bypassPermissions|--yolo\b|"
     r"\bchmod\s+(-R\s+)?777\b|Set-ExecutionPolicy\s+(Unrestricted|Bypass)|\bsudo\s+-S\b"),
    ("medium", "steers-the-agent", "text that tries to override instructions or hide actions from the user",
     r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|preceding)\s+(instructions|prompts|rules)|"
     r"disregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|system|user)('s)?\s+(instructions|prompts|messages)|"
     r"(do\s+not|don'?t|never)\s+(tell|inform|show|mention\s+(this|it)\s+to|alert)\s+the\s+user|"
     r"without\s+(telling|informing|asking|notifying)\s+the\s+user|"
     r"(reveal|print|output|repeat)\s+(your|the)\s+system\s+prompt|you\s+are\s+no\s+longer\s+bound"),
    ("info", "network", "talks to the network",
     r"\b(curl|wget)\s|requests\.(get|post|put)|urllib\.request|http\.client|\bfetch\s*\(|"
     r"invoke-(webrequest|restmethod)|\baxios\b|socket\.connect"),
]]

# zero-width and joiner characters, bidirectional controls, and Unicode tag
# characters (which can spell out text no one sees)
HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]|[\U000e0000-\U000e007f]")


def _text(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf8", errors="replace")


def scan_folder(folder: Path, limit: int = 200) -> dict:
    """{"high": n, "medium": n, "info": n, "scripts": [...], "binaries": [...],
    "findings": [{"level", "rule", "why", "file", "line", "text"}]}"""
    findings: list[dict] = []
    scripts, binaries = [], []
    files = sorted(p for p in folder.rglob("*") if p.is_file() and ".git" not in p.relative_to(folder).parts)
    for p in files:
        rel = p.relative_to(folder).as_posix()
        ext = p.suffix.lower()
        if ext in BINARY_EXT:
            binaries.append(rel)
            findings.append({"level": "medium", "rule": "binary", "why": "a compiled program; it cannot be read",
                             "file": rel, "line": 0, "text": ""})
            continue
        text = _text(p)
        if text is None:
            continue
        is_script = ext in SCRIPT_EXT or text.startswith("#!")
        if is_script:
            scripts.append(rel)
        for n, line in enumerate(text.splitlines(), 1):
            if HIDDEN.search(line):
                shown = HIDDEN.sub(lambda m: f"<U+{ord(m.group(0)):04X}>", line)
                findings.append({"level": "high", "rule": "hidden-text",
                                 "why": "invisible Unicode characters that can hide text from the reader",
                                 "file": rel, "line": n, "text": shown.strip()[:200]})
            for level, rid, why, pat in RULES:
                if rid == "network" and not is_script:
                    continue  # documentation mentions curl all the time
                if pat.search(line):
                    findings.append({"level": level, "rule": rid, "why": why, "file": rel, "line": n,
                                     "text": line.strip()[:200]})
                    break  # one finding per line: the most severe rule
    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["level"]], f["file"], f["line"]))
    counts = {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in order}
    return {**counts, "scripts": scripts, "binaries": binaries, "findings": findings[:limit],
            "truncated": len(findings) > limit}


def key(f: dict) -> tuple:
    """Identity of a finding across versions (line numbers move)."""
    return (f["rule"], f["file"], f["text"])


def new_findings(old: dict | None, new: dict) -> list[dict]:
    """Findings in new that old did not have."""
    seen = {key(f) for f in (old or {}).get("findings", [])}
    return [f for f in new["findings"] if key(f) not in seen]
