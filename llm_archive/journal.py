"""Evidence-based handoffs, usable without an LLM or network connection."""
import hashlib
import re
import shlex
from collections import defaultdict
from pathlib import Path

from llm_archive import db
from llm_archive.paths import atomic_write, data_dir, device_id
from llm_archive.projects import git


def recent_sessions(conn, days=7, project=None, limit=100) -> list[dict]:
    sql = """SELECT c.*, (SELECT count(*) FROM messages WHERE conversation_id = c.id) AS message_count
             FROM conversations c WHERE coalesce(c.is_subagent, 0) = 0
             AND julianday(coalesce(nullif(c.last_activity, ''), c.started_at)) >= julianday('now', ?)"""
    args = [f"-{days} days"]
    if project:
        sql += " AND (c.project = ? OR c.project_key = ?)"
        args.extend([project, project])
    sql += " ORDER BY julianday(coalesce(nullif(c.last_activity, ''), c.started_at)) DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, [*args, limit])]


def resume_command(session: dict) -> str:
    sid = session["session_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid):
        raise ValueError("Session ID contains unsupported characters")
    if session["source"] == "codex":
        return shlex.join(["codex", "resume", sid])
    if session["source"] == "claude":
        return shlex.join(["claude", "--resume", sid])
    raise ValueError("Unsupported session source")


def local_session(session: dict) -> bool:
    return (session.get("origin_device") == device_id() and bool(session.get("source_path"))
            and Path(session["source_path"]).is_file())


def local_workspace(session: dict) -> bool:
    return session.get("origin_device") == device_id()


def handoff(conn, sessions: list[dict]) -> str:
    if not sessions:
        return "No matching activity found.\n"
    lines = [f"# {sessions[0]['project']} — work handoff", "",
             "Captured conversation excerpts and tool evidence. Completion and next steps must be verified.", ""]
    for s in sessions:
        lines += [f"## {s['source']} · {s.get('git_branch') or 'branch not recorded'}", "",
                  f"- Session: `{s['session_id']}`", f"- Last activity: {s.get('last_activity') or s['started_at']}",
                  f"- Computer: {s.get('machine') or 'unknown'}", f"- Directory: `{s.get('cwd') or 'unknown'}`",
                  f"- Repository: `{s.get('repo_url') or s.get('project_key') or 'unknown'}`",
                  f"- Transcript: `{s.get('source_path') or 'unknown'}`", ""]
        rows = conn.execute("SELECT role, content, timestamp FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 8", (s["id"],)).fetchall()
        lines += ["### Latest conversation", ""]
        for r in reversed(rows):
            lines += [f"**{r['role']}** ({r['timestamp']}):", "", r["content"][-4000:], ""]
        events = conn.execute("SELECT kind, content, timestamp FROM events WHERE conversation_id = ? ORDER BY rowid DESC LIMIT 8", (s["id"],)).fetchall()
        if events:
            lines += ["### Recent tool evidence", "", "Outputs are excerpts; a successful command does not establish that the task is finished.", ""]
            for e in reversed(events):
                lines += [f"**{e['kind']}** ({e['timestamp']}):", "", "````text", e["content"], "````", ""]
        if local_workspace(s) and s.get("cwd") and Path(s["cwd"]).is_dir():
            branch = git(s["cwd"], "branch", "--show-current")
            head = git(s["cwd"], "rev-parse", "--short", "HEAD")
            status = git(s["cwd"], "status", "--short")
            lines += ["### Local working tree", "", f"Observed at {db.utc_now()}; current branch `{branch}`, HEAD `{head}`.",
                      "", "````text", status or ("Working tree clean." if head else "Git state unavailable."), "````", ""]
        if local_session(s) and s.get("cwd") and Path(s["cwd"]).is_dir():
            lines += ["Resume this conversation:", "", "```sh", f"cd -- {shlex.quote(s['cwd'])}", resume_command(s), "```", ""]
        else:
            lines += ["The original session is not available locally. Use these notes as context in a new conversation.", ""]
    lines += ["## Continue from here", "", "Review the latest request and assistant update above, verify the branch and working tree, and confirm any remaining work against the tool evidence.",
              "These notes do not restore source files or uncommitted changes.", ""]
    return "\n".join(lines)


def write_handoffs(conn, days=7) -> int:
    groups = defaultdict(list)
    for s in recent_sessions(conn, days=days, limit=10000):
        groups[s.get("project_key") or s["project"]].append(s)
    for key, sessions in groups.items():
        name = re.sub(r"[^a-zA-Z0-9_-]", "-", sessions[0]["project"])
        digest = hashlib.sha256(key.encode()).hexdigest()[:10]
        atomic_write(data_dir() / "journal" / f"{name}-{digest}.md", handoff(conn, sessions[:5]))
    return len(groups)


def checkpoint(sync=True) -> tuple[int, int, int]:
    from llm_archive.ingest import run_ingest
    updated, messages = run_ingest()
    conn = db.get_connection()
    try:
        projects = write_handoffs(conn)
    finally:
        conn.close()
    if sync:
        from llm_archive.sync import sync_if_configured
        sync_if_configured()
    return updated, messages, projects
