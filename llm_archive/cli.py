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
    """Estimate API cost from message sizes."""
    conn = db.get_connection()
    rows = db.message_costs(conn, days=days)
    conn.close()

    if not rows:
        click.echo("No data in this period.")
        return

    # Pricing per million tokens (approximate blended rates)
    PRICING = {
        "claude": {"input": 3.0, "output": 15.0},
        "codex": {"input": 2.50, "output": 10.0},
    }

    by_source = defaultdict(lambda: {"input_chars": 0, "output_chars": 0})
    by_project = defaultdict(lambda: {"input_chars": 0, "output_chars": 0})
    by_day = defaultdict(lambda: {"input_chars": 0, "output_chars": 0})

    for r in rows:
        chars = r["chars"] or 0
        key = "input_chars" if r["role"] == "user" else "output_chars"
        by_source[r["source"]][key] += chars
        by_project[r["project"]][key] += chars
        by_day[r["day"]][key] += chars

    def estimate_cost(source, input_chars, output_chars):
        pricing = PRICING.get(source, PRICING["claude"])
        input_tokens = input_chars / 4
        output_tokens = output_chars / 4
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    total_cost = 0.0

    click.secho("By source:", bold=True)
    for source in sorted(by_source):
        d = by_source[source]
        c = estimate_cost(source, d["input_chars"], d["output_chars"])
        total_cost += c  # don't double count - we'll recalculate total
        input_tok = d["input_chars"] // 4
        output_tok = d["output_chars"] // 4
        click.echo(f"  {source:<10s}  ~{input_tok:>10,} input tok  ~{output_tok:>10,} output tok  ~${c:,.0f}")

    # Recalculate total properly
    total_cost = 0.0
    for source, d in by_source.items():
        total_cost += estimate_cost(source, d["input_chars"], d["output_chars"])

    click.echo()
    click.secho("By project (top 15):", bold=True)
    project_costs = []
    for project, d in by_project.items():
        # Use blended source pricing - approximate with claude rates
        c = estimate_cost("claude", d["input_chars"], d["output_chars"])
        project_costs.append((project, c))
    project_costs.sort(key=lambda x: x[1], reverse=True)
    for project, c in project_costs[:15]:
        if c < 0.5:
            continue
        click.echo(f"  {project:<30s}  ~${c:,.0f}")

    click.echo()
    click.echo(f"Estimated total: ~${total_cost:,.0f} over {days} days")
    click.secho("(Approximate: assumes chars/4 ≈ tokens, blended model pricing)", fg="yellow")


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
