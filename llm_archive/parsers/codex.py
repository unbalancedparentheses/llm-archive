import json
import os
from collections.abc import Iterator
from pathlib import Path

from llm_archive.parsers import ParsedConversation, ParsedMessage, normalize_project
from llm_archive.strip import strip_code_blocks

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


def _extract_project(cwd: str) -> str:
    return normalize_project(os.path.basename(cwd)) if cwd else "unknown"


def _extract_text(content, text_type: str) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == text_type:
            parts.append(item.get("text", ""))
    return "\n".join(parts)


def parse_codex_sessions() -> Iterator[ParsedConversation]:
    if not CODEX_SESSIONS.is_dir():
        return

    for jsonl_file in CODEX_SESSIONS.rglob("*.jsonl"):
        conv = _parse_session(jsonl_file)
        if conv and conv.messages:
            yield conv


def _parse_session(path: Path) -> ParsedConversation | None:
    messages = []
    session_id = path.stem
    cwd = None
    started_at = None

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")

                if msg_type == "session_meta":
                    payload = obj.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd")
                    started_at = payload.get("timestamp")
                    continue

                if msg_type != "response_item":
                    continue

                payload = obj.get("payload", {})
                role = payload.get("role", "")

                if role == "developer":
                    continue
                if role not in ("user", "assistant"):
                    continue

                if role == "user":
                    raw_text = _extract_text(payload.get("content", []), "input_text")
                else:
                    raw_text = _extract_text(payload.get("content", []), "output_text")

                text = strip_code_blocks(raw_text)
                if not text:
                    continue

                timestamp = obj.get("timestamp", "")

                messages.append(ParsedMessage(
                    role=role,
                    content=text,
                    timestamp=timestamp,
                ))
    except OSError:
        return None

    return ParsedConversation(
        session_id=session_id,
        source="codex",
        project=_extract_project(cwd or ""),
        git_branch=None,
        started_at=started_at,
        messages=messages,
    )
