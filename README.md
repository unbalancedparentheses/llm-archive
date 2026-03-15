# llm-archive

Your LLM conversations are full of decisions, ideas, and context that disappear after each session. This tool saves them.

It ingests conversations from Claude Code and Codex CLI, strips the code to keep only the natural language, and gives you search, analytics, and idea mining over everything you've discussed with AI.

## Quick start

```bash
pip install -e .
llm-archive ingest          # parse conversations into SQLite
llm-archive embed           # build semantic search index (optional, needs ollama)
llm-archive install-cron    # auto-ingest daily
```

## What it does

**Search** — find past conversations by keyword or meaning:
```bash
llm-archive search "how did we handle auth" --project holdco
llm-archive search --semantic "retry strategy for failed requests"
```

**Track your time** — see where your hours go:
```bash
llm-archive hours --days 30          # hours per day with visual bars
llm-archive projects --days 30       # hours per project
llm-archive projects --by-source     # Claude vs Codex split
llm-archive timeline --days 14       # daily project breakdown
```

**Mine your thinking** — surface ideas you forgot about:
```bash
llm-archive ideas --days 30          # ideas, problems, arguments, unexplored threads
llm-archive summarize --days 7       # weekly digest (saved to markdown)
llm-archive topics --days 30         # dominant themes via TF-IDF
llm-archive recurring --days 30      # questions you keep asking
```

**Know your costs** — actual token usage from raw JSONL files:
```bash
llm-archive cost --days 30           # breakdown by source and project
```

**Export and explore**:
```bash
llm-archive day 2026-03-12           # drill into a specific day
llm-archive export --format md       # dump conversations to markdown
llm-archive stats                    # overview counts
```

## Supported sources

| Source | Path | Format |
|--------|------|--------|
| Claude Code | `~/.claude/projects/**/*.jsonl` | Messages with token usage |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` | Sessions with token counts |

Adding a new source (e.g., ChatGPT export) is just another parser module in `llm_archive/parsers/`.

## How it works

```
~/.claude/projects/**/*.jsonl  ──┐
                                 ├──▶  strip code blocks    ──▶  SQLite (FTS5)
~/.codex/sessions/**/*.jsonl   ──┘     strip tool calls          │
                                       strip thinking blocks     ├──▶  keyword search
                                       keep natural language     ├──▶  semantic search (embeddings)
                                                                 ├──▶  analytics (hours, projects, cost)
                                                                 └──▶  LLM analysis (ideas, summaries)
```

**What gets stripped**: fenced code blocks, tool_use/tool_result messages, thinking blocks, system prompts, developer messages.

**What gets kept**: user questions, assistant explanations, commentary, inline code references.

## LLM providers

The `summarize` and `ideas` commands call an LLM to analyze your conversations. The first available provider is used:

| Provider | Env var | Model | Cost |
|----------|---------|-------|------|
| Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4 | ~$0.14/run |
| OpenAI | `OPENAI_API_KEY` | gpt-4.1 | ~$0.10/run |
| Kimi | `MOONSHOT_API_KEY` | moonshot-v1-128k | ~$0.03/run |
| DeepSeek | `DEEPSEEK_API_KEY` | deepseek-chat | ~$0.01/run |
| Groq | `GROQ_API_KEY` | llama-3.3-70b | ~$0.02/run |
| Together | `TOGETHER_API_KEY` | Llama-3.3-70B | ~$0.02/run |
| Ollama | — | auto-selects best local model | free |

Every command shows estimated cost and asks for confirmation before calling the API. If no API key is set, it falls back to ollama.

## Semantic search

Keyword search finds exact words. Semantic search finds meaning — "error handling" matches conversations about exception catching, failure recovery, retry logic.

```bash
# First time: build the embedding index (~5 min)
llm-archive embed

# Then search by meaning
llm-archive search --semantic "deploying to production"
```

Uses ollama with `nomic-embed-text` (free, local). Falls back to `sentence-transformers` if ollama isn't available. Incremental — re-running `embed` only processes new messages.

## Auto-ingest

Run `llm-archive install-cron` to set up daily automatic ingestion:

- **macOS**: launchd agent (runs at 06:00 + on login)
- **Linux**: systemd user timer, or crontab fallback

The CLI shows ingestion status on every command:
```
auto-ingest: active (launchd)
db: updated 2026-03-15 02:55
```

## Project structure

```
llm_archive/
├── cli.py           # click CLI (all commands)
├── db.py            # SQLite + FTS5 schema and queries
├── ingest.py        # wires parsers to database
├── strip.py         # remove code blocks from text
├── embeddings.py    # semantic search (ollama / sentence-transformers)
├── summarize.py     # multi-provider LLM calls (ideas, summaries)
├── topics.py        # TF-IDF keyword extraction
├── cost.py          # actual token usage from raw JSONL
├── recurring.py     # trigram similarity clustering
├── scheduler.py     # launchd / systemd / crontab setup
└── parsers/
    ├── __init__.py  # shared types + project name normalization
    ├── claude.py    # Claude Code JSONL parser
    └── codex.py     # Codex CLI JSONL parser
```

## Data storage

Everything lives under `~/.local/share/llm-archive/`:

| File | Purpose |
|------|---------|
| `archive.db` | SQLite database with FTS5 index |
| `embeddings.npz` | Semantic search vectors (~150MB for 50k messages) |
| `embeddings_meta.json` | Embedding provider and dimension info |
| `summaries/YYYY-MM-DD.md` | Generated weekly digests |
| `ideas/YYYY-MM-DD.md` | Mined ideas and insights |
| `ingest.log` | Auto-ingest log |
