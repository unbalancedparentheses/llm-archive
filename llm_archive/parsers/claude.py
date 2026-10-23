import json
import os
from collections.abc import Iterator
from pathlib import Path

from llm_archive.parsers import ParsedConversation, ParsedMessage, normalize_project
from llm_archive.strip import strip_code_blocks
from llm_archive.parsers.events import tool_event, result_event

CLAUDE_BASE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "projects"


def _extract_project(folder_name: str) -> str:
    # Folder names look like: -Users-unbalancedparen-projects-holdco
    # We want the last meaningful segment after stripping the home path prefix.
    parts = folder_name.strip("-").split("-")

    # Find the last known prefix segment and take everything after it
    for prefix in ("projects", "Desktop"):
        if prefix in parts:
            idx = len(parts) - 1 - parts[::-1].index(prefix)
            remainder = "-".join(parts[idx + 1:])
            if remainder:
                return remainder

    # Fallback: take the last segment, or mark as home directory
    last = parts[-1] if parts else folder_name
    if last in ("unbalancedparen", ""):
        return "(home)"
    return last


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return ""


def parse_claude_sessions() -> Iterator[ParsedConversation]:
    if not CLAUDE_BASE.is_dir():
        return

    for project_dir in CLAUDE_BASE.iterdir():
        if not project_dir.is_dir():
            continue

        project = normalize_project(_extract_project(project_dir.name))

        for jsonl_file in project_dir.glob("*.jsonl"):
            conv = _parse_session(jsonl_file, project)
            if conv and conv.messages:
                yield conv


def _parse_session(path: Path, project: str) -> ParsedConversation | None:
    messages = []
    events = []
    cwd = ""
    is_subagent = "subagents" in path.parts
    session_id = path.stem
    git_branch = None
    started_at = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for index, line in enumerate(f):
                if not line.endswith("\n"):
                    continue
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
                if msg_type not in ("user", "assistant"):
                    continue
                cwd = obj.get("cwd") or cwd
                is_subagent = is_subagent or bool(obj.get("isSidechain"))

                git_branch = obj.get("gitBranch") or git_branch
                if started_at is None:
                    started_at = obj.get("timestamp")

                if not is_subagent and obj.get("sessionId"):
                    session_id = obj["sessionId"]

                msg = obj.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block_index, block in enumerate(content):
                        if not isinstance(block, dict):
                            continue
                        eid = block.get("id", f"{index}:{block_index}")
                        if block.get("type") == "tool_use":
                            events.append(tool_event(eid, block.get("name", "tool"), block.get("input", {}), obj.get("timestamp", "")))
                        elif block.get("type") == "tool_result":
                            events.append(result_event(block.get("tool_use_id", eid), block.get("content", ""), obj.get("timestamp", "")))
                role = msg.get("role", msg_type)
                raw_text = _extract_text(msg.get("content", ""))
                text = strip_code_blocks(raw_text)

                if not text:
                    continue

                messages.append(ParsedMessage(
                    role=role,
                    content=text,
                    timestamp=obj.get("timestamp", ""),
                ))
    except OSError:
        return None

    return ParsedConversation(
        session_id=session_id,
        source="claude",
        project=project,
        git_branch=git_branch,
        started_at=started_at,
        messages=messages,
        cwd=cwd,
        source_path=str(path.resolve()),
        is_subagent=is_subagent,
        events=events,
    )
