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
