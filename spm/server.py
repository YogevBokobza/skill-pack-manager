"""Local HTTP server for the UI. Binds 127.0.0.1 only.

Every /api call must carry the per-launch token in the X-SPM-Token header and a
Host header of 127.0.0.1:<port>, so other web pages open in a browser cannot
drive the API (no CSRF, no DNS rebinding).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import mcp, migrate, repo, router
from .core import Config, Model, OpError, UnsafeRemoval
from .mcp import McpError
from .osinfo import WINDOWS, MACOS, data_dir, open_path
from .settings import Settings
from .sources import CcSwitchSource, LocalSource, SourceError

STATIC = Path(__file__).with_name("static")
USER_ERRORS = (OpError, UnsafeRemoval, McpError, SourceError, repo.RepoError, router.RouterError, ValueError, KeyError,
               FileNotFoundError)


class App:
    def __init__(self, cfg: Config | None = None):
        self.base_cfg = cfg
        self.data_dir = cfg.data_dir if cfg else data_dir()
        self.settings = Settings(self.data_dir / "settings.json")
        self.token = secrets.token_urlsafe(24)
        self.last_ping = time.time()
        self.bye_at = 0.0  # set by the page's pagehide beacon (close or reload)
        self.usage_job = {"running": False, "error": None}
        self.ws = repo.Workspace(self.data_dir / "tmp")
        self.secrets = router.Secrets(self.data_dir / "secrets.json")
        self.build()

    # ------------------------------------------------------------- mode/model

    def build(self) -> None:
        s = self.settings.load()
        if self.base_cfg is not None:
            cfg = self.base_cfg
            if s["scan_roots"] is not None:
                cfg.scan_roots = [Path(r) for r in s["scan_roots"]]
        else:
            cfg = Config.default(s["scan_roots"])
        g = s["agents"]["gemini"]
        cfg.gemini = g if g is not None else cfg.gemini_home.is_dir()
        if s["mode"] == "standalone":
            source = self.local_source(cfg)
        else:  # cc-switch, or not chosen yet (the UI shows the first-run question)
            source = CcSwitchSource(cfg.store, cfg.db)
        self.model = Model(cfg, source)

    def local_source(self, cfg: Config) -> LocalSource:
        h = cfg.home
        dirs = {"claude": h / ".claude" / "skills", "opencode": h / ".config" / "opencode" / "skills",
                "codex": h / ".codex" / "skills"}
        if cfg.gemini:
            dirs["gemini"] = cfg.gemini_home / "skills"
        return LocalSource(
            self.data_dir / "store", global_skill_dirs=dirs,
            global_mcp=mcp.global_adapters(cfg.claude_json, cfg.claude_cli, cfg.codex_global, cfg.opencode_dir,
                                           cfg.gemini_home if cfg.gemini else None))

    def migrate(self, b: dict) -> dict:
        cfg = self.model.cfg
        cc, local = CcSwitchSource(cfg.store, cfg.db), self.local_source(cfg)
        p = migrate.plan(cc, local, self.model.load(), replace=bool(b.get("replace")))
        if b.get("dry", True):
            return p
        res = migrate.apply(p, cc, local, self.data_dir, relink=bool(b.get("relink", True)),
                            take_over_global=bool(b.get("take_over_global", True)))
        self.settings.update({"mode": "standalone"})
        self.build()
        return res

    def local(self) -> LocalSource:
        if not isinstance(self.model.source, LocalSource):
            raise OpError("this works in standalone mode; with cc-switch, use cc-switch for it")
        return self.model.source

    def index_html(self) -> bytes:
        html = (STATIC / "index.html").read_text(encoding="utf8")
        theme = self.settings.load()["theme"]  # one of THEMES, safe to put in an attribute
        return html.replace('<html lang="en">', f'<html lang="en" data-theme-pref="{theme}">', 1).encode()

    def state(self) -> dict:
        s = self.settings.load()
        cfg = self.model.cfg
        return self.model.state() | {
            "usage": self.model.cached_usage(), "usage_job": self.usage_job,
            "updates": self.cached_updates() if s["mode"] == "standalone" else {},
            "settings": {"mode": s["mode"], "theme": s["theme"], "scan_roots": [str(r) for r in cfg.scan_roots],
                         "auto_update_check": s["auto_update_check"],
                         "agents": {"gemini": s["agents"]["gemini"], "gemini_found": cfg.gemini_home.is_dir(),
                                    "gemini_on": cfg.gemini},
                         "scan_roots_custom": s["scan_roots"] is not None,
                         "ai": {"writer": (s.get("ai") or {}).get("writer", "template"),
                                "models": (s.get("ai") or {}).get("models", {}),
                                "default_models": router.DEFAULT_MODELS,
                                "keys": {k: bool(v) for k, v in self.secrets.load().items()},
                                "clis": {"claude-cli": cfg.claude_cli.exists(),
                                         "codex-cli": bool(shutil.which("codex"))}}},
            "ccswitch_detected": cfg.db.exists(),
            "platform": "windows" if WINDOWS else "macos" if MACOS else "linux",
        }

    # ------------------------------------------------------------- handlers

    def get(self, path: str, q: dict):
        if path == "/api/state":
            return self.state()
        if path == "/api/folder":
            return self.model.folder_view(self.model.load(), q["path"][0])
        if path == "/api/scan":
            return {"projects": self.model.scan()}
        if path == "/api/insights":
            days = int((q.get("days") or ["60"])[0])
            if not 1 <= days <= 3650:
                raise OpError("days must be between 1 and 3650")
            return self.model.insights(days)
        if path == "/api/mcp/detail":
            name = q["name"][0]
            cat = self.model.mcp_catalog()
            if name not in cat:
                raise OpError(f"no MCP server {name!r}")
            return cat.public(name)
        raise OpError(f"unknown endpoint {path}")

    def post(self, path: str, b: dict):
        m = self.model
        if path == "/api/op":
            return m.run(b["op"], b.get("args", {}), dry=bool(b.get("dry", True)))
        if path == "/api/settings":
            s = self.settings.update(b)
            if "mode" in b or "scan_roots" in b or "agents" in b:
                self.build()
            return s
        if path == "/api/ping":
            self.last_ping = time.time()
            return {"ok": True}
        if path == "/api/pick-folder":
            return {"folder": pick_folder(b.get("start"))}
        if path == "/api/open":
            if not Path(b["path"]).exists():
                raise OpError("path does not exist")
            open_path(b["path"])
            return {"ok": True}
        if path == "/api/open-url":
            url = str(b.get("url", ""))
            if not url.startswith(("https://", "http://")) or any(c in url for c in "\n\r\t \"'<>"):
                raise OpError("only http(s) links can be opened")
            webbrowser.open(url)  # the user's default browser, not the app window
            return {"ok": True}
        if path == "/api/migrate":
            return self.migrate(b)
        if path == "/api/router/draft":
            data = m.load()
            if b["pack"] not in data["packs"]:
                raise OpError(f"no such pack: {b['pack']!r}")
            ai = self.settings.load().get("ai") or {}
            writer = b.get("writer") or ai.get("writer") or "template"
            skills = m.router_skills(data, b["pack"])
            if not skills:
                raise OpError("add skills to the pack first")
            draft = router.write_with_ai(writer, b["pack"], skills, claude_cli=m.cfg.claude_cli,
                                         model=(ai.get("models") or {}).get(writer, ""),
                                         api_key=self.secrets.load().get(writer, ""))
            return draft | {"writer": writer, "skills": [s["name"] for s in skills],
                            "descriptions": {s["name"]: router.one_line(s["description"]) for s in skills}}
        if path == "/api/secret":
            if b.get("name") not in ("anthropic", "openai"):
                raise OpError("unknown key")
            self.secrets.set(b["name"], (b.get("value") or "").strip() or None)
            return {"ok": True}
        if path == "/api/usage":
            if not self.usage_job["running"]:
                self.usage_job.update(running=True, error=None)
                threading.Thread(target=self._usage, daemon=True).start()
            return {"started": True}

        # ---- standalone: skills from repositories
        if path == "/api/repo/refs":
            self.local()
            return repo.refs(b["url"])
        if path == "/api/repo/inspect":
            src = self.local()
            info = self.ws.clone(b["url"], b["ref"])
            meta = src._load()["skills"]
            for s in info["skills"]:
                prev = (meta.get(s["dir"]) or {}).get("source") or {}
                s["in_store"] = (src.store / s["dir"]).exists()
                s["same_source"] = (prev.get("url"), prev.get("path")) == (info["url"], s["path"])
            return info
        if path == "/api/repo/install":
            return {"log": self.local().install(self.ws, b["id"], b.get("paths", []), bool(b.get("replace")))}
        if path == "/api/repo/cancel":
            self.ws.drop(b.get("id", ""))
            return {"ok": True}
        if path == "/api/skill/import":
            return {"log": [self.local().import_folder(b["folder"], bool(b.get("replace")))]}
        if path == "/api/skill/update-preview":
            return self.local().update_preview(self.ws, b["dir"], b.get("ref") or None)
        if path == "/api/skill/update":
            return {"log": [self.local().update(self.ws, b["dir"], b.get("ref") or None, b.get("id") or None)]}
        if path == "/api/skill/history":
            return {"versions": self.local().history(b["dir"])}
        if path == "/api/skill/rollback":
            return {"log": [self.local().rollback(b["dir"], b["id"])]}
        if path == "/api/skill/updates":
            return {"updates": self.check_updates()}
        if path == "/api/skill/remove":
            return {"log": [self.local().remove_skill(b["dir"], m.load())]}
        if path == "/api/skill/global":
            return {"log": [self.local().set_skill_global(b["dir"], b["client"], bool(b["on"]))]}

        # ---- standalone: MCP catalog
        if path == "/api/mcp/save":
            return {"log": [self.local().save_mcp(b["name"], b["config"], b.get("description", ""),
                                                  b.get("keep_env"), b.get("keep_headers"), bool(b.get("new", True)))]}
        if path == "/api/mcp/import-preview":
            servers = LocalSource.parse_import(b["text"])
            cat = mcp.Catalog({n: {"config": c, "global": [], "description": ""} for n, c in servers.items()})
            existing = self.local().mcp_catalog()
            return {"servers": [cat.public(n) | {"exists": n in existing} for n in servers]}
        if path == "/api/mcp/import":
            src = self.local()
            servers = LocalSource.parse_import(b["text"])
            existing = src.mcp_catalog()
            log = []
            for n, c in servers.items():
                if n in existing and not b.get("replace"):
                    log.append(f"skipped {n}: already exists")
                    continue
                log.append(src.save_mcp(n, c, new=n not in existing))
            return {"log": log}
        if path == "/api/mcp/delete":
            return {"log": [self.local().delete_mcp(b["name"], m.load())]}
        if path == "/api/mcp/global":
            return {"log": [self.local().set_mcp_global(b["name"], b["client"], bool(b["on"]))]}
        raise OpError(f"unknown endpoint {path}")

    def check_updates(self) -> dict:
        """Check every installed skill and keep the result, so the Skills tab
        shows it after a restart and the daily check knows when it last ran."""
        u = self.local().check_updates()
        f = self.data_dir / "updates.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"at": int(time.time()), "updates": u}), encoding="utf8")
        return u

    def cached_updates(self) -> dict:
        try:
            return json.loads((self.data_dir / "updates.json").read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}

    def _usage(self):
        try:
            self.model.usage()
        except Exception as e:  # shown in the UI
            self.usage_job["error"] = str(e)
        finally:
            self.usage_job["running"] = False


def pick_folder(start: str | None) -> str | None:
    """Native folder dialog. Tk runs in its own thread per call."""
    result: dict = {}

    def run():
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            result["error"] = "No folder dialog available (tkinter is missing); paste the path instead"
            return
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        result["path"] = filedialog.askdirectory(parent=root, initialdir=start or str(Path.home()),
                                                 title="Choose a folder", mustexist=True)
        root.destroy()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "error" in result:
        raise OpError(result["error"])
    p = result.get("path")
    return os.path.normpath(p) if p else None


def make_handler(app: App, port_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _allowed(self) -> bool:
            host_ok = self.headers.get("Host") == f"127.0.0.1:{port_ref['port']}"
            token_ok = secrets.compare_digest(self.headers.get("X-SPM-Token", ""), app.token)
            return host_ok and token_ok

        def _dispatch(self, fn):
            if not self._allowed():
                return self._json(403, {"error": "forbidden"})
            try:
                self._json(200, fn())
            except USER_ERRORS as e:
                self._json(400, {"error": str(e).strip("'\"")})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, app.index_html(), "text/html; charset=utf-8")
            if u.path == "/icon.png":
                return self._send(200, (STATIC / "icon.png").read_bytes(), "image/png")
            if u.path.startswith("/api/"):
                return self._dispatch(lambda: app.get(u.path, parse_qs(u.query)))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/bye":
                # navigator.sendBeacon cannot set headers, so the token comes in the query
                token = (parse_qs(u.query).get("t") or [""])[0]
                if self.headers.get("Host") != f"127.0.0.1:{port_ref['port']}" or \
                        not secrets.compare_digest(token, app.token):
                    return self._json(403, {"error": "forbidden"})
                app.bye_at = time.time()
                return self._json(200, {"ok": True})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._json(413, {"error": "too large"})
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            self._dispatch(lambda: app.post(u.path, body))

    return Handler


def serve(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    port_ref = {"port": port}
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port_ref["port"]
