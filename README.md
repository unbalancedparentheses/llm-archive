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

# Weekly summary via Claude API (needs ANTHROPIC_API_KEY)
llm-archive summarize --days 7

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
                              [search] [timeline] [hours] [summarize] [recurring]
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
- [ ] v0.2 — Automatic daily ingestion (launchd), summary output to Obsidian or markdown files
- [ ] v0.3 — Stats and patterns: time-per-project, Claude vs Codex usage split, topic extraction
- [ ] v0.4 — Local embeddings (sentence-transformers) + semantic search + cross-project connections
