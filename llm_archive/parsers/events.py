"""Compact tool evidence; separate from the natural-language search index."""
import json

from llm_archive.parsers import ParsedEvent


def tool_event(event_id, name, arguments, timestamp) -> ParsedEvent:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if isinstance(arguments, dict):
        # Record commands/paths, not full file contents or environment variables.
        selected = {k: arguments[k] for k in (
            "command", "cmd", "workdir", "cwd", "file_path", "path", "description",
        ) if k in arguments}
        arguments = json.dumps(selected, ensure_ascii=False)
    elif name == "apply_patch":
        arguments = "\n".join(line for line in str(arguments).splitlines()
                              if line.startswith(("*** Add File:", "*** Update File:", "*** Delete File:")))
    return ParsedEvent(str(event_id), "tool_call", f"{name}: {str(arguments)[:2000]}", timestamp)


def result_event(event_id, output, timestamp) -> ParsedEvent:
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return ParsedEvent(str(event_id) + ":result", "tool_result", output[-2000:], timestamp)
