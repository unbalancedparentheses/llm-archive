# llm-archive

A local tool to ingest, search, and analyze conversations from LLM coding assistants — stripping code to keep only the natural language exchange.

## Supported sources

- **Claude Code** — parses `~/.claude/projects/**/*.jsonl`
- **Codex CLI** — parses `~/.codex/sessions/**/*.jsonl` + `~/.codex/history.jsonl`

Adding new sources (e.g., ChatGPT export JSON) is just another parser module.

## Data formats

### Claude Code

- Location: `~/.claude/projects/<project>/<session>.jsonl`
- Top-level `type: "user"` / `type: "assistant"`
- Content in `message.content` — array of `{type: "text", text: "..."}` or `{type: "tool_use", ...}`
- Metadata: `cwd`, `gitBranch`, `timestamp`, `sessionId`

### Codex CLI

- Location: `~/.codex/sessions/YYYY/MM/DD/<session>.jsonl`
- `type: "response_item"` with `payload.role: "user"` / `"assistant"`
- Content in `payload.content` — array of `{type: "output_text", text: "..."}` or `{type: "input_text", ...}`
- Has `payload.phase: "commentary"` vs `"final"`
- User prompt index at `~/.codex/history.jsonl`

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
                                   [search]  [summarize]
```

One ingester per source, one shared schema. Each source gets a parser module that normalizes into the same `(role, text, timestamp, metadata)` tuples.

## Schema

```sql
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    session_id TEXT UNIQUE,
    source TEXT,          -- "claude" or "codex"
    project TEXT,         -- extracted from directory path
    git_branch TEXT,
    started_at TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role TEXT,            -- "user" or "assistant"
    content TEXT,         -- natural language only, code stripped
    timestamp TEXT
);

CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id);
```

## Stripping rules

- Remove fenced code blocks (``` ... ```)
- Remove tool_use / tool_result messages entirely
- Remove thinking blocks
- Remove system prompts and developer messages
- Keep: user questions, assistant explanations, commentary, final answers

## Project structure

```
llm-archive/
├── llm_archive/
│   ├── __init__.py
│   ├── cli.py           # click CLI: ingest, search, summarize
│   ├── db.py            # SQLite setup + FTS5
│   ├── strip.py         # remove code blocks, tool calls
│   ├── parsers/
│   │   ├── claude.py    # Claude Code JSONL parser
│   │   └── codex.py     # Codex JSONL parser
│   └── summarize.py     # Claude API weekly digest
└── pyproject.toml
```

## CLI usage (planned)

```bash
# Ingest new conversations (run daily via cron or launchd)
llm-archive ingest

# Search
llm-archive search "trusted boundary report"
llm-archive search "how did we handle auth" --project holdco

# Semantic search (v2, with embeddings)
llm-archive similar "memory layout padding alignment"

# Weekly summary
llm-archive summarize --week
llm-archive summarize --month

# Topics
llm-archive topics --week
```

## Analysis goals

- **What you ask most often** — recurring questions reveal knowledge gaps or tooling friction
- **Which answers you reject/correct** — tracks where LLMs fail you, so you can adjust prompts
- **Topic clusters over time** — what you're actually spending time on vs. what you think you are
- **Prompt patterns that get good answers** — reverse-engineer your most effective prompting style
- **Knowledge evolution** — what you asked about 3 months ago vs. now shows learning trajectory

## Roadmap

- [ ] v0.1 — Ingester + SQLite with FTS5 + keyword search CLI
- [ ] v0.2 — Claude API weekly/monthly summaries
- [ ] v0.3 — Local embeddings (sentence-transformers) + semantic search via sqlite-vss
- [ ] v0.4 — Topic clustering + trends over time
