import os
import subprocess
import json


SUMMARY_SYSTEM = """You are analyzing a developer's LLM conversation history.
Summarize what they worked on, what decisions were made, what they struggled with,
and any recurring patterns. Be specific about projects and topics. Keep it concise."""


IDEAS_SYSTEM = """You are analyzing a developer's LLM conversation history to surface their best thinking.
Your job is to find the valuable signal buried in day-to-day coding conversations:
ideas they had, problems they identified, arguments they made, architectural insights,
and things they wanted to build or explore but may not have followed through on.
Be specific. Quote them when possible. Group by theme."""


def _detect_provider() -> str:
    """Return 'anthropic', 'openai', or 'ollama' based on available keys/tools."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    # Check if ollama is running
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return "ollama"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "none"


def _call_anthropic(system: str, prompt: str, max_tokens: int) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai(system: str, prompt: str, max_tokens: int) -> str:
    import openai
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model="gpt-4.1",
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content


def _call_ollama(system: str, prompt: str, max_tokens: int) -> str:
    payload = json.dumps({
        "model": "llama3.2",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    })
    result = subprocess.run(
        ["curl", "-s", "http://localhost:11434/api/chat", "-d", payload],
        capture_output=True, text=True, timeout=300,
    )
    data = json.loads(result.stdout)
    return data["message"]["content"]


def _call_llm(system: str, prompt: str, max_tokens: int = 4000) -> tuple[str, str]:
    """Call the best available LLM. Returns (response_text, provider_name)."""
    provider = _detect_provider()

    if provider == "anthropic":
        return _call_anthropic(system, prompt, max_tokens), "claude"
    elif provider == "openai":
        return _call_openai(system, prompt, max_tokens), "openai"
    elif provider == "ollama":
        return _call_ollama(system, prompt, max_tokens), "ollama (local)"
    else:
        raise RuntimeError(
            "No LLM provider available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or install ollama."
        )


def estimate_cost(text: str, max_output_tokens: int) -> tuple[str, str]:
    """Return (cost_estimate_string, provider_name)."""
    provider = _detect_provider()
    chars = min(len(text), 100_000)
    input_tokens = chars // 4

    if provider == "anthropic":
        cost = (input_tokens * 3.0 + max_output_tokens * 15.0) / 1_000_000
        return f"~{input_tokens:,} tokens → Claude API (~${cost:.2f})", provider
    elif provider == "openai":
        cost = (input_tokens * 2.0 + max_output_tokens * 8.0) / 1_000_000
        return f"~{input_tokens:,} tokens → OpenAI API (~${cost:.2f})", provider
    elif provider == "ollama":
        return f"~{input_tokens:,} tokens → ollama local (free)", provider
    else:
        return "No LLM provider available", "none"


def weekly_summary(text: str, days: int = 7) -> str:
    if len(text) > 100_000:
        text = text[:100_000] + "\n\n[truncated]"

    prompt = f"""Here are the last {days} days of my conversations with LLM coding assistants.
Summarize:
1. What I worked on (by project)
2. Key decisions made
3. What I struggled with or asked repeatedly
4. Patterns you notice

Conversations:
{text}"""

    result, _ = _call_llm(SUMMARY_SYSTEM, prompt, max_tokens=2000)
    return result


def extract_ideas(text: str, days: int = 30) -> str:
    if len(text) > 100_000:
        text = text[:100_000] + "\n\n[truncated]"

    prompt = f"""Here are the last {days} days of my conversations with LLM coding assistants.

Extract and organize:

1. **Ideas** — things I proposed building, features I brainstormed, approaches I suggested. Include half-formed ideas that might be worth revisiting.

2. **Problems worth solving** — pain points I complained about, inefficiencies I noticed, gaps I identified. Things that could become projects or features.

3. **Arguments and opinions** — strong positions I took on architecture, tooling, process, or technology. Include the reasoning when present.

4. **Unexplored threads** — things I mentioned wanting to look into, research, or try but the conversation moved on. These are easy to forget.

5. **Recurring themes** — ideas or problems that keep coming up across different projects. These are likely the most important ones.

For each item, mention which project it came from and quote my words when possible.
Prioritize items that seem genuinely valuable or original over routine coding decisions.

Conversations:
{text}"""

    result, _ = _call_llm(IDEAS_SYSTEM, prompt, max_tokens=4000)
    return result
