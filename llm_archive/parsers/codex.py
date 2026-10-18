import json
import os
from collections.abc import Iterator
from pathlib import Path

from llm_archive.parsers import ParsedConversation, ParsedMessage, normalize_project
from llm_archive.strip import strip_code_blocks
from llm_archive.parsers.events import tool_event, result_event
from llm_archive.parsers.files import open_transcript, transcript_files

CODEX_SESSIONS = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "sessions"


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

    for jsonl_file in transcript_files(CODEX_SESSIONS):
        conv = _parse_session(jsonl_file)
        if conv and conv.messages:
            yield conv


def _parse_session(path: Path) -> ParsedConversation | None:
    messages = []
    events = []
    git_branch = None
    is_subagent = False
    session_id = path.name.removesuffix(".zst").removesuffix(".jsonl")
    cwd = None
    started_at = None

    try:
        with open_transcript(path) as f:
            for index, line in enumerate(f):
                if not line.endswith("\n"):
                    continue  # The writer may still be appending this record.
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                msg_type = obj.get("type", "")

                if msg_type == "session_meta":
                    payload = obj.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd")
                    started_at = payload.get("timestamp")
                    git_branch = (payload.get("git") or {}).get("branch")
                    source = payload.get("source", {})
                    is_subagent = isinstance(source, dict) and "subagent" in source
                    continue

                if msg_type != "response_item":
                    continue

                payload = obj.get("payload", {})
                timestamp = obj.get("timestamp", "")
                kind = payload.get("type")
                if kind in ("function_call", "custom_tool_call"):
                    events.append(tool_event(payload.get("call_id", str(index)), payload.get("name", "tool"),
                                             payload.get("arguments", payload.get("input", "")), timestamp))
                    continue
                if kind in ("function_call_output", "custom_tool_call_output"):
                    events.append(result_event(payload.get("call_id", str(index)), payload.get("output", ""), timestamp))
                    continue
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
        git_branch=git_branch,
        started_at=started_at,
        messages=messages,
        cwd=cwd or "",
        source_path=str(path.resolve()),
        is_subagent=is_subagent,
        events=events,
    )
