# Contributing

Thanks for helping. Bug reports, fixes and small features are welcome.

## Ground rules

- **Standard library only.** The app must run with a plain Python 3.12+ install
  and `git`. Please don't add dependencies.
- **Never delete what the program didn't create.** Every removal goes through a
  check that the target is a link into the store (`core.safe_unlink`) or an
  entry whose name is in the catalog, in a file git does not track
  (`mcp.plan`). New features that remove things need a test that proves
  the user's own files survive.
- **Secrets stay out of the API.** MCP env values and headers are written to the
  agents' config files but never returned to the UI. `tests/test_mcp.py` and
  `tests/test_standalone.py` check this; keep them passing.
- **Windows, macOS and Linux.** Junctions on Windows, symlinks elsewhere. CI runs
  the tests on all three.

## Development

```sh
python -m unittest -v              # all tests
python -m spm --no-window          # serve without opening a window; prints the URL
```

To try the app without touching your own agent setup, build the demo workspace
(made-up projects, skills installed from public GitHub repositories):

```sh
python tools/make_demo.py /tmp/spm-demo
```

and start the app with the environment variables it prints. `SPM_HOME`
redirects every agent folder into the demo, and `tools/fake_claude.py` stands in
for the `claude` CLI so nothing is written to your real `~/.claude.json`.

## Layout

| Path | Role |
|---|---|
| `spm/core.py` | packs.json model, desired vs actual state, plans, links, scan, usage |
| `spm/mcp.py` | MCP catalog, Claude / OpenCode / Codex adapters (project and global), MCP plans |
| `spm/sources.py` | where skills and MCP servers come from: cc-switch, or the program's own store |
| `spm/repo.py` | git refs (branch, tags sorted by version), shallow clones, finding SKILL.md |
| `spm/usage.py` | counts skill use in past Claude Code, OpenCode and Codex sessions |
| `spm/server.py` | local HTTP API (token + Host check) |
| `spm/static/index.html` | the whole UI, plain JavaScript, no build step |
| `spm/__main__.py` | launcher: server + app window |
| `tools/` | demo builder, fake Claude CLI, icon generator |

## Pull requests

Keep them focused, describe what changed and why, list the files you touched,
and include tests for behavior changes.
