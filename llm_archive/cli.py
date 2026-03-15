import os
import platform
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import click

from llm_archive import db
from llm_archive.ingest import run_ingest

MAX_GAP = timedelta(minutes=30)


def _check_scheduler():
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

    db_path = Path.home() / ".local" / "share" / "llm-archive" / "archive.db"
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
def cli():
    """Ingest, search, and analyze LLM conversations."""
    _check_scheduler()


@cli.command()
def ingest():
    """Ingest conversations from Claude Code and Codex CLI."""
    new_convs, new_msgs = run_ingest()
    click.echo(f"Ingested {new_convs} conversations ({new_msgs} messages)")


@cli.command()
@click.argument("query")
@click.option("--project", default=None, help="Filter by project name")
@click.option("--source", type=click.Choice(["claude", "codex"]), default=None)
@click.option("--limit", default=20, help="Max results")
def search(query, project, source, limit):
    """Full-text search across conversations."""
    conn = db.get_connection()
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
    """Generate a summary of recent conversations using Claude API."""
    conn = db.get_connection()
    text = db.summarize_text(conn, days=days)
    conn.close()

    if not text:
        click.echo("No conversations in this period.")
        return

    click.echo(f"Summarizing last {days} days...")
    from llm_archive.summarize import weekly_summary
    summary = weekly_summary(text, days=days)
    click.echo()
    click.echo(summary)

    # Save to markdown file
    summaries_dir = Path.home() / ".local" / "share" / "llm-archive" / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = summaries_dir / f"{today}.md"
    md_path.write_text(f"# LLM Summary — {today} (last {days} days)\n\n{summary}\n")
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
def install_cron():
    """Set up daily automatic ingestion (launchd/systemd/crontab)."""
    from llm_archive.scheduler import install
    result = install()
    click.echo(result)


@cli.command(name="uninstall-cron")
def uninstall_cron():
    """Remove daily automatic ingestion."""
    from llm_archive.scheduler import uninstall
    result = uninstall()
    click.echo(result)


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
