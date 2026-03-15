import sqlite3
from pathlib import Path

from llm_archive.parsers import ParsedConversation

DB_DIR = Path.home() / ".local" / "share" / "llm-archive"
DB_PATH = DB_DIR / "archive.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    session_id TEXT UNIQUE,
    source TEXT,
    project TEXT,
    git_branch TEXT,
    started_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role TEXT,
    content TEXT,
    timestamp TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
"""


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM conversations WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row is not None


def insert_conversation(conn: sqlite3.Connection, conv: ParsedConversation) -> None:
    cur = conn.execute(
        "INSERT INTO conversations (session_id, source, project, git_branch, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv.session_id, conv.source, conv.project, conv.git_branch, conv.started_at),
    )
    conv_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO messages (conversation_id, role, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        [(conv_id, m.role, m.content, m.timestamp) for m in conv.messages],
    )


def search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    source: str | None = None,
    limit: int = 20,
) -> list[dict]:
    sql = """
        SELECT c.source, c.project, c.git_branch, m.role, m.timestamp,
               snippet(messages_fts, 0, '>>>', '<<<', '...', 30) as snippet
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        JOIN conversations c ON c.id = m.conversation_id
        WHERE messages_fts MATCH ?
    """
    params: list = [query]

    if project:
        sql += " AND c.project = ?"
        params.append(project)
    if source:
        sql += " AND c.source = ?"
        params.append(source)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    total_convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    by_source = conn.execute(
        "SELECT source, COUNT(*) as convs FROM conversations GROUP BY source"
    ).fetchall()

    by_project = conn.execute(
        "SELECT project, COUNT(*) as convs FROM conversations "
        "GROUP BY project ORDER BY convs DESC LIMIT 20"
    ).fetchall()

    return {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "by_source": [dict(r) for r in by_source],
        "by_project": [dict(r) for r in by_project],
    }


def timeline(
    conn: sqlite3.Connection,
    days: int = 14,
    project: str | None = None,
) -> list[dict]:
    sql = """
        SELECT date(c.started_at) as day,
               c.source,
               c.project,
               COUNT(DISTINCT c.id) as convs,
               COUNT(m.id) as msgs
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.id
        WHERE c.started_at >= date('now', ?)
    """
    params: list = [f"-{days} days"]

    if project:
        sql += " AND c.project = ?"
        params.append(project)

    sql += " GROUP BY day, c.source, c.project ORDER BY day DESC, convs DESC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def daily_detail(
    conn: sqlite3.Connection,
    days: int = 7,
    project: str | None = None,
) -> list[dict]:
    sql = """
        SELECT date(c.started_at) as day,
               time(c.started_at) as time,
               c.source,
               c.project,
               c.session_id,
               COUNT(m.id) as msgs,
               MIN(m.timestamp) as first_msg,
               MAX(m.timestamp) as last_msg
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.id
        WHERE c.started_at >= date('now', ?)
    """
    params: list = [f"-{days} days"]

    if project:
        sql += " AND c.project = ?"
        params.append(project)

    sql += " GROUP BY c.id ORDER BY c.started_at DESC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def message_timestamps(conn: sqlite3.Connection, days: int = 30) -> list[tuple[str, str]]:
    sql = """
        SELECT date(m.timestamp) as day, m.timestamp
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.timestamp != '' AND m.timestamp >= date('now', ?)
        ORDER BY m.timestamp
    """
    return conn.execute(sql, [f"-{days} days"]).fetchall()


def project_timestamps(conn: sqlite3.Connection, days: int = 30) -> list[tuple[str, str, str, str]]:
    sql = """
        SELECT c.project, date(m.timestamp) as day, m.timestamp, c.source
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.timestamp != '' AND m.timestamp >= date('now', ?)
        ORDER BY m.timestamp
    """
    return conn.execute(sql, [f"-{days} days"]).fetchall()


def day_detail(conn: sqlite3.Connection, date: str) -> list[dict]:
    sql = """
        SELECT c.id, c.source, c.project, c.session_id,
               time(c.started_at) as start_time,
               COUNT(m.id) as msgs,
               (SELECT content FROM messages
                WHERE conversation_id = c.id AND role = 'user'
                ORDER BY timestamp LIMIT 1) as first_question
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.id
        WHERE date(c.started_at) = ?
        GROUP BY c.id
        ORDER BY c.started_at
    """
    rows = conn.execute(sql, [date]).fetchall()
    return [dict(r) for r in rows]


def summarize_text(conn: sqlite3.Connection, days: int = 7) -> str:
    sql = """
        SELECT c.source, c.project, m.role, m.content, m.timestamp
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.timestamp >= date('now', ?)
        ORDER BY m.timestamp
    """
    rows = conn.execute(sql, [f"-{days} days"]).fetchall()

    parts = []
    for r in rows:
        parts.append(f"[{r['source']}/{r['project']}] {r['role']}: {r['content'][:500]}")
    return "\n".join(parts)


def export_conversations(conn: sqlite3.Connection, days: int = 30, project: str | None = None, source: str | None = None) -> list[dict]:
    sql = """
        SELECT c.id, c.session_id, c.source, c.project, c.git_branch, c.started_at
        FROM conversations c
        WHERE c.started_at >= date('now', ?)
    """
    params: list = [f"-{days} days"]
    if project:
        sql += " AND c.project = ?"
        params.append(project)
    if source:
        sql += " AND c.source = ?"
        params.append(source)
    sql += " ORDER BY c.started_at"

    convs = conn.execute(sql, params).fetchall()
    results = []
    for c in convs:
        msgs = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE conversation_id = ? ORDER BY timestamp",
            (c["id"],)
        ).fetchall()
        results.append({
            "session_id": c["session_id"],
            "source": c["source"],
            "project": c["project"],
            "git_branch": c["git_branch"],
            "started_at": c["started_at"],
            "messages": [dict(m) for m in msgs],
        })
    return results


def conversation_message_sizes(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    sql = """
        SELECT c.id as conv_id, c.source, c.project, date(c.started_at) as day,
               m.role, length(m.content) as chars
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.id
        WHERE c.started_at >= date('now', ?)
        ORDER BY c.id, m.timestamp
    """
    rows = conn.execute(sql, [f"-{days} days"]).fetchall()
    return [dict(r) for r in rows]


def conversation_texts(conn: sqlite3.Connection, days: int = 30, project: str | None = None) -> list[dict]:
    sql = """
        SELECT c.id, c.project, group_concat(m.content, ' ') as text
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.id
        WHERE c.started_at >= date('now', ?)
    """
    params: list = [f"-{days} days"]
    if project:
        sql += " AND c.project = ?"
        params.append(project)
    sql += " GROUP BY c.id"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def user_messages(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    sql = """
        SELECT m.content, c.project, m.timestamp
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.role = 'user' AND m.timestamp >= date('now', ?)
          AND length(m.content) > 20
          AND m.content NOT LIKE '%<task-notification>%'
          AND m.content NOT LIKE '%<turn_aborted>%'
          AND m.content NOT LIKE '%<local-command%'
          AND m.content NOT LIKE '%<command-name>%'
          AND m.content NOT LIKE '%<environment_context>%'
          AND m.content NOT LIKE '%AGENTS.md instructions%'
          AND m.content NOT LIKE '%Request interrupted by user%'
          AND m.content NOT LIKE '%session is being continued%'
          AND m.content NOT LIKE '%<image %'
          AND m.content NOT LIKE '%Set model to%'
        ORDER BY m.timestamp
    """
    rows = conn.execute(sql, [f"-{days} days"]).fetchall()
    return [dict(r) for r in rows]


def all_message_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM messages").fetchall()]


def get_message_texts(conn: sqlite3.Connection, ids: list[int]) -> list[tuple[int, str]]:
    if not ids:
        return []
    # Process in batches to avoid SQLite variable limit
    results = []
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT id, content FROM messages WHERE id IN ({placeholders})", batch
        ).fetchall()
        results.extend((r[0], r[1]) for r in rows)
    return results


def get_messages_by_ids(
    conn: sqlite3.Connection,
    ids: list[int],
    project: str | None = None,
    source: str | None = None,
) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    sql = f"""
        SELECT m.id, m.content, m.role, m.timestamp,
               c.source, c.project, c.git_branch
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.id IN ({placeholders})
    """
    params: list = list(ids)
    if project:
        sql += " AND c.project = ?"
        params.append(project)
    if source:
        sql += " AND c.source = ?"
        params.append(source)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
