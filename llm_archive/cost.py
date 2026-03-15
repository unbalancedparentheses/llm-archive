"""Parse actual token usage from raw JSONL files."""
import json
from collections import defaultdict
from pathlib import Path

from llm_archive.parsers import normalize_project
from llm_archive.parsers.claude import CLAUDE_BASE, _extract_project

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# Pricing per million tokens
# Claude: blended across Sonnet/Opus, including cache writes/reads
CLAUDE_PRICING = {
    "input": 3.0,
    "cache_creation": 3.75,
    "cache_read": 0.30,
    "output": 15.0,
}
# OpenAI: blended GPT-4.1/o3/o4-mini
CODEX_PRICING = {
    "input": 2.50,
    "cached_input": 0.625,
    "output": 10.0,
    "reasoning_output": 10.0,
}


def _parse_claude_usage() -> list[dict]:
    """Extract token usage from Claude Code JSONL files."""
    results = []
    if not CLAUDE_BASE.is_dir():
        return results

    for project_dir in CLAUDE_BASE.iterdir():
        if not project_dir.is_dir():
            continue
        project = normalize_project(_extract_project(project_dir.name))

        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if obj.get("type") != "assistant":
                            continue

                        msg = obj.get("message", {})
                        usage = msg.get("usage")
                        if not usage:
                            continue

                        results.append({
                            "source": "claude",
                            "project": project,
                            "timestamp": obj.get("timestamp", ""),
                            "input_tokens": usage.get("input_tokens", 0),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                        })
            except OSError:
                continue

    return results


def _parse_codex_usage() -> list[dict]:
    """Extract token usage from Codex CLI JSONL files."""
    results = []
    if not CODEX_SESSIONS.is_dir():
        return results

    for jsonl_file in CODEX_SESSIONS.rglob("*.jsonl"):
        project = "unknown"
        try:
            with open(jsonl_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") == "session_meta":
                        cwd = obj.get("payload", {}).get("cwd", "")
                        if cwd:
                            import os
                            project = normalize_project(os.path.basename(cwd))
                        continue

                    if obj.get("type") != "event_msg":
                        continue

                    payload = obj.get("payload", {})
                    if payload.get("type") != "token_count":
                        continue

                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    if not last:
                        continue

                    results.append({
                        "source": "codex",
                        "project": project,
                        "timestamp": obj.get("timestamp", ""),
                        "input_tokens": last.get("input_tokens", 0),
                        "cached_input_tokens": last.get("cached_input_tokens", 0),
                        "output_tokens": last.get("output_tokens", 0),
                        "reasoning_output_tokens": last.get("reasoning_output_tokens", 0),
                    })
        except OSError:
            continue

    return results


def compute_costs(days: int = 30) -> dict:
    """Compute actual costs from raw JSONL token usage."""
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    claude_usage = _parse_claude_usage()
    codex_usage = _parse_codex_usage()

    by_source = defaultdict(lambda: {"cost": 0.0, "input_tokens": 0, "output_tokens": 0})
    by_project = defaultdict(lambda: {"cost": 0.0, "input_tokens": 0, "output_tokens": 0})

    for u in claude_usage:
        if u["timestamp"] < cutoff:
            continue

        input_cost = u["input_tokens"] * CLAUDE_PRICING["input"] / 1_000_000
        cache_create_cost = u["cache_creation_input_tokens"] * CLAUDE_PRICING["cache_creation"] / 1_000_000
        cache_read_cost = u["cache_read_input_tokens"] * CLAUDE_PRICING["cache_read"] / 1_000_000
        output_cost = u["output_tokens"] * CLAUDE_PRICING["output"] / 1_000_000
        total = input_cost + cache_create_cost + cache_read_cost + output_cost

        total_input = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]

        by_source["claude"]["cost"] += total
        by_source["claude"]["input_tokens"] += total_input
        by_source["claude"]["output_tokens"] += u["output_tokens"]

        by_project[u["project"]]["cost"] += total
        by_project[u["project"]]["input_tokens"] += total_input
        by_project[u["project"]]["output_tokens"] += u["output_tokens"]

    for u in codex_usage:
        if u["timestamp"] < cutoff:
            continue

        uncached_input = u["input_tokens"] - u.get("cached_input_tokens", 0)
        cached_input = u.get("cached_input_tokens", 0)
        input_cost = uncached_input * CODEX_PRICING["input"] / 1_000_000
        cached_cost = cached_input * CODEX_PRICING["cached_input"] / 1_000_000
        output_cost = u["output_tokens"] * CODEX_PRICING["output"] / 1_000_000
        reasoning_cost = u.get("reasoning_output_tokens", 0) * CODEX_PRICING["reasoning_output"] / 1_000_000
        total = input_cost + cached_cost + output_cost + reasoning_cost

        by_source["codex"]["cost"] += total
        by_source["codex"]["input_tokens"] += u["input_tokens"]
        by_source["codex"]["output_tokens"] += u["output_tokens"]

        by_project[u["project"]]["cost"] += total
        by_project[u["project"]]["input_tokens"] += u["input_tokens"]
        by_project[u["project"]]["output_tokens"] += u["output_tokens"]

    return {
        "by_source": dict(by_source),
        "by_project": dict(by_project),
    }
