import anthropic


SYSTEM = """You are analyzing a developer's LLM conversation history.
Summarize what they worked on, what decisions were made, what they struggled with,
and any recurring patterns. Be specific about projects and topics. Keep it concise."""


def weekly_summary(text: str, days: int = 7) -> str:
    client = anthropic.Anthropic()

    # Truncate to fit context — ~100k chars is safe
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

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text
