"""Small project summaries from recorded messages, without model calls."""
import re
from collections import OrderedDict
from datetime import datetime, timezone


def display_time(value: str | None, now: datetime | None = None) -> str:
    if not value:
        return "never"
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = int((now - stamp).total_seconds())
    age = abs(seconds)
    unit = f"{age // 86400}d" if age >= 86400 else f"{age // 3600}h" if age >= 3600 else f"{age // 60}m"
    relative = "just now" if age < 60 else f"in {unit}" if seconds < 0 else f"{unit} ago"
    return f"{stamp.astimezone().strftime('%Y-%m-%d %H:%M %Z')} ({relative})"


def useful_text(text: str) -> str:
    for tag in ("environment_context", "user_instructions", "task-notification", "turn_aborted"):
        text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text, flags=re.S)
    text = text.strip()
    if text.startswith(("# AGENTS.md", "This session is being continued", "<local-command", "<command-name>",
                        "[Earlier usage-limit notes", "<task-notification", "<turn_aborted")):
        return ""
    return text


def excerpt(text: str, length=180) -> str:
    text = re.sub(r"\s+", " ", text).strip(" #*-")
    return text if len(text) <= length else text[:length - 1].rsplit(" ", 1)[0] + "…"


def session_summary(conn, session: dict) -> dict:
    rows = conn.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 60", (session["id"],)).fetchall()
    users = [useful_text(r["content"]) for r in rows if r["role"] == "user"]
    assistants = [text for r in rows if r["role"] == "assistant" if (text := useful_text(r["content"]))]
    generic = re.compile(r"(?:please )?(?:fix (?:anything|everything).*|continue.*|keep going.*|commit(?: and push)?.*|anything (?:else|missing|to improve).*|ok(?:ay)?(?: yes)?[.!]?)", re.I)
    specific = [text for text in users if text and not generic.fullmatch(text.strip())]
    users = specific or users
    task = next((t for t in users if len(t) >= 20), next((t for t in users if t), "No request recorded"))
    update = next((t for t in assistants if t), "No assistant update recorded")
    next_step = "Not recorded; inspect the handoff"
    if assistants:
        for line in re.split(r"\n+|(?<=[.!?])\s+", assistants[0]):
            if len(line.strip()) >= 15 and re.search(r"\b(next(?: step)?|remaining|pending|blocked|still fails?|need to|siguiente|pendiente)\b", line, re.I):
                next_step = line
                break
    return {"task": excerpt(task), "last_update": excerpt(update), "next_step": excerpt(next_step),
            "evidence_session": session["session_id"]}


def project_overview(conn, sessions: list[dict]) -> list[dict]:
    groups = OrderedDict()
    for s in sessions:
        key = s.get("project_key") or s["project"]
        groups.setdefault(key, []).append(s)
    results = []
    for key, rows in groups.items():
        latest = rows[0]
        results.append({"project": latest["project"], "project_key": key,
                        "last_activity": latest.get("last_activity") or latest["started_at"],
                        "session_count": len(rows), "sources": sorted({r["source"] for r in rows}),
                        "directories": sorted({r["cwd"] for r in rows if r.get("cwd")}),
                        "machines": sorted({r["machine"] for r in rows if r.get("machine")}),
                        **session_summary(conn, latest)})
    return results
