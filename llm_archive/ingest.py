from llm_archive.db import get_connection, insert_conversation, session_exists
from llm_archive.parsers.claude import parse_claude_sessions
from llm_archive.parsers.codex import parse_codex_sessions


def run_ingest() -> tuple[int, int]:
    conn = get_connection()
    new_convs = 0
    new_msgs = 0

    for conv in parse_claude_sessions():
        if session_exists(conn, conv.session_id):
            continue
        insert_conversation(conn, conv)
        new_convs += 1
        new_msgs += len(conv.messages)
        if new_convs % 100 == 0:
            conn.commit()

    for conv in parse_codex_sessions():
        if session_exists(conn, conv.session_id):
            continue
        insert_conversation(conn, conv)
        new_convs += 1
        new_msgs += len(conv.messages)
        if new_convs % 100 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    return new_convs, new_msgs
