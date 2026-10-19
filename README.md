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
llm-archive status         # grouped projects, task excerpts, and capture/sync health
llm-archive reopen --dry-run # preview recent project tabs
llm-archive automation enable # macOS: also save live tabs and recover automatically
```

Use a virtual environment if your Python installation requires one:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install the service after installing the package. It uses the executable from your environment, so keep that environment and checkout in place.

## Automatic project journal and recovery

The collector checks for changed session files every **120 seconds**. Sessions already in the archive are refreshed as they grow; unchanged files are skipped. It also reads nested Claude subagent transcripts and plain or Zstandard-compressed (`.jsonl.zst`) Codex sessions and archives. Compressed files are streamed, and a session appearing in both plain and compressed form is deduplicated. Repository remotes identify the same project across worktrees and computers; each session keeps its original directory, branch, computer, and transcript location.

Each checkpoint produces readable Markdown handoffs for projects active in the last seven days. A handoff includes recent requests and assistant updates, compact tool calls/results, and the current local Git branch, HEAD, and changed-file list when available. Search continues to index natural-language messages; tool evidence is stored separately. Handoffs use recorded evidence and run without an LLM or API costs. They do not infer that a task succeeded from an assistant's completion claim.

```bash
llm-archive status                       # last check, successful check, sync, recent work
llm-archive status --project holdco
llm-archive status --sessions            # individual session IDs, directories, and branches
llm-archive status --json                # machine-readable output
llm-archive resume holdco                # latest exchanges and continuation commands
llm-archive resume holdco --output /tmp/holdco-handoff.md
llm-archive resume holdco --session SESSION_ID --days 90
llm-archive checkpoint --no-sync         # immediate local checkpoint
llm-archive ingest --force               # reparse unchanged files after parser upgrades
```

By default, `status` groups sessions under each repository and shows short excerpts of the latest request, assistant update, and a reported next step when present. These are deterministic excerpts, not AI-generated conclusions. Each project summary refers to its most recently active session; `--sessions` exposes parallel tasks. Times are displayed in the computer's local timezone with a relative age; JSON retains the original timestamps.

### Preserve earlier transcript versions

If an agent rewrites or truncates a transcript, the current searchable view is updated while the previous captured messages, tool evidence, and session metadata are retained as an immutable version. Historical versions are included in private sync. Normal appends preserve existing message IDs and do not create redundant whole-conversation versions.

```bash
llm-archive history holdco
llm-archive history holdco --session SESSION_ID
llm-archive history holdco --revision REVISION_KEY --output /tmp/recovered-context.json
```

`history` lists preserved versions and lets you inspect or export one by its full key or a unique prefix. Previous versions are kept separately from the current search index. Version preservation protects captures made after this feature is installed; it cannot reconstruct content lost before it was captured.

`resume` prints notes; it does not launch an agent. Give the handoff to either agent in a new conversation, or use the recorded resume command when the original session is still on this computer. Project names that match more than one repository can be disambiguated with the repository identity shown in the handoff, such as `github.com/owner/project`.

### Reopen your projects in Ghostty

For hands-off recovery on macOS, enable it once:

```bash
llm-archive automation enable
```

This installs both the two-minute capture/sync service and a separate Ghostty observer. Continue starting Claude and Codex normally. The observer checks live terminals every **10 seconds** and maintains the `automatic` workspace, including multiple conversations in the same directory. It uses Claude's live session registrations and Codex's open transcript/writer files and session titles to identify the conversations actually running. Ambiguous terminals are saved as shell tabs rather than assigned a different conversation.

On your next login, it starts Ghostty if needed. When Ghostty relaunches after quitting or crashing, it waits for the app to settle, then opens the saved directories and resumes the identified local sessions automatically. Already-running agent sessions are skipped. Quitting Ghostty does not make it immediately pop back open; your next login or Ghostty launch triggers recovery. Closing individual tabs, or all windows while Ghostty keeps running, updates the saved workspace after the next stable observation. A quit or crash preserves the last captured workspace. Changes made immediately before a crash may fall between observations.

Snapshots are local first and join the existing private sync at the next checkpoint; recovery works offline. The observer runs separately so a slow Git push cannot block workspace capture. No prompts are submitted to agents and no commands from transcripts are replayed. Normal operation needs no save or reopen commands.

```bash
llm-archive automation status             # last check, capture, recovery, tab/session counts
llm-archive status                        # includes automatic recovery health
llm-archive automation disable            # stop tab recovery; keep capture and private sync
llm-archive automation recover --dry-run  # optional preview of what is still missing
llm-archive automation recover            # retry after a reported recovery error
```

If a recovery is interrupted or fails partway through, the original snapshot is retained and the observer stops retrying that launch to avoid repeated tabs. `status` reports the error; `automation recover` retries only missing sessions. macOS may require one-time Automation permission for the background observer to control Ghostty.

Enabling recovery adds a marked `window-save-state = never` override to `~/Library/Application Support/com.mitchellh.ghostty/config`. This [macOS configuration file loads after XDG configuration](https://ghostty.org/docs/config), so native restoration does not create a second copy of the saved windows. Your other settings are retained, including Nix-managed XDG configuration. Disabling recovery removes only this block and reloads the previous settings. An edited block or a symlinked macOS override file is reported instead of overwritten.

Recovery recreates terminals as tabs; it does not reconstruct split/window layouts, arbitrary programs, or uncommitted source files. The observer never types into or closes an existing terminal. Shell integration must report a working directory to recover a shell-only tab; a missing directory on a newly opened shell is reported and does not block other sessions. Agent identification depends on the native metadata available in your installed agent versions. On another computer, install/configure the collector once: synced workspaces and handoffs are available there, but original native transcripts and source checkouts must exist locally for an exact agent resume. Automatic recovery uses this computer's own workspace, so it does not unexpectedly open another computer's tabs.

For occasional manual restoration, restore recent project directories and worktrees with one command:

```bash
llm-archive reopen --dry-run             # preview; no windows or tabs are opened
llm-archive reopen                       # open directories as Ghostty tabs
llm-archive reopen --resume              # also resume the latest local agent session per directory
llm-archive reopen --project holdco      # restore just one project
llm-archive reopen --days 7 --limit 50   # expand the default three days / ten directories
llm-archive reopen --session SESSION_ID --resume # select a particular session
llm-archive reopen --new-tabs           # explicitly allow tabs for directories already open
```

The command opens one tab per distinct directory, with the project as its title. It skips directories already reported open by Ghostty; `--new-tabs` overrides this, including when you want to resume a different session in an already-open directory. For a directory with several agent sessions, `--resume` selects the most recently active session unless `--session` names one explicitly. Find IDs with `status --sessions`. It executes only the fixed `codex resume ID` or `claude --resume ID` continuation command, never commands copied out of a transcript. It does not recover the previous split layout or detect which projects you intentionally closed. Duplicate detection relies on Ghostty's reported working directories, which may be unavailable before shell integration initializes.

Requires **macOS and Ghostty 1.3+**, using its [native AppleScript API](https://ghostty.org/docs/features/applescript). macOS may request Automation permission the first time. Default tab restoration opens your normal shell; `--resume` explicitly starts the recorded agent session.

On another computer, map recorded directories to your local checkouts:

```bash
llm-archive sync
llm-archive reopen --projects-root ~/projects --days 7 --dry-run
llm-archive reopen --projects-root ~/projects --days 7
llm-archive resume holdco
```

Mapping tries the original directory name, then the project name. Missing directories are reported and skipped; repositories are not cloned or switched to another branch automatically. An agent's native session can only be resumed when its original transcript exists on this device. Synced handoffs still work as context for a fresh conversation.

### Named workspaces

Save an explicit selection you can restore later:

```bash
llm-archive workspace save daily                         # recent local project directories
llm-archive workspace save research --project holdco --project concrete2
llm-archive workspace save current --from-tabs            # the directories open in Ghostty now
llm-archive workspace list
llm-archive reopen --workspace daily --dry-run
llm-archive reopen --workspace daily --resume
```

A manually saved workspace records directories and the selected agent session per directory, including shell-only tabs when using `--from-tabs`. Saving the same local name updates that selection. The observer manages the reserved `automatic` workspace and preserves individual conversations there; use `automation recover` to restore all of them. Workspace files store no terminal output or arbitrary startup commands. Definitions sync with the journal and can be restored on another computer using `--projects-root`. When several computers saved the same name, the local definition is preferred; use the `DEVICE_ID:NAME` selector shown by `workspace list` to choose a foreign definition.

### Private sync between computers

Create a **separate, empty private Git repository** for journal data, then configure the same remote once on each computer:

```bash
llm-archive sync-init git@github.com:YOUR_USER/llm-journal-private.git
llm-archive checkpoint
llm-archive install-service
```

Use an existing Git credential helper or an SSH key available without an interactive password prompt. Every checkpoint commits this computer's snapshot, fetches and imports other computers' updates, then pushes. You can also run `llm-archive sync` manually. Local capture continues when the remote is unavailable; the next checkpoint retries, and `status` reports the last successful sync and any error. Automated snapshot commits use the `llm-archive` identity and are unsigned.

Each installation has a persistent device ID and owns a separate path in the sync repository. Sessions are stored as individual compressed `.json.gz` records, keeping Git objects small instead of rewriting one large combined archive. Exchanged records include stripped conversations, compact tool evidence, preserved transcript versions, named workspace definitions, project/session metadata, directory paths, and computer names. These records can contain sensitive conversation or command output: use a private repository. The source-code repository is not the sync destination. The SQLite database, embeddings, agent credentials, and agent configuration files are not uploaded. Sync is disabled until `sync-init` is run.

Sync format version 2 can read older JSONL device snapshots and writes compressed session records. Upgrade the collector on every computer before using version 2; older collectors reject its format marker rather than silently overlooking new records.

On each additional computer, install the same version of `llm-archive`, run `sync-init` with the same private remote, run `sync` to download existing context, and install the background service. Each computer then pulls and pushes at every checkpoint automatically. A repository existing on GitHub alone does not collect activity from computers without the collector installed.

### Checkpoint scheduling

```bash
llm-archive install-service                 # every 120 seconds
llm-archive install-service --interval 300  # every five minutes
llm-archive uninstall-service
llm-archive watch --interval 120            # foreground alternative; stop with Ctrl-C
```

macOS uses a launchd agent with an immediate check on login/load; Linux uses a systemd user timer, with a crontab fallback. `install-cron` and `uninstall-cron` remain aliases for existing users. Minimum interval is 60 seconds. For the crontab fallback, the interval must be a whole number of minutes dividing an hour, such as two, five, or ten minutes. Reinstall the service to change its interval or source paths.

Only one ingest/sync writer runs at a time. Completed file updates commit individually, partial trailing JSONL records are retried after the writer finishes them, and file signatures let collection catch up after downtime. `status` distinguishes a successful scan from the latest source activity and remote sync. A sleeping, powered-off, or offline computer cannot checkpoint or upload new work; anything not yet written locally or synced can still be lost.

**The journal preserves context, not source-code backups.** It records changed-file names but does not save file contents, untracked files, or uncommitted changes. Keep your normal code backup/Git workflow. Sessions with persistence disabled and remote-only sessions without local transcripts are not captured. Transcript formats can change after agent upgrades; adapters may need updating, and `ingest --force` can then rebuild affected records while preserving previous versions when content changes.

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
├── overview.py      # project summaries and local/relative times
├── terminal.py      # Ghostty project-tab restoration
├── recovery.py      # live workspace observer and automatic agent recovery
├── ghostty_config.py # reversible coordination with native window restoration
├── workspaces.py    # named directory/session selections
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
| `workspaces/` | Local and imported named workspace definitions |
| `recovery-state.json`, `recovery.log` | Local observer health and recovery attempts |
| `device-id` | Stable identity for this installation |
| `config.json` | Journal sync configuration |
| `sync/` | Optional clone of the dedicated private journal repository |
| `collector.lock` | Prevents overlapping ingest/sync writes |

## Development

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases, source files, and local Git remotes. They do not read your agent histories, open terminal apps, or contact model APIs.
