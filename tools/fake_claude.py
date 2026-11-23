"""A stand-in for `claude mcp add-json/remove` that edits SPM_CLAUDE_JSON instead
of ~/.claude.json. For demos and manual tests with SPM_HOME:

  SPM_CLAUDE_CLI=tools/fake_claude.py SPM_CLAUDE_JSON=<sandbox>/.claude.json python -m spm
"""
import json
import os
import sys
from pathlib import Path


def main(argv):
    if len(argv) < 4 or argv[0] != "mcp" or argv[2] != "--scope":
        sys.exit("fake claude: only `mcp add-json|remove --scope local|user <name> [json]`")
    action, scope, name = argv[1], argv[3], argv[4]
    f = Path(os.environ["SPM_CLAUDE_JSON"])
    j = json.loads(f.read_text(encoding="utf8")) if f.exists() else {}
    if scope == "user":
        servers = j.setdefault("mcpServers", {})
    else:
        servers = j.setdefault("projects", {}).setdefault(os.getcwd().replace("\\", "/"), {}).setdefault("mcpServers", {})
    if action == "add-json":
        if name in servers:
            sys.exit(f"MCP server {name} already exists in {scope} config")
        servers[name] = json.loads(argv[5])
    elif action == "remove":
        if name not in servers:
            sys.exit(f"No MCP server named {name} in {scope} config")
        del servers[name]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(j, indent=2), encoding="utf8")


if __name__ == "__main__":
    main(sys.argv[1:])
