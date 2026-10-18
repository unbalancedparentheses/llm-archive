"""Portable, per-device snapshots in a dedicated private Git repository.

SQLite and agent credential/config files are never copied. Network failures leave
local capture intact; unpushed commits are retried by the next checkpoint.
"""
import json
import gzip
import hashlib
import subprocess
from pathlib import Path

from llm_archive import db
from llm_archive.parsers import ParsedConversation, ParsedEvent, ParsedMessage
from llm_archive.paths import atomic_write, collector_lock, data_dir, device_id, read_config

MARKER = "journal-format.json"


def _git(root: Path, *args: str, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True,
                            timeout=180 if args and args[0] == "push" else 45, env=_git_environment())
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
        atomic_write(root / MARKER, json.dumps({"format": "llm-archive-journal", "version": 2}) + "\n")
    config = read_config()
    config["sync_directory"] = str(root)
    atomic_write(data_dir() / "config.json", json.dumps(config, indent=2) + "\n")
    return root


def _check_format(root: Path):
    marker = json.loads((root / MARKER).read_text())
    if marker.get("format") != "llm-archive-journal" or marker.get("version") not in (1, 2):
        raise RuntimeError("Unsupported journal sync format")


def export_device(conn, root: Path) -> Path:
    from llm_archive.workspaces import export_local
    folder = root / "devices" / device_id()
    folder.mkdir(parents=True, exist_ok=True)
    records_dir = folder / "sessions"
    records_dir.mkdir(exist_ok=True)
    for row in conn.execute("SELECT * FROM conversations WHERE origin_device = ? ORDER BY session_id", (device_id(),)):
        cid = row["id"]
        record = db.conversation_record(conn, cid)
        record["revisions"] = [json.loads(r[0]) for r in conn.execute(
            "SELECT snapshot FROM conversation_revisions WHERE conversation_id = ? ORDER BY revision_key", (cid,))]
        # Bound Git object size to one compressed session, not the entire archive.
        name = hashlib.sha256(record["session_id"].encode()).hexdigest() + ".json.gz"
        path = records_dir / name
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        compressed = gzip.compress(encoded, mtime=0)
        if not path.exists() or path.read_bytes() != compressed:
            from llm_archive.paths import atomic_write_bytes
            atomic_write_bytes(path, compressed)
    # The database is authoritative for this generated snapshot. Historical
    # versions remain in records and previously published Git history.
    (folder / "sessions.jsonl").unlink(missing_ok=True)
    atomic_write(root / MARKER, json.dumps({"format": "llm-archive-journal", "version": 2}) + "\n")
    workspaces_path = folder / "workspaces.json"
    workspace_text = json.dumps(export_local(), indent=2, sort_keys=True) + "\n"
    if not workspaces_path.exists() or workspaces_path.read_text() != workspace_text:
        atomic_write(workspaces_path, workspace_text)
    return records_dir


def _device_records(folder: Path):
    legacy = folder / "sessions.jsonl"
    if legacy.exists():
        with legacy.open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)
    for path in sorted((folder / "sessions").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            yield json.load(stream)


def import_devices(conn, root: Path) -> int:
    from llm_archive.workspaces import import_foreign
    imported = 0
    own = device_id()
    for folder in sorted((root / "devices").iterdir()):
        if not folder.is_dir() or folder.name == own:
            continue
        workspace_path = folder / "workspaces.json"
        if workspace_path.exists():
            import_foreign(folder.name, json.loads(workspace_path.read_text()))
        for record in _device_records(folder):
            try:
                existing = conn.execute("SELECT id, collected_at FROM conversations WHERE session_id = ?", (record["session_id"],)).fetchone()
                if existing:
                    for snapshot in record.get("revisions", []):
                        if snapshot.get("session_id") != record["session_id"]:
                            raise ValueError("Revision belongs to another session")
                        db.store_revision(conn, existing["id"], snapshot)
                if existing and (existing["collected_at"] or "") >= record["collected_at"]:
                    continue
                conv = ParsedConversation(
                    session_id=record["session_id"], source=record["source"], project=record["project"],
                    git_branch=record.get("git_branch"), started_at=record.get("started_at"),
                    messages=[ParsedMessage(**m) for m in record["messages"]],
                    events=[ParsedEvent(**e) for e in record.get("events", [])],
                    cwd=record.get("cwd", ""), is_subagent=bool(record.get("is_subagent")),
                )
                db.upsert_conversation(conn, conv, metadata=record)
                if not existing:
                    cid = conn.execute("SELECT id FROM conversations WHERE session_id = ?", (record["session_id"],)).fetchone()[0]
                    for snapshot in record.get("revisions", []):
                        if snapshot.get("session_id") != record["session_id"]:
                            raise ValueError("Revision belongs to another session")
                        db.store_revision(conn, cid, snapshot)
                imported += 1
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"Invalid journal record in {folder}: {exc}") from exc
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
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, EOFError, KeyError, TypeError) as exc:
            conn.rollback()
            db.set_state(conn, "sync_error", str(exc))
            conn.commit()
            raise RuntimeError(f"Journal sync failed; local capture is intact. {exc}") from exc
        finally:
            conn.close()
