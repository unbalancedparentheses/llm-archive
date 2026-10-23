import os
import platform
import subprocess
import sys
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import click

from llm_archive import db
from llm_archive.ingest import run_ingest

MAX_GAP = timedelta(minutes=30)


def _check_scheduler(show_db=True):
    if not sys.stdout.isatty():
        return
    system = platform.system()
    if system == "Darwin":
        result = subprocess.run(
            ["launchctl", "list", "com.llm-archive.ingest"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            click.secho("auto-ingest: active (launchd)", fg="green")
        else:
            click.secho("auto-ingest: not installed (run: llm-archive install-cron)", fg="yellow")
    elif system == "Linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "llm-archive-ingest.timer"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            click.secho("auto-ingest: active (systemd)", fg="green")
        else:
            # Check crontab fallback
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if result.returncode == 0 and "llm-archive-ingest" in result.stdout:
                click.secho("auto-ingest: active (crontab)", fg="green")
            else:
                click.secho("auto-ingest: not installed (run: llm-archive install-cron)", fg="yellow")

    if not show_db:
        return
    db_path = db.DB_PATH
    if db_path.exists():
        mtime = datetime.fromtimestamp(db_path.stat().st_mtime)
        age = datetime.now() - mtime
        if age.days > 2:
            click.secho(f"db: last updated {age.days} days ago", fg="yellow")
        else:
            click.secho(f"db: updated {mtime.strftime('%Y-%m-%d %H:%M')}", fg="green")
    else:
        click.secho("db: not found (run: llm-archive ingest)", fg="yellow")
    click.echo()


@click.group()
@click.pass_context
def cli(ctx):
    """Ingest, search, and analyze LLM conversations."""
    if ctx.invoked_subcommand != "status":
        _check_scheduler()


@cli.command()
@click.option("--force", is_flag=True, help="Reparse all files, including unchanged ones")
def ingest(force):
    """Ingest conversations from Claude Code and Codex CLI."""
    try:
        new_convs, new_msgs = run_ingest(force=force)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Updated {new_convs} conversations ({new_msgs} new or refreshed messages)")


@cli.command()
@click.option("--rebuild", is_flag=True, help="Rebuild all embeddings from scratch")
def embed(rebuild):
    """Build or update the semantic embedding index."""
    from llm_archive.embeddings import build_embeddings, get_provider

    provider, dim = get_provider()
    click.echo(f"Embedding provider: {provider} ({dim} dimensions)")

    conn = db.get_connection()

    def progress(done, total):
        click.echo(f"  {done}/{total} messages embedded...", nl=False)
        click.echo("\r", nl=False)

    new, total = build_embeddings(conn, rebuild=rebuild, progress_fn=progress)
    conn.close()

    if new > 0:
        click.echo(f"\nEmbedded {new} new messages ({total} total)")
    else:
        click.echo(f"Already up to date ({total} messages embedded)")


@cli.command()
@click.argument("query")
@click.option("--project", default=None, help="Filter by project name")
@click.option("--source", type=click.Choice(["claude", "codex"]), default=None)
@click.option("--limit", default=20, help="Max results")
@click.option("--semantic", is_flag=True, help="Use semantic similarity (requires: llm-archive embed)")
def search(query, project, source, limit, semantic):
    """Full-text search across conversations."""
    conn = db.get_connection()

    if semantic:
        from llm_archive.embeddings import semantic_search
        results = semantic_search(conn, query, project=project, source=source, limit=limit)
        conn.close()

        if not results:
            click.echo("No results found.")
            return

        for r in results:
            score = click.style(f"{r['score']:.3f}", fg="yellow")
            header = f"[{r['source']}/{r['project']}]"
            if r["git_branch"]:
                header += f" ({r['git_branch']})"
            header += f" {r['timestamp']}  score={score}"
            click.secho(header, fg="cyan")
            click.echo(f"  {r['role']}: {r['snippet']}")
            click.echo()
    else:
        results = db.search(conn, query, project=project, source=source, limit=limit)
        conn.close()

        if not results:
            click.echo("No results found.")
            return

        for r in results:
            header = f"[{r['source']}/{r['project']}]"
            if r["git_branch"]:
                header += f" ({r['git_branch']})"
            header += f" {r['timestamp']}"
            click.secho(header, fg="cyan")
            click.echo(f"  {r['role']}: {r['snippet']}")
            click.echo()


@cli.command()
@click.option("--days", default=14, help="Number of days to show")
@click.option("--project", default=None, help="Filter by project name")
def timeline(days, project):
    """Show which projects you worked on each day."""
    conn = db.get_connection()
    rows = db.timeline(conn, days=days, project=project)
    conn.close()

    if not rows:
        click.echo("No conversations in this period.")
        return

    current_day = None
    for r in rows:
        if r["day"] != current_day:
            current_day = r["day"]
            click.echo()
            click.secho(current_day, fg="green", bold=True)
        source_tag = click.style(f"[{r['source']}]", fg="cyan")
        click.echo(f"  {source_tag} {r['project']}: {r['convs']} convs, {r['msgs']} msgs")


@cli.command()
@click.option("--days", default=30, help="Number of days to show")
def hours(days):
    """Show active LLM usage hours per day."""
    conn = db.get_connection()
    rows = db.message_timestamps(conn, days=days)
    conn.close()

    by_day = defaultdict(list)
    for day, ts in rows:
        try:
            t = datetime.fromisoformat(ts[:19].replace("T", " "))
            by_day[day].append(t)
        except ValueError:
            continue

    if not by_day:
        click.echo("No data in this period.")
        return

    total_hours = 0.0
    for day in sorted(by_day.keys(), reverse=True):
        timestamps = sorted(by_day[day])
        active = timedelta()
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            if gap <= MAX_GAP:
                active += gap

        h = active.total_seconds() / 3600
        total_hours += h
        first = timestamps[0].strftime("%H:%M")
        last = timestamps[-1].strftime("%H:%M")
        bar = click.style("█" * int(h), fg="green")
        click.echo(f"{day}  {first}–{last} UTC  {h:5.1f}h  {len(timestamps):5d} msgs  {bar}")

    avg = total_hours / len(by_day)
    click.echo()
    click.echo(f"Total: {total_hours:.0f}h over {len(by_day)} days, avg {avg:.1f}h/day")


@cli.command()
@click.option("--days", default=30, help="Number of days to analyze")
@click.option("--by-source", is_flag=True, help="Break down by source (Claude vs Codex)")
def projects(days, by_source):
    """Show active hours per project."""
    conn = db.get_connection()
    rows = db.project_timestamps(conn, days=days)
    conn.close()

    if by_source:
        by_key = defaultdict(list)
        for project, day, ts, source in rows:
            try:
                t = datetime.fromisoformat(ts[:19].replace("T", " "))
                by_key[(project, source)].append(t)
            except ValueError:
                continue

        if not by_key:
            click.echo("No data in this period.")
            return

        key_hours = {}
        for key, timestamps in by_key.items():
            timestamps.sort()
            active = timedelta()
            for i in range(1, len(timestamps)):
                gap = timestamps[i] - timestamps[i - 1]
                if gap <= MAX_GAP:
                    active += gap
            key_hours[key] = active.total_seconds() / 3600

        ranked = sorted(key_hours.items(), key=lambda x: x[1], reverse=True)
        total = sum(h for _, h in ranked)

        # Source totals
        source_totals = defaultdict(float)
        for (project, source), h in ranked:
            source_totals[source] += h

        click.secho("By source:", bold=True)
        for source in sorted(source_totals, key=source_totals.get, reverse=True):
            h = source_totals[source]
            pct = (h / total * 100) if total > 0 else 0
            bar = click.style("█" * int(h), fg="green")
            click.echo(f"  {source:<10s} {h:5.1f}h  {pct:4.0f}%  {bar}")
        click.echo()

        click.secho("By project + source:", bold=True)
        for (project, source), h in ranked:
            if h < 0.1:
                continue
            pct = (h / total * 100) if total > 0 else 0
            label = f"{project} [{source}]"
            bar = click.style("█" * int(h), fg="green")
            click.echo(f"  {label:<40s} {h:5.1f}h  {pct:4.0f}%  {bar}")
    else:
        by_project = defaultdict(list)
        for project, day, ts, source in rows:
            try:
                t = datetime.fromisoformat(ts[:19].replace("T", " "))
                by_project[project].append(t)
            except ValueError:
                continue

        if not by_project:
            click.echo("No data in this period.")
            return

        project_hours = {}
        for project, timestamps in by_project.items():
            timestamps.sort()
            active = timedelta()
            for i in range(1, len(timestamps)):
                gap = timestamps[i] - timestamps[i - 1]
                if gap <= MAX_GAP:
                    active += gap
            project_hours[project] = active.total_seconds() / 3600

        ranked = sorted(project_hours.items(), key=lambda x: x[1], reverse=True)
        total = sum(h for _, h in ranked)

        for project, h in ranked:
            if h < 0.1:
                continue
            pct = (h / total * 100) if total > 0 else 0
            bar = click.style("█" * int(h), fg="green")
            click.echo(f"  {project:<30s} {h:5.1f}h  {pct:4.0f}%  {bar}")

    click.echo()
    click.echo(f"Total: {total:.0f}h across {len(ranked)} projects ({days} days)")


@cli.command()
@click.argument("date")
def day(date):
    """Drill into a specific day. Usage: llm-archive day 2026-03-12"""
    conn = db.get_connection()
    rows = db.day_detail(conn, date)
    conn.close()

    if not rows:
        click.echo(f"No conversations on {date}.")
        return

    click.secho(f"{date} — {len(rows)} conversations\n", fg="green", bold=True)
    for r in rows:
        time = r["start_time"] or "??:??"
        source_tag = click.style(f"[{r['source']}]", fg="cyan")
        question = (r["first_question"] or "")[:120].replace("\n", " ")
        click.echo(f"  {time} UTC  {source_tag} {r['project']} ({r['msgs']} msgs)")
        if question:
            click.echo(f"    {question}")
        click.echo()


@cli.command()
@click.option("--days", default=7, help="Number of days to summarize")
def summarize(days):
    """Generate a summary of recent conversations."""
    conn = db.get_connection()
    text = db.summarize_text(conn, days=days)
    conn.close()

    if not text:
        click.echo("No conversations in this period.")
        return

    from llm_archive.summarize import estimate_cost
    cost_str, provider = estimate_cost(text, max_output_tokens=2000)
    if provider == "none":
        click.echo("No LLM provider available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or install ollama.")
        return
    if not click.confirm(f"{cost_str}. Continue?"):
        return

    click.echo(f"Summarizing last {days} days via {provider}...")
    from llm_archive.summarize import weekly_summary
    summary = weekly_summary(text, days=days)
    click.echo()
    click.echo(summary)

    # Save to markdown file
    summaries_dir = db.DB_DIR / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = summaries_dir / f"{today}.md"
    md_path.write_text(f"# LLM Summary — {today} (last {days} days)\n\n{summary}\n")
    click.secho(f"\nSaved to {md_path}", fg="green")


@cli.command()
@click.option("--days", default=30, help="Number of days to mine")
@click.option("--project", default=None, help="Filter by project")
def ideas(days, project):
    """Find ideas, problems, arguments, and unexplored threads in your conversations."""
    conn = db.get_connection()
    text = db.summarize_text(conn, days=days)
    conn.close()

    if not text:
        click.echo("No conversations in this period.")
        return

    from llm_archive.summarize import estimate_cost
    cost_str, provider = estimate_cost(text, max_output_tokens=4000)
    if provider == "none":
        click.echo("No LLM provider available. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or install ollama.")
        return
    if not click.confirm(f"{cost_str}. Continue?"):
        return

    click.echo(f"Mining conversations via {provider}...")
    from llm_archive.summarize import extract_ideas
    result = extract_ideas(text, days=days)
    click.echo()
    click.echo(result)

    # Save to file
    ideas_dir = db.DB_DIR / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = ideas_dir / f"{today}.md"
    md_path.write_text(f"# Ideas & Gems — {today} (last {days} days)\n\n{result}\n")
    click.secho(f"\nSaved to {md_path}", fg="green")


@cli.command()
@click.option("--days", default=30, help="Number of days to analyze")
@click.option("--threshold", default=0.4, help="Similarity threshold (0-1)")
@click.option("--min-count", default=3, help="Minimum occurrences to report")
def recurring(days, threshold, min_count):
    """Find questions you keep asking."""
    conn = db.get_connection()
    messages = db.user_messages(conn, days=days)
    conn.close()

    if not messages:
        click.echo("No user messages in this period.")
        return

    from llm_archive.recurring import find_recurring
    clusters = find_recurring(messages, threshold=threshold, min_cluster=min_count)

    if not clusters:
        click.echo("No recurring patterns found.")
        return

    click.echo(f"Found {len(clusters)} recurring patterns:\n")
    for c in clusters[:20]:
        projects = ", ".join(c["projects"])
        click.secho(f"  {c['count']}x across [{projects}]", fg="yellow")
        click.echo(f"    \"{c['example']}\"")
        click.echo(f"    {c['first'][:10]} — {c['last'][:10]}")
        click.echo()


@cli.command(name="install-cron")
@click.option("--interval", default=120, type=click.IntRange(min=60), show_default=True, help="Seconds between checkpoints")
def install_cron(interval):
    """Install periodic capture, handoffs, and configured sync (also: install-service)."""
    from llm_archive.scheduler import install
    try:
        click.echo(install(interval=interval))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="uninstall-cron")
def uninstall_cron():
    """Remove automatic checkpoints (also: uninstall-service)."""
    from llm_archive.scheduler import uninstall
    result = uninstall()
    click.echo(result)


@cli.command()
@click.option("--days", default=30, help="Number of days to export")
@click.option("--project", default=None, help="Filter by project")
@click.option("--source", type=click.Choice(["claude", "codex"]), default=None)
@click.option("--format", "fmt", type=click.Choice(["md", "json"]), default="md", help="Output format")
@click.option("--output", "output_dir", default=None, help="Output directory (default: ./llm-export)")
def export(days, project, source, fmt, output_dir):
    """Export conversations to markdown or JSON files."""
    conn = db.get_connection()
    convs = db.export_conversations(conn, days=days, project=project, source=source)
    conn.close()

    if not convs:
        click.echo("No conversations to export.")
        return

    out = Path(output_dir) if output_dir else Path("llm-export")
    out.mkdir(parents=True, exist_ok=True)

    for c in convs:
        date = (c["started_at"] or "unknown")[:10]
        safe_project = c["project"].replace("/", "-")
        filename = f"{date}_{safe_project}_{c['session_id'][:8]}"

        if fmt == "md":
            lines = [
                f"# {c['project']} — {c['started_at']}",
                f"",
                f"**Source:** {c['source']}",
            ]
            if c["git_branch"]:
                lines.append(f"**Branch:** {c['git_branch']}")
            lines.append("")
            lines.append("---")
            lines.append("")
            for m in c["messages"]:
                role_label = "User" if m["role"] == "user" else "Assistant"
                lines.append(f"### {role_label}")
                lines.append("")
                lines.append(m["content"])
                lines.append("")

            (out / f"{filename}.md").write_text("\n".join(lines))
        else:
            import json
            (out / f"{filename}.json").write_text(json.dumps(c, indent=2, ensure_ascii=False))

    click.echo(f"Exported {len(convs)} conversations to {out}/")


@cli.command()
@click.option("--days", default=30, help="Number of days to analyze")
def cost(days):
    """Show API cost from actual token usage in JSONL files."""
    click.echo("Scanning raw JSONL files for token usage...")
    from llm_archive.cost import compute_costs
    data = compute_costs(days=days)

    by_source = data["by_source"]
    by_project = data["by_project"]

    if not by_source:
        click.echo("No usage data found.")
        return

    total_cost = 0.0

    click.secho("\nBy source:", bold=True)
    for source in sorted(by_source):
        d = by_source[source]
        total_cost += d["cost"]
        click.echo(
            f"  {source:<10s}  {d['input_tokens']:>12,} input tok  "
            f"{d['output_tokens']:>12,} output tok  ${d['cost']:,.2f}"
        )

    click.echo()
    click.secho("By project (top 15):", bold=True)
    ranked = sorted(by_project.items(), key=lambda x: x[1]["cost"], reverse=True)
    for project, d in ranked[:15]:
        if d["cost"] < 0.50:
            continue
        click.echo(f"  {project:<30s}  ${d['cost']:,.2f}")

    click.echo()
    click.echo(f"Total: ${total_cost:,.2f} over {days} days")


@cli.command()
@click.option("--days", default=30, help="Number of days to analyze")
@click.option("--project", default=None, help="Filter by project")
@click.option("--top", default=30, help="Number of topics to show")
def topics(days, project, top):
    """Extract topics from recent conversations."""
    conn = db.get_connection()
    convs = db.conversation_texts(conn, days=days, project=project)
    conn.close()

    if not convs:
        click.echo("No conversations in this period.")
        return

    from llm_archive.topics import extract_topics
    results = extract_topics(convs, top_n=top)

    if not results:
        click.echo("No topics extracted.")
        return

    click.secho(f"Top {len(results)} topics ({days} days):\n", bold=True)
    for t in results:
        projects = ", ".join(t["projects"][:5])
        count_str = click.style(f"{t['count']}x", fg="yellow")
        click.echo(f"  {t['topic']:<25s} {count_str}  [{projects}]")


@cli.command()
def stats():
    """Show ingestion statistics."""
    conn = db.get_connection()
    s = db.stats(conn)
    conn.close()

    click.echo(f"Total: {s['total_conversations']} conversations, {s['total_messages']} messages\n")

    click.echo("By source:")
    for row in s["by_source"]:
        click.echo(f"  {row['source']}: {row['convs']} conversations")

    click.echo("\nTop projects:")
    for row in s["by_project"]:
        click.echo(f"  {row['project']}: {row['convs']} conversations")


@cli.command()
@click.option("--no-sync", is_flag=True, help="Capture locally without contacting the configured sync remote")
def checkpoint(no_sync):
    """Capture changed sessions, write handoffs, and sync if configured."""
    from llm_archive.journal import checkpoint as capture
    try:
        updated, messages, projects = capture(sync=not no_sync)
    except (RuntimeError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{db.utc_now()}: checked sessions; updated {updated} conversations, {messages} messages, {projects} project handoffs.")


@cli.command()
@click.option("--interval", default=120, type=click.IntRange(min=60), show_default=True)
def watch(interval):
    """Run checkpoints in the foreground until Ctrl-C."""
    from llm_archive.journal import checkpoint as capture
    try:
        while True:
            try:
                updated, messages, projects = capture()
                click.echo(f"{db.utc_now()}: {updated} sessions updated; {projects} project handoffs.")
            except (OSError, RuntimeError, ValueError) as exc:
                click.echo(f"{db.utc_now()}: {exc}", err=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option("--days", default=7, type=click.IntRange(min=1), show_default=True)
@click.option("--project", default=None, help="Project name or repository identity")
@click.option("--limit", default=20, type=click.IntRange(min=1), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable status and recent sessions")
@click.option("--sessions", "show_sessions", is_flag=True, help="Show individual sessions instead of grouping by project")
def status(days, project, limit, as_json, show_sessions):
    """See capture/sync health and recent work across projects and computers."""
    from llm_archive.journal import recent_sessions
    from llm_archive.paths import read_config
    from llm_archive.overview import display_time, project_overview, session_summary
    from llm_archive.recovery import read_state
    recovery = {"enabled": bool(read_config().get("automatic_recovery")), **read_state()}
    conn = db.get_connection()
    try:
        state = db.get_state(conn)
        sessions = recent_sessions(conn, days=days, project=project, limit=10000)
        projects = project_overview(conn, sessions)[:limit]
        details = [{**s, **session_summary(conn, s)} for s in sessions[:limit]] if show_sessions else []
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps({"collector": state, "sync_configured": bool(read_config().get("sync_directory")), "recovery": recovery,
                               "projects": projects, "sessions": details if show_sessions else sessions[:limit]}, indent=2))
        return
    _check_scheduler(show_db=False)
    click.echo(f"Last check:      {display_time(state.get('last_check'))}")
    click.echo(f"Last successful: {display_time(state.get('last_success'))}")
    click.echo(f"Last sync:       {display_time(state.get('last_sync')) if read_config().get('sync_directory') else 'not configured'}")
    if recovery["enabled"]:
        click.echo(f"Auto recovery:   enabled · {recovery.get('saved_tabs', 0)} tabs / {recovery.get('resumable_tabs', 0)} agent sessions")
        click.echo(f"Workspace saved: {display_time(recovery.get('last_capture'))}")
        click.echo(f"Recovery check:  {display_time(recovery.get('last_check'))}")
        if recovery.get("unknown_directories"):
            click.echo(f"  {recovery['unknown_directories']} shell tabs have no reported directory and cannot yet be restored.")
        if recovery.get("error"):
            click.secho(f"recovery_error: {recovery['error']}", fg="yellow")
    for key in ("capture_error", "sync_error"):
        if state.get(key):
            click.secho(f"{key}: {state[key]}", fg="yellow")
    click.echo()
    for p in details if show_sessions else projects:
        if show_sessions:
            click.secho(f"{p['project']}  [{p['source']}]  {p.get('git_branch') or ''}  {p['session_id']}", bold=True)
            click.echo(f"  {display_time(p.get('last_activity') or p['started_at'])} · {p.get('machine') or 'unknown computer'}")
            click.echo(f"  {p.get('cwd') or 'directory not recorded'}")
        else:
            click.secho(f"{p['project']}  · {p['session_count']} sessions · {', '.join(p['sources'])}", bold=True)
            click.echo(f"  {display_time(p['last_activity'])}")
        click.echo(f"  Task: {p['task']}")
        click.echo(f"  Update: {p['last_update']}")
        click.echo(f"  Next (reported): {p['next_step']}")
        click.echo()
    if not sessions:
        click.echo("No recent sessions. Run llm-archive checkpoint to capture existing history.")


@cli.command()
@click.argument("project")
@click.option("--session", "session_id", help="Only versions of this session")
@click.option("--revision", help="Print a previous version by its key or unique prefix")
@click.option("--output", type=click.Path(path_type=Path), help="Save the selected version as JSON")
def history(project, session_id, revision, output):
    """Inspect preserved versions of rewritten or truncated transcripts."""
    from llm_archive.overview import display_time
    from llm_archive.paths import atomic_write
    conn = db.get_connection()
    try:
        records = db.revisions(conn, project, session_id)
    finally:
        conn.close()
    if revision:
        matches = [r for r in records if r["revision_key"].startswith(revision)]
        if len(matches) != 1:
            raise click.ClickException("Revision not found or prefix is ambiguous.")
        text = json.dumps(json.loads(matches[0]["snapshot"]), indent=2, ensure_ascii=False)
        if output:
            atomic_write(output, text + "\n")
            click.echo(f"Saved preserved version to {output}")
        else:
            click.echo(text)
        return
    if output:
        raise click.ClickException("Use --revision when saving a version.")
    for r in records:
        snapshot = json.loads(r["snapshot"])
        click.echo(f"{r['revision_key'][:12]}  {display_time(r['captured_at'])}  [{r['source']}] {r['session_id']}  {len(snapshot['messages'])} messages")
    if not records:
        click.echo("No previous versions recorded; existing captured versions are preserved when a transcript is rewritten.")


@cli.command()
@click.argument("project")
@click.option("--days", default=30, type=click.IntRange(min=1), show_default=True)
@click.option("--session", "session_id", default=None, help="Limit the handoff to a session ID")
@click.option("--output", type=click.Path(path_type=Path), help="Write a portable Markdown handoff")
def resume(project, days, session_id, output):
    """Print the latest context and continuation commands for PROJECT."""
    from llm_archive.journal import handoff, recent_sessions
    from llm_archive.paths import atomic_write
    conn = db.get_connection()
    try:
        sessions = recent_sessions(conn, days=days, project=project, limit=10000)
        if session_id:
            sessions = [s for s in sessions if s["session_id"] == session_id]
        if not sessions:
            raise click.ClickException("No matching activity. Run checkpoint or increase --days.")
        text = handoff(conn, sessions[:5])
    finally:
        conn.close()
    if output:
        atomic_write(output, text)
        click.echo(f"Saved handoff to {output}")
    else:
        click.echo(text)


@cli.command()
@click.option("--days", default=3, type=click.IntRange(min=1), show_default=True)
@click.option("--project", default=None, help="Only reopen this project")
@click.option("--limit", default=10, type=click.IntRange(min=1), show_default=True, help="Maximum directories to reopen")
@click.option("--resume", "resume_agents", is_flag=True, help="Resume the latest local agent session in each directory")
@click.option("--projects-root", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Find local checkouts under this directory on another computer")
@click.option("--dry-run", is_flag=True, help="Preview tabs and commands without opening Ghostty")
@click.option("--session", "session_id", help="Choose a specific session instead of the latest in its directory")
@click.option("--workspace", "workspace_name", help="Restore a saved workspace")
@click.option("--new-tabs", is_flag=True, help="Open additional tabs even for directories already open in Ghostty")
def reopen(days, project, limit, resume_agents, projects_root, dry_run, session_id, workspace_name, new_tabs):
    """Restore recent project/worktree directories as Ghostty tabs (macOS)."""
    from llm_archive.journal import recent_sessions
    from llm_archive.terminal import open_ghostty, reopen_plan, ghostty_directories
    conn = db.get_connection()
    try:
        if workspace_name:
            from llm_archive.workspaces import load
            sessions = load(workspace_name)["sessions"]
            if project:
                sessions = [s for s in sessions if s["project"] == project or s.get("project_key") == project]
        else:
            sessions = recent_sessions(conn, days=36500 if session_id else days, project=project, limit=10000)
        if session_id:
            sessions = [s for s in sessions if s["session_id"] == session_id]
            if not sessions:
                raise ValueError("Session not found; use llm-archive status --sessions to choose an ID.")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    plan = reopen_plan(sessions, resume=resume_agents, projects_root=projects_root)[:limit]
    for p in plan:
        p["new_tab"] = new_tabs
    if not plan:
        click.echo("No recent project directories. Run checkpoint or increase --days.")
        return
    existing = set()
    if dry_run and not new_tabs and platform.system() == "Darwin":
        try:
            existing = ghostty_directories()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            click.echo(f"Existing tabs could not be inspected: {exc}", err=True)
    for p in plan:
        click.echo(f"{p['project']}: {p['cwd'] or 'skipped'}")
        if p["cwd"] in existing:
            click.echo("  Already open; will be skipped (use --new-tabs to open another).")
        if p["command"]:
            click.echo(f"  {p['command']}")
        if p["note"]:
            click.echo(f"  {p['note']}")
    if dry_run:
        return
    try:
        click.echo(f"Opened {open_ghostty(plan)} Ghostty tabs.")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.group()
def workspace():
    """Save and inspect named project workspaces."""


@workspace.command(name="save")
@click.argument("name")
@click.option("--days", default=3, type=click.IntRange(min=1), show_default=True)
@click.option("--project", "projects", multiple=True, help="Include only these projects (repeatable)")
@click.option("--from-tabs", is_flag=True, help="Save the directories currently open in Ghostty")
def workspace_save(name, days, projects, from_tabs):
    from llm_archive.journal import recent_sessions
    from llm_archive.workspaces import save
    from llm_archive.terminal import ghostty_directories
    conn = db.get_connection()
    try:
        sessions = recent_sessions(conn, days=days, limit=10000)
        if projects:
            sessions = [s for s in sessions if s["project"] in projects or s.get("project_key") in projects]
        directories = ghostty_directories() if from_tabs else None
        if directories is not None and projects:
            directories &= {str(Path(s["cwd"]).resolve()) for s in sessions if s.get("cwd")}
        record = save(name, sessions, directories=directories)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Saved {len(record['sessions'])} directories as {name}. Restore with: llm-archive reopen --workspace {name}")


@workspace.command(name="list")
def workspace_list():
    from llm_archive.workspaces import list_workspaces
    from llm_archive.overview import display_time
    records = list_workspaces()
    for record in records:
        click.echo(f"{record['selector']}  {len(record['sessions'])} directories  {display_time(record['saved_at'])}")
    if not records:
        click.echo("No saved workspaces. Use: llm-archive workspace save NAME")


@cli.command(name="sync-init")
@click.argument("remote")
def sync_init(remote):
    """Connect a dedicated private Git repository for journal data."""
    from llm_archive.sync import configure
    try:
        root = configure(remote)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Journal sync configured: {root}\nRun llm-archive sync to exchange records now; checkpoints will sync automatically.")


@cli.command()
def sync():
    """Exchange journal records with the configured private Git remote."""
    from llm_archive.sync import sync_if_configured
    from llm_archive.paths import read_config
    if not read_config().get("sync_directory"):
        raise click.ClickException("Sync is not configured. Use: llm-archive sync-init <private-git-url>")
    try:
        count = sync_if_configured()
    except (RuntimeError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Synced journal; imported {count} session updates.")


@cli.command()
@click.option("--sync-history", is_flag=True,
              help="Also replace the private journal's Git history with one commit and force-push it.")
def compact(sync_history):
    """Drop preserved versions that only repeat the current conversation, and shrink storage."""
    from llm_archive.paths import collector_lock, read_config
    with collector_lock():
        conn = db.get_connection()
        try:
            pruned = db.prune_redundant_revisions(conn)
            conn.execute("VACUUM")
        finally:
            conn.close()
    click.echo(f"Removed {pruned} preserved versions that repeated the current conversation.")
    if sync_history:
        from llm_archive.sync import compact_history
        directory = read_config().get("sync_directory")
        if not directory:
            raise click.ClickException("Sync is not configured. Use: llm-archive sync-init <private-git-url>")
        try:
            compact_history(Path(directory))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("Replaced the journal history with one commit and pushed it.")


@cli.group()
def automation():
    """Enable hands-off capture, private sync, and Ghostty workspace recovery."""


@automation.command(name="enable")
def automation_enable():
    """Install capture/sync plus automatic live-tab saving and agent recovery (macOS)."""
    from llm_archive.recovery import enable
    try:
        for message in enable():
            click.echo(message)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc


@automation.command(name="disable")
def automation_disable():
    """Stop automatic tab saving/recovery; keep background capture and sync."""
    from llm_archive.recovery import disable
    try:
        click.echo(disable())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc


@automation.command(name="status")
def automation_status():
    """Show workspace recovery health without opening Ghostty."""
    from llm_archive.paths import read_config
    from llm_archive.recovery import read_state
    click.echo(json.dumps({"enabled": bool(read_config().get("automatic_recovery")), **read_state()}, indent=2))


@automation.command(name="recover")
@click.option("--dry-run", is_flag=True, help="Preview recovery without opening tabs")
def automation_recover(dry_run):
    """Retry the automatic workspace after a recovery error, skipping running agents."""
    from llm_archive.recovery import recover
    try:
        plan = recover(dry_run=dry_run)
        click.echo(json.dumps(plan, indent=2))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="recovery-watch", hidden=True)
def recovery_watch():
    """Internal foreground observer managed by launchd."""
    from llm_archive.recovery import run
    try:
        run()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        pass


cli.add_command(install_cron, "install-service")
cli.add_command(uninstall_cron, "uninstall-service")
