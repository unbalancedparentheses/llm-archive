import click

from llm_archive import db
from llm_archive.ingest import run_ingest


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
