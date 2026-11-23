"""User settings in <data dir>/settings.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("cc-switch", "standalone")
THEMES = ("light", "dark", "system")


class Settings:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            s = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            s = {}
        if s.get("theme") not in THEMES:
            s["theme"] = "system"
        if s.get("mode") not in MODES:
            s["mode"] = None  # first run: the UI asks
        if not isinstance(s.get("scan_roots"), list):
            s["scan_roots"] = None  # None = platform defaults
        if not isinstance(s.get("auto_update_check"), bool):
            s["auto_update_check"] = True  # standalone: check skill updates once a day when the app opens
        return s

    def update(self, body: dict) -> dict:
        s = self.load()
        if "theme" in body:
            if body["theme"] not in THEMES:
                raise ValueError(f"theme must be one of {', '.join(THEMES)}")
            s["theme"] = body["theme"]
        if "mode" in body:
            if body["mode"] not in MODES:
                raise ValueError(f"mode must be one of {', '.join(MODES)}")
            s["mode"] = body["mode"]
        if "auto_update_check" in body:
            if not isinstance(body["auto_update_check"], bool):
                raise ValueError("auto_update_check must be true or false")
            s["auto_update_check"] = body["auto_update_check"]
        if "ai" in body:
            from .router import WRITERS
            ai = body["ai"] if isinstance(body["ai"], dict) else {}
            cur = s.get("ai") if isinstance(s.get("ai"), dict) else {}
            if "writer" in ai:
                if ai["writer"] not in WRITERS:
                    raise ValueError(f"writer must be one of {', '.join(WRITERS)}")
                cur["writer"] = ai["writer"]
            for k, v in (ai.get("models") or {}).items():
                if k in WRITERS and isinstance(v, str) and len(v) < 100:
                    cur.setdefault("models", {})[k] = v.strip()
            s["ai"] = cur
        if "scan_roots" in body:
            roots = body["scan_roots"]
            if not isinstance(roots, list) or not all(isinstance(r, str) and r.strip() for r in roots):
                raise ValueError("scan_roots must be a list of folder paths")
            s["scan_roots"] = [os.path.normpath(r.strip()) for r in roots]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2) + "\n", encoding="utf8")
        os.replace(tmp, self.path)
        return s
