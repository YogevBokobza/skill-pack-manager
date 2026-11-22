"""Operating-system specific paths and helpers (Windows, macOS, Linux)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW; must be 0 elsewhere
HOME = Path.home()
APP = "skill-pack-manager"
APP_ROOT = Path(__file__).resolve().parents[1]

# Global skill folders each client reads, used in standalone mode for
# "enable globally". Project folders use the same names under the project.
GLOBAL_SKILL_DIRS = {
    "claude": HOME / ".claude" / "skills",
    "opencode": HOME / ".config" / "opencode" / "skills",
    "codex": HOME / ".codex" / "skills",
}
PROJECT_SKILL_DIRS = (".claude/skills", ".opencode/skills", ".agents/skills")


def data_dir() -> Path:
    if os.environ.get("SPM_DATA"):
        return Path(os.environ["SPM_DATA"])
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local")) / APP
    if MACOS:
        return HOME / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / APP


def claude_cli() -> Path:
    if os.environ.get("SPM_CLAUDE_CLI"):
        return Path(os.environ["SPM_CLAUDE_CLI"])
    found = shutil.which("claude")
    if found:
        return Path(found)
    return HOME / ".local" / "bin" / ("claude.exe" if WINDOWS else "claude")


def default_scan_roots() -> list[str]:
    names = ("Projects", "projects", "dev", "code", "src", "repos", "source/repos")
    return [str(HOME / n) for n in names if (HOME / n).is_dir()]


def open_path(p: str) -> None:
    if WINDOWS:
        os.startfile(p)  # noqa: S606 - local folder in Explorer
    elif MACOS:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def app_browser() -> list[str] | None:
    """A Chromium-family browser that supports --app windows, or None."""
    cands: list[Path | str] = []
    if WINDOWS:
        for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = Path(os.environ.get(v, ""))
            cands += [base / "Microsoft/Edge/Application/msedge.exe", base / "Google/Chrome/Application/chrome.exe"]
    elif MACOS:
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        cands += [shutil.which(n) or "" for n in
                  ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")]
    for c in cands:
        if c and Path(c).is_file():
            return [str(c)]
    return None


def restrict_file(p: Path) -> None:
    """Owner-only permissions for files that hold secrets (no-op on Windows,
    where the per-user data folder is already private)."""
    if not WINDOWS:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
