"""Read-only repository discovery. Worktrees share a project, but retain their cwd."""
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from llm_archive.parsers import normalize_project


def git_output(cwd: str, *args: str, timeout: float = 5) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git(cwd: str, *args: str) -> str:
    return git_output(cwd, *args) or ""


def canonical_remote(remote: str) -> str:
    if "://" in remote:
        parsed = urlsplit(remote)
        if not parsed.hostname:
            return ""
        remote = parsed.hostname.lower() + parsed.path
    else:
        remote = re.sub(r"^[^/@]+@", "", remote).replace(":", "/", 1)
    return remote.removesuffix(".git").rstrip("/")


CLAUDE_WORKTREES = "/.claude/worktrees/"
CLAUDE_SCRATCHPAD = re.compile(r"^(?:/private)?/tmp/claude-\d+/(-[^/]+)/")


def claude_encoded(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


@lru_cache(maxsize=256)
def claude_launch_dir(name: str) -> str:
    """Claude names directories after the launch cwd with every non-alphanumeric character as '-'."""
    def walk(path: Path, rest: str) -> str:
        if not rest:
            return str(path)
        try:
            entries = sorted(os.listdir(path), key=len, reverse=True)
        except OSError:
            return ""
        for entry in entries:
            encoded = claude_encoded(entry)
            if rest == encoded or rest.startswith(encoded + "-"):
                found = walk(path / entry, rest[len(encoded) + 1:])
                if found:
                    return found
        return ""
    return walk(Path("/"), name[1:]) if name.startswith("-") else ""


def missing_repository(cwd: str, recorded_remote: str) -> dict:
    """A deleted worktree cannot be asked; use the agent's recorded remote or the checkout it lived in."""
    remote = canonical_remote(recorded_remote) if recorded_remote else ""
    if remote:
        return {"cwd": cwd, "repo_root": "", "project_key": remote, "repo_url": remote,
                "project": normalize_project(remote.rsplit("/", 1)[-1])}
    parent = ""
    if CLAUDE_WORKTREES in cwd:
        parent = cwd.split(CLAUDE_WORKTREES, 1)[0]
    elif match := CLAUDE_SCRATCHPAD.match(cwd):
        parent = claude_launch_dir(match.group(1))
    if parent and parent != cwd:
        return {**repository(parent), "cwd": cwd, "repo_root": ""}
    return {"cwd": cwd, "repo_root": "", "project_key": "directory:" + cwd,
            "repo_url": "", "project": normalize_project(Path(cwd).name) if cwd else "unknown"}


@lru_cache(maxsize=1024)
def repository(cwd: str, recorded_remote: str = "") -> dict:
    root = git(cwd, "rev-parse", "--show-toplevel") if cwd else ""
    if not root:
        return missing_repository(cwd, recorded_remote)
    common = git(cwd, "rev-parse", "--git-common-dir")
    common_path = (Path(cwd) / common).resolve()
    remote = canonical_remote(git(cwd, "remote", "get-url", "origin"))
    name = remote.rsplit("/", 1)[-1] if remote else common_path.parent.name
    return {"cwd": cwd, "repo_root": root, "project_key": remote or "git:" + str(common_path),
            "repo_url": remote, "project": normalize_project(name)}
