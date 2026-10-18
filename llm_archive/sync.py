"""Portable, per-device snapshots in a dedicated private Git repository.

SQLite and agent credential/config files are never copied. Network failures leave
local capture intact; unpushed commits are retried by the next checkpoint.
"""
import json
import subprocess
from pathlib import Path

from llm_archive import db
from llm_archive.parsers import ParsedConversation, ParsedEvent, ParsedMessage
from llm_archive.paths import atomic_write, collector_lock, data_dir, device_id, read_config

MARKER = "journal-format.json"


def _git(root: Path, *args: str, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True,
                            timeout=45, env=_git_environment())
    if check and result.returncode:
        raise RuntimeError(f"Journal Git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result


def _git_environment():
    import os
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes"}


def configure(remote: str) -> Path:
    root = data_dir() / "sync"
    if root.exists():
        raise RuntimeError(f"Sync directory already exists: {root}. Existing data was left intact.")
    root.parent.mkdir(parents=True, exist_ok=True)
    # A separate repository prevents publishing the journal with this project's source.
    result = subprocess.run(["git", "clone", "--", remote, str(root)], text=True,
                            capture_output=True, timeout=45, env=_git_environment())
    if result.returncode:
        raise RuntimeError(f"Could not clone journal repository: {result.stderr.strip()}")
    _git(root, "config", "user.name", "llm-archive")
    _git(root, "config", "user.email", "llm-archive@localhost")
    # Journal snapshots are machine-generated data commits, not source commits.
    # They must work unattended without an interactive signing-key prompt.
    _git(root, "config", "commit.gpgsign", "false")
    tracked = _git(root, "ls-files").stdout.splitlines()
    if not (root / MARKER).exists() and set(tracked) - {"README.md", ".gitignore", "LICENSE"}:
        raise RuntimeError("Use a dedicated empty private repository for journal data. Clone left intact for inspection.")
    if (root / MARKER).exists():
        _check_format(root)
    else:
        atomic_write(root / MARKER, json.dumps({"format": "llm-archive-journal", "version": 1}) + "\n")
    config = read_config()
    config["sync_directory"] = str(root)
    atomic_write(data_dir() / "config.json", json.dumps(config, indent=2) + "\n")
    return root


def _check_format(root: Path):
    marker = json.loads((root / MARKER).read_text())
    if marker != {"format": "llm-archive-journal", "version": 1}:
        raise RuntimeError("Unsupported journal sync format")


def export_device(conn, root: Path) -> Path:
    records = []
    for row in conn.execute("SELECT * FROM conversations WHERE origin_device = ? ORDER BY session_id", (device_id(),)):
        record = dict(row)
        cid = record.pop("id")
        record["messages"] = [dict(m) for m in conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))]
        record["events"] = [dict(e) for e in conn.execute(
            "SELECT event_id, kind, content, timestamp FROM events WHERE conversation_id = ? ORDER BY rowid", (cid,))]
        records.append(record)
    path = root / "devices" / device_id() / "sessions.jsonl"
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    if not path.exists() or path.read_text() != text:
        atomic_write(path, text)
    return path


def import_devices(conn, root: Path) -> int:
    imported = 0
    own = device_id()
    for path in sorted((root / "devices").glob("*/sessions.jsonl")):
        if path.parent.name == own:
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                    existing = conn.execute("SELECT collected_at FROM conversations WHERE session_id = ?", (record["session_id"],)).fetchone()
                    if existing and (existing[0] or "") >= record["collected_at"]:
                        continue
                    conv = ParsedConversation(
                        session_id=record["session_id"], source=record["source"], project=record["project"],
                        git_branch=record.get("git_branch"), started_at=record.get("started_at"),
                        messages=[ParsedMessage(**m) for m in record["messages"]],
                        events=[ParsedEvent(**e) for e in record.get("events", [])],
                        cwd=record.get("cwd", ""), is_subagent=bool(record.get("is_subagent")),
                    )
                    db.upsert_conversation(conn, conv, metadata=record)
                    imported += 1
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError(f"Invalid journal record in {path}: {exc}") from exc
    return imported


def sync_if_configured() -> int:
    config = read_config()
    if not config.get("sync_directory"):
        return 0
    return run_sync(Path(config["sync_directory"]))


def run_sync(root: Path) -> int:
    with collector_lock():
        conn = db.get_connection()
        try:
            _check_format(root)
            export_device(conn, root)
            _git(root, "add", "--", MARKER, f"devices/{device_id()}")
            if _git(root, "diff", "--cached", "--quiet", check=False).returncode:
                _git(root, "-c", "user.name=llm-archive", "-c", "user.email=llm-archive@localhost",
                     "commit", "-m", "Checkpoint work journal")
            # Each device owns a separate path. Rebase unpushed checkpoints after
            # fetching other machines; never force-push or reset local history.
            _git(root, "fetch", "origin")
            branch = _git(root, "branch", "--show-current").stdout.strip()
            remote_ref = f"refs/remotes/origin/{branch}"
            if _git(root, "show-ref", "--verify", "--quiet", remote_ref, check=False).returncode == 0:
                rebased = _git(root, "rebase", remote_ref, check=False)
                if rebased.returncode:
                    _git(root, "rebase", "--abort", check=False)
                    raise RuntimeError("Journal sync has conflicting device records. Local capture is intact; inspect the sync repository.")
            imported = import_devices(conn, root)
            conn.commit()
            _git(root, "push", "-u", "origin", branch)
            db.set_state(conn, "last_sync", db.utc_now())
            db.set_state(conn, "sync_error", "")
            conn.commit()
            from llm_archive.journal import write_handoffs
            write_handoffs(conn)
            return imported
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            conn.rollback()
            db.set_state(conn, "sync_error", str(exc))
            conn.commit()
            raise RuntimeError(f"Journal sync failed; local capture is intact. {exc}") from exc
        finally:
            conn.close()
