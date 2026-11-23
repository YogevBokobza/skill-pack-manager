"""Install skills from git repositories (standalone mode).

The flow mirrors what cc-switch offers:
  1. refs(url): the default branch first, then release tags newest first
  2. inspect(url, ref): shallow clone into a temp folder, list every SKILL.md
  3. install(...): copy the chosen skill folders into the store and record
     where they came from (url, ref, commit, path inside the repo)

Git runs with protocol.ext disabled (the ext:: transport can run commands),
no terminal prompts, and no LFS smudge filters. Cloning never runs code from
the repository: hooks are not transferred and the checkout is plain files.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .osinfo import NO_WINDOW

URL = re.compile(r"^(https?://[^\s]+|ssh://[^\s]+|git@[A-Za-z0-9.-]+:[^\s]+|file://[^\s]+)$")
SAFE_GIT = ["-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user",
            "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
            "-c", "core.symlinks=false"]
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GCM_INTERACTIVE": "never"}
MAX_SKILL_BYTES = 20 * 1024 * 1024


class RepoError(Exception):
    pass


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not URL.match(url) or url.startswith("-"):
        raise RepoError("use an https://, ssh://, git@host:path or file:// git URL")
    return url


def git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
    try:
        r = subprocess.run(["git", *SAFE_GIT, *args], cwd=cwd, capture_output=True, text=True,
                           env=ENV, creationflags=NO_WINDOW, timeout=timeout)
    except FileNotFoundError as e:
        raise RepoError("git is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RepoError("git timed out") from e
    if r.returncode != 0:
        raise RepoError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git failed")
    return r.stdout


def web_url(url: str | None, ref: str | None = None, path: str | None = None) -> str | None:
    """Browser link to a skill folder at a ref: GitHub and GitLab get a deep
    link (tree/<ref>/<path>), other https hosts the repository page."""
    if not url:
        return None
    u = url.strip().removesuffix("/").removesuffix(".git")
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", u)
    if m:
        u = f"https://{m.group(1)}/{m.group(2)}"
    if not re.match(r"^https?://", u):
        return None
    host = re.sub(r"^https?://", "", u).split("/")[0].lower()
    sub = "" if not path or path == "." else "/" + path.strip("/")
    if ref and host in ("github.com", "www.github.com"):
        return f"{u}/tree/{ref}{sub}"
    if ref and host == "gitlab.com":
        return f"{u}/-/tree/{ref}{sub}"
    return u


def ref_kind(url: str, ref: str) -> str:
    """'tag' or 'branch' for a ref of a remote repository."""
    try:
        out = git(["ls-remote", "--tags", "--heads", check_url(url), ref], timeout=60)
    except RepoError:
        return "branch"
    refs = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
    return "tag" if any(r.startswith("refs/tags/") for r in refs) and f"refs/heads/{ref}" not in refs else "branch"


def version_key(tag: str):
    """Sort key: version numbers first (v1.10 > v1.9), a final release above its
    pre-releases (v2.0.0 > v2.0.0-rc1), then text. Tags without numbers sort last."""
    m = re.search(r"\d+(?:\.\d+)*", tag)
    nums = [int(n) for n in m.group(0).split(".")] if m else []
    pre = 0 if re.search(r"(alpha|beta|rc|pre|dev|preview)", tag, re.I) else 1
    return (bool(nums), nums, pre, tag)


def refs(url: str) -> dict:
    url = check_url(url)
    head = git(["ls-remote", "--symref", url, "HEAD"], timeout=60)
    default, head_sha = None, None
    for line in head.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            default = line[5:].split("\t")[0].removeprefix("refs/heads/")
        elif line.endswith("\tHEAD"):
            head_sha = line.split("\t")[0]
    out = git(["ls-remote", "--tags", "--heads", url], timeout=60)
    tags, branches = {}, {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/"):
            name = ref[10:]
            if name.endswith("^{}"):
                tags[name[:-3]] = sha  # peeled commit of an annotated tag wins
            else:
                tags.setdefault(name, sha)
        elif ref.startswith("refs/heads/"):
            branches[ref[11:]] = sha
    default = default or ("main" if "main" in branches else "master" if "master" in branches else None)
    items = []
    if default:
        items.append({"ref": default, "kind": "branch", "commit": branches.get(default, head_sha), "default": True})
    for name in sorted(tags, key=version_key, reverse=True):
        items.append({"ref": name, "kind": "tag", "commit": tags[name], "default": False})
    others = sorted(b for b in branches if b != default)
    return {"url": url, "refs": items, "branches": others}


def find_skills(root: Path, max_depth: int = 5) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules") and depth < max_depth]
        if "SKILL.md" in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []  # a skill's own sub-folders are its resources
    return sorted(found)


def frontmatter(md: str) -> dict:
    if not md.startswith("---"):
        return {}
    block = md.split("---", 2)[1]
    out, key = {}, None
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            out[key] = "" if val in (">", "|", ">-", "|-") else val.strip('"\'')
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def folder_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


class Workspace:
    """Temp clones, keyed by an id handed to the UI between inspect and install."""

    def __init__(self, base: Path):
        self.base = base
        self.clones: dict[str, dict] = {}

    def cleanup_old(self, max_age: float = 3600) -> None:
        if not self.base.is_dir():
            return
        for d in self.base.iterdir():
            if time.time() - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
                self.clones.pop(d.name, None)

    def clone(self, url: str, ref: str) -> dict:
        url = check_url(url)
        if not ref or ref.startswith("-") or any(c in ref for c in " \t\n~^:?*[\\"):
            raise RepoError("invalid ref")
        self.cleanup_old()
        cid = uuid.uuid4().hex[:12]
        dest = self.base / cid
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--depth", "1", "--branch", ref, "--no-tags", "--single-branch", "--", url, str(dest)], timeout=300)
        commit = git(["rev-parse", "HEAD"], cwd=dest).strip()
        skills = []
        for d in find_skills(dest):
            md = (d / "SKILL.md").read_text(encoding="utf8", errors="replace")
            fm = frontmatter(md)
            rel = d.relative_to(dest).as_posix() or "."
            skills.append({"path": rel, "dir": d.name if rel != "." else url.rstrip("/").split("/")[-1].removesuffix(".git"),
                           "name": fm.get("name") or d.name, "description": fm.get("description", "")[:500],
                           "preview": md[:6000], "bytes": folder_size(d)})
        info = {"id": cid, "url": url, "ref": ref, "kind": ref_kind(url, ref), "commit": commit, "skills": skills}
        self.clones[cid] = {**info, "path": dest}
        return info

    def get(self, cid: str) -> dict:
        c = self.clones.get(cid)
        if not c or not c["path"].is_dir():
            raise RepoError("that download expired; inspect the repository again")
        return c

    def drop(self, cid: str) -> None:
        c = self.clones.pop(cid, None)
        if c:
            shutil.rmtree(c["path"], ignore_errors=True)


def copy_skill(src: Path, dst: Path) -> None:
    """Copy a skill folder into the store, replacing the content but keeping the
    folder itself (project links point at the folder path)."""
    if folder_size(src) > MAX_SKILL_BYTES:
        raise RepoError(f"{src.name} is larger than {MAX_SKILL_BYTES // 1024 // 1024} MB")
    dst.mkdir(parents=True, exist_ok=True)
    for child in dst.iterdir():
        shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    for child in src.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink():
            continue  # never copy links out of a downloaded repo
        if child.is_dir():
            shutil.copytree(child, dst / child.name, symlinks=False,
                            ignore=lambda d, names: [n for n in names if os.path.islink(os.path.join(d, n))])
        else:
            shutil.copy2(child, dst / child.name)
