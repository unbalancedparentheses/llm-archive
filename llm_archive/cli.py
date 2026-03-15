from collections import defaultdict
from datetime import datetime, timedelta

import click

from llm_archive import db
from llm_archive.ingest import run_ingest

MAX_GAP = timedelta(minutes=30)


@click.group()
def cli():
    """Ingest, search, and analyze LLM conversations."""


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
