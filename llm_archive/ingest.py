"""Poll existing transcripts; agents need no wrappers, hooks, or config edits."""
import os
from pathlib import Path

from llm_archive import db
from llm_archive.parsers.claude import _parse_session as parse_claude, _extract_project
from llm_archive.parsers.codex import _parse_session as parse_codex
from llm_archive.parsers import normalize_project
from llm_archive.paths import collector_lock
from llm_archive.projects import repository


def source_roots() -> list[tuple[str, Path]]:
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    return [("claude", claude / "projects"), ("codex", codex / "sessions"),
            ("codex", codex / "archived_sessions")]


def run_ingest(force: bool = False) -> tuple[int, int]:
    with collector_lock():
        return _run_ingest(force)


def _run_ingest(force: bool = False) -> tuple[int, int]:
    conn = db.get_connection()
    updated = new_msgs = 0
    errors = []
    repository.cache_clear()
    try:
        for source, root in source_roots():
            if not root.is_dir():
                continue
            for path in root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                    key = str(path.resolve())
                    previous = conn.execute("SELECT size, mtime_ns FROM source_files WHERE path = ?", (key,)).fetchone()
                    if not force and previous and tuple(previous) == (stat.st_size, stat.st_mtime_ns):
                        continue
                    conv = (parse_codex(path) if source == "codex" else
                            parse_claude(path, normalize_project(_extract_project(path.parent.name))))
                    if conv is None:
                        raise OSError("Could not read transcript")
                    if conv.messages or conv.events:
                        _, added = db.upsert_conversation(conn, conv)
                        updated += 1
                        new_msgs += added
                    # Use the pre-read signature, so appends during parsing are retried.
                    conn.execute("INSERT OR REPLACE INTO source_files VALUES (?, ?, ?)",
                                 (key, stat.st_size, stat.st_mtime_ns))
                    conn.commit()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
        db.set_state(conn, "last_check", db.utc_now())
        if not errors:
            db.set_state(conn, "last_success", db.utc_now())
        db.set_state(conn, "capture_error", "\n".join(errors[:20]))
        conn.commit()
        if errors:
            raise RuntimeError(f"Checkpoint incomplete: {len(errors)} unreadable transcript(s). See llm-archive status.")
        return updated, new_msgs
    finally:
        conn.close()
