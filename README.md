# llm-archive

A local tool to ingest, search, and analyze conversations from LLM coding assistants — stripping code to keep only the natural language exchange.

## Supported sources

- **Claude Code** — parses `~/.claude/projects/**/*.jsonl`
- **Codex CLI** — parses `~/.codex/sessions/**/*.jsonl` + `~/.codex/history.jsonl`

Adding new sources (e.g., ChatGPT export JSON) is just another parser module.

## Install

```bash
pip install -e .
llm-archive ingest
```

## CLI usage

```bash
# Ingest new conversations (incremental, safe to re-run)
llm-archive ingest

# Full-text search (keyword matching)
llm-archive search "trusted boundary report"
llm-archive search "how did we handle auth" --project holdco --source claude

# Semantic search (search by meaning, not keywords)
llm-archive embed                    # build index (one-time, ~5 min)
llm-archive search --semantic "retry strategy for failed requests"

# Drill into a specific day
llm-archive day 2026-03-12

# Daily project breakdown
llm-archive timeline --days 30

# Active usage hours per day
llm-archive hours --days 30

# Hours per project (where your time goes)
llm-archive projects --days 30

# Claude vs Codex usage split
llm-archive projects --by-source

# Weekly summary (saves to ~/.local/share/llm-archive/summaries/)
llm-archive summarize --days 7

# Mine your conversations for ideas, problems, and unexplored threads
llm-archive ideas --days 30

# Extract topics from conversations (TF-IDF)
llm-archive topics --days 30

# API cost from actual token usage in JSONL files
llm-archive cost --days 30

# Export conversations to markdown or JSON
llm-archive export --days 7 --format md
llm-archive export --project holdco --format json --output ./backup

# Find questions you keep asking
llm-archive recurring --days 30

# Overview counts
llm-archive stats
```

## Architecture

```
~/.claude/projects/**/*.jsonl  ──┐
                                 ├──▶  [ingester]  ──▶  SQLite (FTS5)
~/.codex/sessions/**/*.jsonl   ──┘         │
                                           ▼
                                    strip code blocks
                                    strip tool calls
                                    normalize to common schema
                                           │
                                           ▼
                                   conversations(id, source, project, branch, started_at)
                                   messages(id, conv_id, role, content, timestamp)
                                           │
                                           ▼
                              [search] [search --semantic] [timeline] [hours]
                              [projects] [cost] [ideas] [summarize]
                              [recurring] [topics] [export] [embed]
```

## Project structure

```
llm-archive/
├── llm_archive/
│   ├── __init__.py
│   ├── cli.py           # click CLI
│   ├── db.py            # SQLite + FTS5
│   ├── ingest.py        # wires parsers to db
│   ├── strip.py         # remove code blocks
│   ├── recurring.py     # trigram similarity clustering
│   ├── summarize.py     # multi-provider LLM calls
│   ├── topics.py        # TF-IDF keyword extraction
│   ├── cost.py          # actual token usage from JSONL
│   ├── embeddings.py    # semantic search (ollama/sentence-transformers)
│   └── parsers/
│       ├── __init__.py  # shared types + normalize_project
│       ├── claude.py    # Claude Code JSONL parser
│       └── codex.py     # Codex CLI JSONL parser
└── pyproject.toml
```

## LLM providers

The `summarize` and `ideas` commands need an LLM. Providers are tried in order — the first available one is used:

| Priority | Provider | Env var | Model |
|----------|----------|---------|-------|
| 1 | Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4 |
| 2 | OpenAI | `OPENAI_API_KEY` | gpt-4.1 |
| 3 | Kimi (Moonshot) | `MOONSHOT_API_KEY` | moonshot-v1-128k |
| 4 | DeepSeek | `DEEPSEEK_API_KEY` | deepseek-chat |
| 5 | Groq | `GROQ_API_KEY` | llama-3.3-70b |
| 6 | Together | `TOGETHER_API_KEY` | Llama-3.3-70B |
| 7 | Ollama (local) | — | auto-selects best installed model |

If no API key is set, it falls back to **ollama** (free, runs locally). The command shows estimated cost before running and asks for confirmation. Ollama shows as "free".

```bash
# With API key
export ANTHROPIC_API_KEY=sk-ant-...
llm-archive ideas --days 30
# ~25,000 tokens → Claude API (~$0.14). Continue? [y/N]

# Without any API key (uses ollama)
llm-archive ideas --days 30
# ~25,000 tokens → ollama/qwen2.5:32b (free). Continue? [y/N]
```

## Stripping rules

- Remove fenced code blocks
- Remove tool_use / tool_result messages
- Remove thinking blocks
- Remove system prompts and developer messages
- Keep inline code references
- Keep user questions, assistant explanations, commentary

## Changelog

### v0.4.0

- `embed` — build semantic embedding index using ollama (nomic-embed-text) or sentence-transformers fallback
- `search --semantic` — search by meaning instead of keywords, cosine similarity over embeddings
- Incremental embedding — only processes new messages on subsequent runs
- Embeddings stored as numpy `.npz` file (~150MB for 50k messages)

### v0.3.0

- `ideas` — mine conversations for ideas, problems, arguments, and unexplored threads
- `topics` — TF-IDF keyword extraction across conversations, shows dominant themes per project
- `cost` — actual API cost from token usage in raw JSONL files (cache-aware pricing for Claude and Codex)
- `export` — dump conversations to markdown or JSON files with `--project`, `--source`, `--format` filters
- Multi-provider LLM support: Anthropic → OpenAI → Kimi → DeepSeek → Groq → Together → ollama (local, free)
- Cost confirmation before any API call; ollama auto-selects best installed model

### v0.2.0

- `projects` — active hours per project with visual bars and percentages
- `projects --by-source` — Claude vs Codex usage split per project
- `summarize` now saves digests to `~/.local/share/llm-archive/summaries/YYYY-MM-DD.md`
- `install-cron` / `uninstall-cron` — automatic daily ingestion (launchd/systemd/crontab)
- Startup health check showing auto-ingest status and DB freshness

### v0.1.0

- Parsers for Claude Code and Codex CLI JSONL formats
- Incremental ingestion into SQLite with FTS5 full-text search
- Project name normalization across sources (dots, underscores, hyphens unified)
- `ingest` — parse and store new conversations
- `search` — full-text search with `--project` and `--source` filters
- `stats` — overview counts by source and project
- `timeline` — daily project breakdown with conversation/message counts
- `hours` — active usage hours per day (30-min gap threshold, visual bars)
- `day` — drill into a specific date, shows each conversation with timestamp and first user message
- `summarize` — weekly/monthly digest via Claude API
- `recurring` — finds repeated questions using trigram similarity, filters system noise

## Roadmap

- [x] v0.1 — Ingestion + FTS5 search + timeline + hours + day drill-down + recurring detection + Claude API summaries
- [x] v0.2 — Auto-ingest, summary to markdown, per-project hours, Claude vs Codex split
- [x] v0.3 — Topic extraction, conversation export, actual token cost tracking, ideas mining
- [x] v0.4 — Semantic search with ollama embeddings (sentence-transformers fallback)
