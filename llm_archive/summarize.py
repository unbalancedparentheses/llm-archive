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


# Provider priority order. Each entry: (name, env_var, model, call_fn_name)
PROVIDERS = [
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("kimi", "MOONSHOT_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("together", "TOGETHER_API_KEY"),
    ("ollama", None),
]


def _detect_provider() -> str:
    """Return best available provider name."""
    for name, env_var in PROVIDERS:
        if env_var and os.environ.get(env_var):
            return name
        if name == "ollama":
            try:
                result = subprocess.run(
                    ["ollama", "list"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return "ollama"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    return "none"


def _openai_compatible_call(
    base_url: str, api_key: str, model: str,
    system: str, prompt: str, max_tokens: int,
) -> str:
    """Generic OpenAI-compatible API call."""
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload, headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


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
    return _openai_compatible_call(
        "https://api.openai.com/v1", os.environ["OPENAI_API_KEY"],
        "gpt-4.1", system, prompt, max_tokens,
    )


def _call_kimi(system: str, prompt: str, max_tokens: int) -> str:
    return _openai_compatible_call(
        "https://api.moonshot.cn/v1", os.environ["MOONSHOT_API_KEY"],
        "moonshot-v1-128k", system, prompt, max_tokens,
    )


def _call_deepseek(system: str, prompt: str, max_tokens: int) -> str:
    return _openai_compatible_call(
        "https://api.deepseek.com/v1", os.environ["DEEPSEEK_API_KEY"],
        "deepseek-chat", system, prompt, max_tokens,
    )


def _call_groq(system: str, prompt: str, max_tokens: int) -> str:
    return _openai_compatible_call(
        "https://api.groq.com/openai/v1", os.environ["GROQ_API_KEY"],
        "llama-3.3-70b-versatile", system, prompt, max_tokens,
    )


def _call_together(system: str, prompt: str, max_tokens: int) -> str:
    return _openai_compatible_call(
        "https://api.together.xyz/v1", os.environ["TOGETHER_API_KEY"],
        "meta-llama/Llama-3.3-70B-Instruct-Turbo", system, prompt, max_tokens,
    )


def _best_ollama_model() -> str:
    """Pick the best available ollama model."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return "llama3.2"
        models = result.stdout.lower()
        # Prefer larger models
        for preferred in ["qwen2.5:32b", "llama3.3", "qwen2.5:14b", "llama3.1", "llama3.2"]:
            if preferred in models:
                return preferred
        # Fall back to first model listed
        lines = result.stdout.strip().split("\n")
        if len(lines) > 1:
            return lines[1].split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "llama3.2"


def _call_ollama(system: str, prompt: str, max_tokens: int) -> str:
    import sys
    import urllib.request

    model = _best_ollama_model()
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "options": {"num_predict": max_tokens},
    }).encode()

    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    full_response = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            chunk = json.loads(line)
            token = chunk.get("message", {}).get("content", "")
            if token:
                sys.stderr.write(token)
                sys.stderr.flush()
                full_response.append(token)
    sys.stderr.write("\n")
    return "".join(full_response)


_CALLERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "kimi": _call_kimi,
    "deepseek": _call_deepseek,
    "groq": _call_groq,
    "together": _call_together,
    "ollama": _call_ollama,
}


def _call_llm(system: str, prompt: str, max_tokens: int = 4000) -> tuple[str, str]:
    """Call the best available LLM. Returns (response_text, provider_name)."""
    provider = _detect_provider()
    caller = _CALLERS.get(provider)
    if not caller:
        raise RuntimeError(
            "No LLM provider available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "MOONSHOT_API_KEY, DEEPSEEK_API_KEY, GROQ_API_KEY, TOGETHER_API_KEY, "
            "or install ollama."
        )
    label = "ollama local" if provider == "ollama" else provider
    return caller(system, prompt, max_tokens), label


# Approximate pricing per MTok (input, output)
_PRICING = {
    "anthropic": (3.0, 15.0),
    "openai": (2.0, 8.0),
    "kimi": (1.0, 2.0),
    "deepseek": (0.27, 1.10),
    "groq": (0.59, 0.79),
    "together": (0.88, 0.88),
}


def estimate_cost(text: str, max_output_tokens: int) -> tuple[str, str]:
    """Return (cost_estimate_string, provider_name)."""
    provider = _detect_provider()
    chars = min(len(text), 100_000)
    input_tokens = chars // 4

    if provider == "ollama":
        model = _best_ollama_model()
        return f"~{input_tokens:,} tokens → ollama/{model} (free)", provider
    elif provider in _PRICING:
        inp, out = _PRICING[provider]
        cost = (input_tokens * inp + max_output_tokens * out) / 1_000_000
        return f"~{input_tokens:,} tokens → {provider} (~${cost:.2f})", provider
    elif provider == "none":
        return "No LLM provider available", "none"
    else:
        return f"~{input_tokens:,} tokens → {provider}", provider


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
