"""Read-only repository discovery. Worktrees share a project, but retain their cwd."""
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from llm_archive.parsers import normalize_project


def git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def canonical_remote(remote: str) -> str:
    if "://" in remote:
        parsed = urlsplit(remote)
        if not parsed.hostname:
            return ""
        remote = parsed.hostname.lower() + parsed.path
    else:
        remote = re.sub(r"^[^/@]+@", "", remote).replace(":", "/", 1)
    return remote.removesuffix(".git").rstrip("/")


@lru_cache(maxsize=1024)
def repository(cwd: str) -> dict:
    root = git(cwd, "rev-parse", "--show-toplevel") if cwd else ""
    if not root:
        return {"cwd": cwd, "repo_root": "", "project_key": "directory:" + cwd,
                "repo_url": "", "project": normalize_project(Path(cwd).name) if cwd else "unknown"}
    common = git(cwd, "rev-parse", "--git-common-dir")
    common_path = (Path(cwd) / common).resolve()
    remote = canonical_remote(git(cwd, "remote", "get-url", "origin"))
    name = remote.rsplit("/", 1)[-1] if remote else common_path.parent.name
    return {"cwd": cwd, "repo_root": root, "project_key": remote or "git:" + str(common_path),
            "repo_url": remote, "project": normalize_project(name)}
