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

# Full-text search
llm-archive search "trusted boundary report"
llm-archive search "how did we handle auth" --project holdco --source claude

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

# Weekly summary via Claude API (needs ANTHROPIC_API_KEY)
# Also saves to ~/.local/share/llm-archive/summaries/
llm-archive summarize --days 7

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
                              [search] [timeline] [hours] [projects] [cost]
                              [summarize] [recurring] [topics] [export]
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
│   ├── summarize.py     # Claude API digest
│   ├── topics.py        # TF-IDF keyword extraction
│   ├── cost.py          # actual token usage from JSONL
│   └── parsers/
│       ├── __init__.py  # shared types + normalize_project
│       ├── claude.py    # Claude Code JSONL parser
│       └── codex.py     # Codex CLI JSONL parser
└── pyproject.toml
```

## Stripping rules

- Remove fenced code blocks
- Remove tool_use / tool_result messages
- Remove thinking blocks
- Remove system prompts and developer messages
- Keep inline code references
- Keep user questions, assistant explanations, commentary

## Changelog

### v0.3.0

- `topics` — TF-IDF keyword extraction across conversations, shows dominant themes per project
- `cost` — actual API cost from token usage in raw JSONL files (cache-aware pricing for Claude and Codex)
- `export` — dump conversations to markdown or JSON files with `--project`, `--source`, `--format` filters

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
- [x] v0.3 — Topic extraction, conversation export, actual token cost tracking
- [ ] v0.4 — Local embeddings (sentence-transformers) + semantic search + cross-project connections
