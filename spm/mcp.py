"""MCP servers per folder, managed the same way as skills.

Desired state: packs.<name>.mcp plus folders.<path>.mcp in packs.json.
Server definitions come from the cc-switch database (table mcp_servers).
Actual state is read from three clients:

  claude   ~/.claude.json projects.<folder>.mcpServers (Claude "local" scope),
           written only through `claude mcp add-json/remove --scope local`
  opencode <folder>/.opencode/opencode.json, key "mcp"
  codex    <folder>/.codex/config.toml, tables [mcp_servers."<name>"]
  gemini   <folder>/.gemini/settings.json, key "mcpServers" (when Gemini CLI is enabled)

An entry is managed when its name exists in cc-switch and its file is not
tracked by git. Entries with other names, and any entry in a git-tracked file,
belong to the project and are never changed. Env values and headers are written
into the client files (as apply-skill-packs.py always did) but are never
returned by the API.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

CLIENTS = ("claude", "opencode", "codex", "gemini")  # gemini only when enabled
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def norm(p) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def transport(cfg: dict) -> str:
    t = cfg.get("type")
    if cfg.get("url"):
        return t if t in ("sse", "http", "streamable-http") else "http"
    return "stdio"


def toml_str(s: str) -> str:
    return json.dumps(s)  # JSON string escaping is valid TOML basic-string escaping


def toml_inline(d: dict) -> str:
    return "{ " + ", ".join(f"{toml_str(k)} = {toml_str(str(v))}" for k, v in d.items()) + " }"


def git_tracked(folder: Path, rel: str) -> bool:
    try:
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=folder, capture_output=True,
                           creationflags=NO_WINDOW, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class McpError(Exception):
    pass


# ------------------------------------------------------------------ catalog

class Catalog:
    """Known MCP servers: name -> {"config", "global": [clients], "description"}."""

    def __init__(self, servers: dict[str, dict] | None = None):
        self.servers: dict[str, dict] = servers or {}

    @classmethod
    def from_ccswitch(cls, db: Path) -> "Catalog":
        servers: dict[str, dict] = {}
        if not db.exists():
            return cls(servers)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cols = [r[1] for r in con.execute("pragma table_info(mcp_servers)")]
            rows = con.execute("select * from mcp_servers").fetchall() if cols else []
            for row in rows:
                d = dict(zip(cols, row))
                try:
                    cfg = json.loads(d["server_config"])
                except (TypeError, ValueError):
                    continue
                glob = [c for c in CLIENTS if d.get(f"enabled_{c}")]
                servers[d["name"]] = {"config": cfg, "global": glob,
                                      "description": d.get("description") or ""}
        finally:
            con.close()
        return cls(servers)

    def __contains__(self, name) -> bool:
        return name in self.servers

    def config(self, name) -> dict:
        return self.servers[name]["config"]

    def public(self, name) -> dict:
        """Everything the UI may see: no env values, no header values."""
        s = self.servers[name]
        cfg = s["config"]
        return {"name": name, "transport": transport(cfg), "command": cfg.get("command"),
                "args": cfg.get("args") or [], "url": cfg.get("url"),
                "env_keys": sorted((cfg.get("env") or {}).keys()),
                "header_keys": sorted((cfg.get("headers") or {}).keys()),
                "global": s["global"], "description": s["description"][:300]}


# ----------------------------------------------------------------- adapters

class ClaudeLocal:
    name = "claude"
    rel = None  # lives in ~/.claude.json, never in the project

    def __init__(self, claude_json: Path, cli: Path):
        self.claude_json, self.cli = claude_json, cli
        self._cache: tuple[float, dict] | None = None

    scope = "local"

    def _json(self) -> dict:
        try:
            mtime = self.claude_json.stat().st_mtime
        except OSError:
            return {}
        if not self._cache or self._cache[0] != mtime:
            try:
                j = json.loads(self.claude_json.read_text(encoding="utf8"))
            except (OSError, ValueError):
                return {}
            self._cache = (mtime, j)
        return self._cache[1]

    def _projects(self) -> dict:
        return self._json().get("projects") or {}

    def read(self, folder: Path) -> dict:
        nf = norm(folder)
        for key, v in self._projects().items():
            if norm(key) == nf:
                return dict(v.get("mcpServers") or {})
        return {}

    def tracked(self, folder: Path) -> bool:
        return False

    @staticmethod
    def spec(cfg: dict) -> dict:
        if cfg.get("url"):
            s = {"type": "sse" if transport(cfg) == "sse" else "http", "url": cfg["url"]}
            if cfg.get("headers"):
                s["headers"] = cfg["headers"]
            return s
        return {"type": "stdio", "command": cfg["command"], "args": cfg.get("args") or [],
                "env": cfg.get("env") or {}}

    def run_cli(self, folder: Path, args: list[str]) -> None:
        # a .py "CLI" (tools/fake_claude.py) runs under this Python: demos and tests
        cmd = [sys.executable.replace("pythonw", "python"), str(self.cli)] if self.cli.suffix == ".py" else [str(self.cli)]
        if not self.cli.exists():
            raise McpError(f"Claude Code CLI not found at {self.cli}; install it or set SPM_CLAUDE_CLI")
        r = subprocess.run([*cmd, *args], cwd=folder, capture_output=True, text=True,
                           creationflags=NO_WINDOW, timeout=120)
        if r.returncode != 0:
            raise McpError(f"claude {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-300:]}")
        self._cache = None

    def add(self, folder: Path, name: str, cfg: dict) -> None:
        self.run_cli(folder, ["mcp", "add-json", "--scope", self.scope, name, json.dumps(self.spec(cfg))])

    def remove(self, folder: Path, name: str) -> None:
        self.run_cli(folder, ["mcp", "remove", "--scope", self.scope, name])

    def supports(self, cfg: dict) -> str | None:
        return None


class OpenCodeFile:
    name = "opencode"
    rel = ".opencode/opencode.json"

    def file(self, folder: Path) -> Path:
        return folder / self.rel

    def _load(self, folder: Path) -> dict | None:
        f = self.file(folder)
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf8"))
        except ValueError as e:
            raise McpError(f"{f} is not valid JSON: {e}") from e

    def read(self, folder: Path) -> dict:
        try:
            j = self._load(folder)
        except McpError:
            return {}
        return dict((j or {}).get("mcp") or {})

    def tracked(self, folder: Path) -> bool:
        return git_tracked(folder, self.rel)

    @staticmethod
    def spec(cfg: dict) -> dict:
        if cfg.get("url"):
            s = {"type": "remote", "url": cfg["url"], "enabled": True}
            if cfg.get("headers"):
                s["headers"] = cfg["headers"]
            return s
        s = {"type": "local", "command": [cfg["command"], *(cfg.get("args") or [])], "enabled": True}
        if cfg.get("env"):
            s["environment"] = cfg["env"]
        return s

    def _save(self, folder: Path, j: dict) -> None:
        f = self.file(folder)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(j, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
        os.replace(tmp, f)

    def add(self, folder: Path, name: str, cfg: dict) -> None:
        j = self._load(folder) or {"$schema": "https://opencode.ai/config.json"}
        j.setdefault("mcp", {})[name] = self.spec(cfg)
        self._save(folder, j)

    def remove(self, folder: Path, name: str) -> bool:
        """True when the file was left empty and deleted."""
        j = self._load(folder) or {}
        if name in (j.get("mcp") or {}):
            del j["mcp"][name]
            if not j["mcp"]:
                del j["mcp"]
            if not {k: v for k, v in j.items() if k != "$schema"}:
                return self._delete(folder)
            self._save(folder, j)
        return False

    def _delete(self, folder) -> bool:
        f = self.file(folder)
        f.unlink()
        try:
            f.parent.rmdir()  # only when empty
        except OSError:
            pass
        return True

    def supports(self, cfg: dict) -> str | None:
        return None


HEADER = re.compile(r"""^\s*\[\s*mcp_servers\s*\.\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$""")


def drop_toml_tables(text: str, name: str) -> str:
    """Remove [mcp_servers.<name>] and its sub-tables; keep everything else."""
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith("["):
            m = HEADER.match(line)
            if m:
                key = m.group(1)
                key = json.loads(f'"{key}"') if key is not None else (m.group(2) or m.group(3))
                inside = key == name
            else:
                inside = False
        if not inside:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


class CodexFile:
    name = "codex"
    rel = ".codex/config.toml"

    def __init__(self, global_config: Path):
        self.global_config = global_config

    def file(self, folder: Path) -> Path:
        return folder / self.rel

    def read(self, folder: Path) -> dict:
        f = self.file(folder)
        try:
            return dict(tomllib.loads(f.read_text(encoding="utf8")).get("mcp_servers") or {})
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def tracked(self, folder: Path) -> bool:
        return git_tracked(folder, self.rel)

    @staticmethod
    def block(name: str, cfg: dict) -> str:
        lines = [f"[mcp_servers.{toml_str(name)}]"]
        if cfg.get("url"):
            lines.append(f"url = {toml_str(cfg['url'])}")
            if cfg.get("headers"):
                lines.append(f"http_headers = {toml_inline(cfg['headers'])}")
        else:
            lines.append(f"command = {toml_str(cfg['command'])}")
            lines.append("args = [" + ", ".join(toml_str(str(a)) for a in cfg.get("args") or []) + "]")
            if cfg.get("env"):
                lines.append(f"env = {toml_inline(cfg['env'])}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _write_checked(f: Path, new: str, check) -> None:
        old = f.read_text(encoding="utf8") if f.exists() else None
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(new, encoding="utf8")
        try:
            check(tomllib.loads(new))
        except Exception as e:
            if old is None:
                f.unlink()
            else:
                f.write_text(old, encoding="utf8")
            raise McpError(f"{f}: edit rolled back, {e}") from e

    def add(self, folder: Path, name: str, cfg: dict) -> None:
        f = self.file(folder)
        text = f.read_text(encoding="utf8") if f.exists() else ""
        text = drop_toml_tables(text, name).rstrip()
        new = (text + "\n\n" if text else "") + self.block(name, cfg)

        def check(d):
            if name not in d.get("mcp_servers", {}):
                raise McpError("server missing after write")
        self._write_checked(f, new, check)
        self._trust(folder)

    def remove(self, folder: Path, name: str) -> None:
        f = self.file(folder)
        if not f.exists():
            return
        new = drop_toml_tables(f.read_text(encoding="utf8"), name)

        def check(d):
            if name in d.get("mcp_servers", {}):
                raise McpError(f"{name!r} is defined in a form this tool does not edit")
        self._write_checked(f, new, check)

    def _trust(self, folder: Path) -> None:
        """Codex reads a project .codex/config.toml only for trusted projects."""
        g = self.global_config
        text = g.read_text(encoding="utf8") if g.exists() else ""
        try:
            projects = tomllib.loads(text).get("projects") or {}
        except tomllib.TOMLDecodeError:
            return  # never touch a global config we cannot parse
        if any(norm(k) == norm(folder) for k in projects):
            return
        key = str(folder).lower() if os.name == "nt" else str(folder)
        if "'" in key:
            return
        new = text.rstrip() + f"\n\n[projects.'{key}']\ntrust_level = \"trusted\"\n"
        self._write_checked(g, new, lambda d: None)

    def supports(self, cfg: dict) -> str | None:
        if transport(cfg) == "sse":
            return "Codex supports streamable HTTP only, not SSE"
        return None


# --------------------------------------------------------- global variants
# Used in standalone mode for "enable globally". They ignore the folder argument.

class ClaudeUser(ClaudeLocal):
    """Claude "user" scope: top-level mcpServers in ~/.claude.json, via the CLI."""
    scope = "user"

    def read(self, folder=None) -> dict:
        return dict(self._json().get("mcpServers") or {})

    def run_cli(self, folder, args):
        super().run_cli(Path.home(), args)


class OpenCodeGlobal(OpenCodeFile):
    """~/.config/opencode/opencode.json. A config with comments (.jsonc) is read
    when possible but never rewritten: rewriting would drop the comments."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir

    def file(self, folder=None) -> Path:
        return self.config_dir / "opencode.json"

    def tracked(self, folder=None) -> bool:
        return False

    def _load(self, folder=None):
        if not self.file().exists() and (self.config_dir / "opencode.jsonc").exists():
            raise McpError("the global OpenCode config is opencode.jsonc; add MCP servers there by hand")
        return super()._load(folder)


class CodexGlobal(CodexFile):
    """~/.codex/config.toml; edits only [mcp_servers."<name>"] tables."""

    def file(self, folder=None) -> Path:
        return self.global_config

    def tracked(self, folder=None) -> bool:
        return False

    def _trust(self, folder) -> None:
        pass


class GeminiFile(OpenCodeFile):
    """Gemini CLI: <folder>/.gemini/settings.json, key "mcpServers". The file
    holds other settings too; they are kept. Gemini allows comments in it, and
    a file with comments is never rewritten (the comments would be lost)."""
    name = "gemini"
    rel = ".gemini/settings.json"

    def _load(self, folder) -> dict | None:
        f = self.file(folder)
        if not f.exists():
            return None
        try:
            j = json.loads(f.read_text(encoding="utf8"))
        except ValueError as e:
            raise McpError(f"{f} has comments or is not plain JSON; add MCP servers there by hand") from e
        if not isinstance(j, dict):
            raise McpError(f"{f} is not a JSON object")
        return j

    def read(self, folder) -> dict:
        try:
            j = self._load(folder)
        except McpError:
            return {}
        return dict((j or {}).get("mcpServers") or {})

    @staticmethod
    def spec(cfg: dict) -> dict:
        if cfg.get("url"):
            # Gemini: "url" is SSE, "httpUrl" is streamable HTTP
            s = {"url": cfg["url"]} if transport(cfg) == "sse" else {"httpUrl": cfg["url"]}
            if cfg.get("headers"):
                s["headers"] = cfg["headers"]
            return s
        s = {"command": cfg["command"], "args": list(cfg.get("args") or [])}
        if cfg.get("env"):
            s["env"] = cfg["env"]
        return s

    def add(self, folder, name: str, cfg: dict) -> None:
        j = self._load(folder) or {}
        j.setdefault("mcpServers", {})[name] = self.spec(cfg)
        self._save(folder, j)

    def remove(self, folder, name: str) -> bool:
        j = self._load(folder) or {}
        if name in (j.get("mcpServers") or {}):
            del j["mcpServers"][name]
            if not j["mcpServers"]:
                del j["mcpServers"]
            if not j:
                return self._delete(folder)
            self._save(folder, j)
        return False


class GeminiGlobal(GeminiFile):
    """~/.gemini/settings.json"""

    def __init__(self, gemini_home: Path):
        self.gemini_home = gemini_home

    def file(self, folder=None) -> Path:
        return self.gemini_home / "settings.json"

    def tracked(self, folder=None) -> bool:
        return False


def default_adapters(claude_json: Path, claude_cli: Path, codex_global: Path, gemini: bool = False) -> dict:
    a = {"claude": ClaudeLocal(claude_json, claude_cli), "opencode": OpenCodeFile(),
         "codex": CodexFile(codex_global)}
    if gemini:
        a["gemini"] = GeminiFile()
    return a


def global_adapters(claude_json: Path, claude_cli: Path, codex_global: Path, opencode_dir: Path,
                    gemini_home: Path | None = None) -> dict:
    a = {"claude": ClaudeUser(claude_json, claude_cli), "opencode": OpenCodeGlobal(opencode_dir),
         "codex": CodexGlobal(codex_global)}
    if gemini_home is not None:
        a["gemini"] = GeminiGlobal(gemini_home)
    return a


# -------------------------------------------------------------- desired/plan

def desired(data: dict, folder) -> dict[str, list[str]]:
    from .packset import active
    nf, out = norm(folder), {}
    for pname in active(data, folder):
        for m in data["packs"][pname].get("mcp", []):
            out.setdefault(m, []).append(pname)
    for path, v in data.get("folders", {}).items():
        if norm(path) == nf:
            for m in v.get("mcp", []):
                out.setdefault(m, []).append("+extra")
    return out


class Tracked:
    """Lazy per-folder cache of 'is this client's file tracked by git'."""

    def __init__(self, folder: Path, adapters: dict):
        self.folder, self.adapters, self.cache = folder, adapters, {}

    def __call__(self, client: str) -> bool:
        if client not in self.cache:
            a = self.adapters[client]
            self.cache[client] = bool(a.rel) and a.tracked(self.folder)
        return self.cache[client]


def actual(folder: Path, catalog: Catalog, adapters: dict, tracked: Tracked | None = None) -> dict[str, dict[str, str]]:
    """name -> {client: managed|own|locked}"""
    tracked = tracked or Tracked(folder, adapters)
    out: dict[str, dict[str, str]] = {}
    for c, a in adapters.items():
        entries = a.read(folder)
        if not entries:
            continue
        locked = tracked(c)
        for name in entries:
            out.setdefault(name, {})[c] = "locked" if locked else ("managed" if name in catalog else "own")
    return out


def plan(data: dict, folder, catalog: Catalog, adapters: dict, off: dict | None = None) -> dict:
    """off: adapters of agents that are switched off; entries this program
    manages there (name in the catalog, file not tracked by git) are removed."""
    folder = Path(folder)
    res = {"mcp_add": [], "mcp_remove": [], "mcp_conflicts": [], "mcp_missing": [], "mcp_warnings": []}
    if not folder.is_dir():
        return res
    tracked = Tracked(folder, adapters)
    want, have = desired(data, folder), actual(folder, catalog, adapters, tracked)
    for name in sorted(want):
        if name not in catalog:
            res["mcp_missing"].append(name)
            continue
        cfg, glob = catalog.config(name), [c for c in catalog.servers[name]["global"] if c in adapters]
        for c, a in adapters.items():
            kind = have.get(name, {}).get(c)
            why = a.supports(cfg)
            if kind == "managed":
                continue
            if kind in ("own", "locked"):
                res["mcp_conflicts"].append([c, name, kind])
            elif why:
                res["mcp_warnings"].append(f"{name} skipped for {c}: {why}")
            elif tracked(c):
                res["mcp_conflicts"].append([c, name, "locked"])
            else:
                res["mcp_add"].append([c, name])
        if glob:
            res["mcp_warnings"].append(f"{name} is already enabled globally for {', '.join(glob)}; "
                                       "a folder entry duplicates it there")
    for name, places in sorted(have.items()):
        if name in want:
            continue
        for c, kind in places.items():
            if kind == "managed":
                res["mcp_remove"].append([c, name])
    if off:
        for name, places in sorted(actual(folder, catalog, off).items()):
            for c, kind in places.items():
                if kind == "managed":
                    res["mcp_remove"].append([c, name])
    return res


def apply(p: dict, folder: Path, catalog: Catalog, adapters: dict) -> tuple[list[str], set[str], set[str]]:
    """Returns the log, files created and files deleted (paths for .git/info/exclude)."""
    log, created, deleted = [], set(), set()
    for c, name in p["mcp_add"]:
        a = adapters[c]
        if a.rel and not (folder / a.rel).exists():
            created.add(f"/{a.rel}")
        a.add(folder, name, catalog.config(name))
        log.append(f"MCP {name} added for {c}")
    for c, name in p["mcp_remove"]:
        a = adapters[c]
        if a.remove(folder, name):
            deleted.add(f"/{a.rel}")
            log.append(f"MCP {name} removed for {c}; {a.rel} was left empty and deleted")
        else:
            log.append(f"MCP {name} removed for {c}")
    return log, created, deleted
