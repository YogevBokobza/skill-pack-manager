"""Build a demo workspace with made-up projects, for screenshots and trying the
app without touching your own setup. Everything lives under one folder.

  python tools/make_demo.py <demo-dir>
  # then, as printed:
  SPM_DATA=<demo>/data SPM_HOME=<demo>/home SPM_PACKS=<demo>/packs.json \\
  SPM_CLAUDE_CLI=tools/fake_claude.py SPM_CLAUDE_JSON=<demo>/home/.claude.json python -m spm

Skills are installed from public GitHub repositories over the network.
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECTS = {
    "shop-frontend": {"package.json": '{"name": "shop-frontend"}'},
    "landing-page": {"index.html": "<h1>Hello</h1>"},
    "space-shooter": {"ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.30f1"},
    "racing-prototype": {"ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 6000.0.30f1"},
    "api-server": {"pyproject.toml": '[project]\nname = "api-server"'},
}


def main(root: Path):
    root = root.resolve()
    for name, files in PROJECTS.items():
        d = root / "projects" / name
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text, encoding="utf8")
        if not (d / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (root / "home").mkdir(exist_ok=True)
    data = root / "data"
    data.mkdir(exist_ok=True)
    (data / "settings.json").write_text(json.dumps(
        {"mode": "standalone", "theme": "light", "scan_roots": [str(root / "projects")]}, indent=2))
    print(f"demo workspace ready in {root}")
    print("start it with these environment variables:")
    for k, v in {"SPM_DATA": data, "SPM_HOME": root / "home", "SPM_PACKS": root / "packs.json",
                 "SPM_CLAUDE_CLI": Path(__file__).with_name("fake_claude.py"),
                 "SPM_CLAUDE_JSON": root / "home" / ".claude.json"}.items():
        print(f"  {k}={v}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
