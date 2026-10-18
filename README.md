# llm-archive

Your LLM conversations are full of decisions, ideas, and context that disappear after each session. This tool saves them.

It ingests conversations from Claude Code and Codex CLI, strips the code to keep only the natural language, and gives you search, analytics, and idea mining over everything you've discussed with AI. It also keeps a project journal, creates recovery handoffs, and reopens recent projects as Ghostty tabs.

**Keep running Codex and Claude exactly as you do today.** A separate background collector reads the session files they already write. No wrappers, agent hooks, prompt changes, or project configuration edits are needed.

## Quick start

```bash
pip install -e .
llm-archive ingest          # parse conversations into SQLite
llm-archive embed           # build semantic search index (optional, needs ollama)
llm-archive checkpoint     # capture current work and write project handoffs
llm-archive install-service # checkpoint every two minutes + on login
llm-archive status         # recent projects and capture/sync health
llm-archive reopen --dry-run # preview recent project tabs
```

Use a virtual environment if your Python installation requires one:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install the service after installing the package. It uses the executable from your environment, so keep that environment and checkout in place.

## Automatic project journal and recovery

The collector checks for changed session files every **120 seconds**. Sessions already in the archive are refreshed as they grow; unchanged files are skipped. It also reads nested Claude subagent transcripts and uncompressed archived Codex sessions. Repository remotes identify the same project across worktrees and computers; each session keeps its original directory, branch, computer, and transcript location.

Each checkpoint produces readable Markdown handoffs for projects active in the last seven days. A handoff includes recent requests and assistant updates, compact tool calls/results, and the current local Git branch, HEAD, and changed-file list when available. Search continues to index natural-language messages; tool evidence is stored separately. Handoffs use recorded evidence and run without an LLM or API costs. They do not infer that a task succeeded from an assistant's completion claim.

```bash
llm-archive status                       # last check, successful check, sync, recent work
llm-archive status --project holdco
llm-archive status --json                # machine-readable output
llm-archive resume holdco                # latest exchanges and continuation commands
llm-archive resume holdco --output /tmp/holdco-handoff.md
llm-archive resume holdco --session SESSION_ID --days 90
llm-archive checkpoint --no-sync         # immediate local checkpoint
llm-archive ingest --force               # reparse unchanged files after parser upgrades
```

`resume` prints notes; it does not launch an agent. Give the handoff to either agent in a new conversation, or use the recorded resume command when the original session is still on this computer. Project names that match more than one repository can be disambiguated with the repository identity shown in the handoff, such as `github.com/owner/project`.

### Reopen your projects in Ghostty

After a crash, reboot, or break, restore recent project directories and worktrees with one command:

```bash
llm-archive reopen --dry-run             # preview; no windows or tabs are opened
llm-archive reopen                       # open directories as Ghostty tabs
llm-archive reopen --resume              # also resume the latest local agent session per directory
llm-archive reopen --project holdco      # restore just one project
llm-archive reopen --days 7 --limit 50   # expand the default three days / ten directories
```

The command opens one tab per distinct directory, with the project as its title. For a directory with several agent sessions, `--resume` selects the most recently active session. It executes only the fixed `codex resume ID` or `claude --resume ID` continuation command, never commands copied out of a transcript. Running it again can create additional tabs. It does not recover the previous split layout or detect which projects you intentionally closed.

Requires **macOS and Ghostty 1.3+**, using its [native AppleScript API](https://ghostty.org/docs/features/applescript). macOS may request Automation permission the first time. Default tab restoration opens your normal shell; `--resume` explicitly starts the recorded agent session.

On another computer, map recorded directories to your local checkouts:

```bash
llm-archive sync
llm-archive reopen --projects-root ~/projects --days 7 --dry-run
llm-archive reopen --projects-root ~/projects --days 7
llm-archive resume holdco
```

Mapping tries the original directory name, then the project name. Missing directories are reported and skipped; repositories are not cloned or switched to another branch automatically. An agent's native session can only be resumed when its original transcript exists on this device. Synced handoffs still work as context for a fresh conversation.

### Private sync between computers

Create a **separate, empty private Git repository** for journal data, then configure the same remote once on each computer:

```bash
llm-archive sync-init git@github.com:YOUR_USER/llm-journal-private.git
llm-archive checkpoint
llm-archive install-service
```

Use an existing Git credential helper or an SSH key available without an interactive password prompt. Every checkpoint commits this computer's snapshot, fetches and imports other computers' updates, then pushes. You can also run `llm-archive sync` manually. Local capture continues when the remote is unavailable; the next checkpoint retries, and `status` reports the last successful sync and any error. Automated snapshot commits use the `llm-archive` identity and are unsigned.

Each installation has a persistent device ID and owns a separate path in the sync repository. Exchanged JSONL records include stripped conversations, compact tool evidence, project/session metadata, directory paths, and computer names. These records can contain sensitive conversation or command output: use a private repository. The source-code repository is not the sync destination. The SQLite database, embeddings, agent credentials, and agent configuration files are not uploaded. Sync is disabled until `sync-init` is run.

### Checkpoint scheduling

```bash
llm-archive install-service                 # every 120 seconds
llm-archive install-service --interval 300  # every five minutes
llm-archive uninstall-service
llm-archive watch --interval 120            # foreground alternative; stop with Ctrl-C
```

macOS uses a launchd agent with an immediate check on login/load; Linux uses a systemd user timer, with a crontab fallback. `install-cron` and `uninstall-cron` remain aliases for existing users. Minimum interval is 60 seconds. For the crontab fallback, the interval must be a whole number of minutes dividing an hour, such as two, five, or ten minutes. Reinstall the service to change its interval or source paths.

Only one ingest/sync writer runs at a time. Completed file updates commit individually, partial trailing JSONL records are retried after the writer finishes them, and file signatures let collection catch up after downtime. `status` distinguishes a successful scan from the latest source activity and remote sync. A sleeping, powered-off, or offline computer cannot checkpoint or upload new work; anything not yet written locally or synced can still be lost.

**The journal preserves context, not source-code backups.** It records changed-file names but does not save file contents, untracked files, or uncommitted changes. Keep your normal code backup/Git workflow. Sessions with persistence disabled, remote-only sessions without local transcripts, and compressed `.jsonl.zst` archives are not captured. Transcript formats can change after agent upgrades; adapters may need updating, and `ingest --force` can then rebuild affected records.

Custom locations are supported through `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and `LLM_ARCHIVE_HOME`. The service preserves these variables and your `PATH` when installed. A newly configured custom source directory needs a service reinstall. Do not copy `device-id` between machines; each installation should have its own ID.

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

The journal additionally retains compact tool evidence in a separate table, without adding full tool results to the search index. Full code patches and tool environment dictionaries are not retained as tool-call evidence.

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

Run `llm-archive install-service` (or `install-cron`) to set up periodic checkpoints:

- **macOS**: launchd agent (every two minutes + on login)
- **Linux**: systemd user timer, or crontab fallback (every two minutes)

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
├── journal.py       # recent activity, handoffs, and checkpoints
├── terminal.py      # Ghostty project-tab restoration
├── sync.py          # per-device snapshots in a dedicated private Git repo
├── paths.py         # local state, device identity, and collector locking
├── projects.py      # repository/worktree identity
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

Everything lives under `~/.local/share/llm-archive/` (or `LLM_ARCHIVE_HOME`):

| File | Purpose |
|------|---------|
| `archive.db` | SQLite database with FTS5 index |
| `embeddings.npz` | Semantic search vectors (~150MB for 50k messages) |
| `embeddings_meta.json` | Embedding provider and dimension info |
| `summaries/YYYY-MM-DD.md` | Generated weekly digests |
| `ideas/YYYY-MM-DD.md` | Mined ideas and insights |
| `ingest.log` | Auto-ingest log |
| `journal/PROJECT-HASH.md` | Automatic project recovery handoffs |
| `device-id` | Stable identity for this installation |
| `config.json` | Journal sync configuration |
| `sync/` | Optional clone of the dedicated private journal repository |
| `collector.lock` | Prevents overlapping ingest/sync writes |

## Development

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases, source files, and local Git remotes. They do not read your agent histories, open terminal apps, or contact model APIs.
