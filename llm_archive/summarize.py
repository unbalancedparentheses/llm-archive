import anthropic


SUMMARY_SYSTEM = """You are analyzing a developer's LLM conversation history.
Summarize what they worked on, what decisions were made, what they struggled with,
and any recurring patterns. Be specific about projects and topics. Keep it concise."""


IDEAS_SYSTEM = """You are analyzing a developer's LLM conversation history to surface their best thinking.
Your job is to find the valuable signal buried in day-to-day coding conversations:
ideas they had, problems they identified, arguments they made, architectural insights,
and things they wanted to build or explore but may not have followed through on.
Be specific. Quote them when possible. Group by theme."""


def _call_claude(system: str, prompt: str, max_tokens: int = 4000) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


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

    return _call_claude(SUMMARY_SYSTEM, prompt, max_tokens=2000)


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

    return _call_claude(IDEAS_SYSTEM, prompt, max_tokens=4000)
