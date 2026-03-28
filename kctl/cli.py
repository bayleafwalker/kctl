import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import db as _db
from . import extract as _extract
from . import publish as _publish
from . import review as _review


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_tags(tags_json: str | None) -> str:
    if not tags_json:
        return ""
    try:
        return ", ".join(json.loads(tags_json))
    except (json.JSONDecodeError, TypeError):
        return tags_json or ""


def _print_candidate(c: dict) -> None:
    tags = _format_tags(c.get("tags"))
    click.echo(f"  #{c['id']:>4}  [{c['status']:>9}]  {c['event_type']:20}  {c['summary']}")
    if tags:
        click.echo(f"           tags: {tags}")


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)
    db_path = _db.get_db_path()
    conn = _db.get_connection(db_path)
    _db.init_db(conn)
    ctx.obj["conn"] = conn
    ctx.obj["db_path"] = db_path
    ctx.call_on_close(conn.close)


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@cli.command("extract")
@click.option("--sprint-id", type=int, default=None, help="Scope extraction to one sprint")
@click.option("--full", is_flag=True, default=False, help="Re-scan all events (still skips existing candidates)")
@click.option(
    "--event-types",
    default=None,
    help="Comma-separated event types to extract (default: decision,blocker-resolved,pattern-noted,risk-accepted,lesson-learned)",
)
@click.option(
    "--sprintctl-db",
    default=None,
    help="Path to sprintctl DB (default: SPRINTCTL_DB env var or ~/.sprintctl/sprintctl.db)",
)
@click.option("--no-preflight", is_flag=True, default=False, help="Skip sprintctl maintain check")
@click.pass_obj
def extract_cmd(obj, sprint_id, full, event_types, sprintctl_db, no_preflight) -> None:
    """Extract knowledge candidates from sprintctl events."""
    kctl_conn = obj["conn"]
    now = _now()

    sc_db_path = Path(sprintctl_db) if sprintctl_db else _extract.get_sprintctl_db_path()
    if not sc_db_path.exists():
        click.echo(f"Error: sprintctl DB not found at {sc_db_path}", err=True)
        sys.exit(1)

    try:
        sc_conn = _db.get_sprintctl_connection(sc_db_path)
    except Exception as exc:
        click.echo(f"Error: could not open sprintctl DB: {exc}", err=True)
        sys.exit(1)

    try:
        _db.validate_sprintctl_schema(sc_conn)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not no_preflight:
        warnings = _extract.run_preflight(sc_conn)
        for w in warnings:
            click.echo(f"Warning: {w}", err=True)

    event_type_set = (
        set(event_types.split(",")) if event_types
        else _extract.DEFAULT_EVENT_TYPES
    )

    state = _db.get_extractor_state(kctl_conn, str(sc_db_path))
    since_event_id = 0 if full or state is None else state["last_event_id"]

    created, structured_count = _extract.extract_candidates(
        sprintctl_conn=sc_conn,
        kctl_conn=kctl_conn,
        sprintctl_db_path=str(sc_db_path),
        event_types=event_type_set,
        since_event_id=since_event_id,
        sprint_id=sprint_id,
        now=now,
    )
    sc_conn.close()

    total = len(created)
    bare_count = total - structured_count
    if total == 0:
        click.echo("Extracted 0 new candidates.")
    elif bare_count == 0:
        click.echo(f"Extracted {total} candidate(s) ({structured_count} with structured payloads).")
    else:
        click.echo(
            f"Extracted {total} candidate(s) "
            f"({structured_count} with structured payloads, {bare_count} from bare event)."
        )
    pending = len(_db.list_candidates(kctl_conn, status="candidate"))
    click.echo(f"{pending} candidate(s) awaiting review.")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@cli.group("review")
def review_group() -> None:
    """Review knowledge candidates."""


@review_group.command("list")
@click.option(
    "--status",
    default="candidate",
    type=click.Choice(["candidate", "approved", "rejected", "published", "all"]),
    help="Filter by status (default: candidate)",
)
@click.option("--tag", default=None, help="Filter by tag")
@click.option("--sprint-id", type=int, default=None, help="Filter by source sprint ID")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def review_list(obj, status, tag, sprint_id, output_json) -> None:
    """List candidates."""
    conn = obj["conn"]
    effective_status = None if status == "all" else status
    candidates = _db.list_candidates(conn, status=effective_status, tag=tag, sprint_id=sprint_id)

    if output_json:
        rows = [
            {
                "id": c["id"],
                "status": c["status"],
                "event_type": c["event_type"],
                "summary": c["summary"],
                "tags": json.loads(c.get("tags") or "[]"),
                "source_sprint_id": c["source_sprint_id"],
                "source_track": c.get("source_track"),
                "confidence": c.get("confidence"),
            }
            for c in candidates
        ]
        click.echo(json.dumps(rows))
        return

    if not candidates:
        click.echo("No candidates found.")
        return

    click.echo(f"{'ID':>6}  {'Status':>9}  {'Event type':20}  Summary")
    click.echo("-" * 80)
    for c in candidates:
        _print_candidate(c)


@review_group.command("show")
@click.option("--id", "candidate_id", type=int, required=True, help="Candidate ID")
@click.pass_obj
def review_show(obj, candidate_id) -> None:
    """Show a candidate in detail."""
    conn = obj["conn"]
    c = _db.get_candidate(conn, candidate_id)
    if c is None:
        click.echo(f"Candidate #{candidate_id} not found.", err=True)
        sys.exit(1)

    click.echo(f"Candidate #{c['id']}")
    click.echo(f"  Status:      {c['status']}")
    click.echo(f"  Event type:  {c['event_type']}")
    click.echo(f"  Track:       {c.get('source_track') or '(none)'}")
    click.echo(f"  Item ID:     {c['source_item_id'] or '(none)'}")
    click.echo(f"  Summary:     {c['summary']}")
    click.echo(f"  Detail:      {c['detail'] or '(none)'}")
    click.echo(f"  Tags:        {_format_tags(c.get('tags')) or '(none)'}")
    click.echo(f"  Confidence:  {c['confidence'] or '(none)'}")
    click.echo(f"  Sprint:      {c['source_sprint_id']} (container ref)")
    click.echo(f"  Extracted:   {c['extracted_at']}")
    if c.get("reviewed_at"):
        click.echo(f"  Reviewed:    {c['reviewed_at']} by {c['reviewed_by']}")
    if c.get("review_notes"):
        click.echo(f"  Notes:       {c['review_notes']}")


@review_group.command("approve")
@click.option("--id", "candidate_id", type=int, required=True, help="Candidate ID")
@click.option("--title", default=None, help="Override summary/title")
@click.option("--tags", default=None, help='Override tags as JSON array, e.g. \'["auth","lessons"]\'')
@click.option("--reviewer", default="human", help="Reviewer identifier")
@click.pass_obj
def review_approve(obj, candidate_id, title, tags, reviewer) -> None:
    """Approve a candidate."""
    conn = obj["conn"]
    try:
        updated = _review.approve_candidate(
            conn, candidate_id=candidate_id, now=_now(),
            reviewed_by=reviewer, title=title, tags=tags,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Candidate #{candidate_id} approved.")
    click.echo(f"  Summary: {updated['summary']}")


@review_group.command("reject")
@click.option("--id", "candidate_id", type=int, required=True, help="Candidate ID")
@click.option("--reason", default=None, help="Reason for rejection")
@click.option("--reviewer", default="human", help="Reviewer identifier")
@click.pass_obj
def review_reject(obj, candidate_id, reason, reviewer) -> None:
    """Reject a candidate."""
    conn = obj["conn"]
    try:
        _review.reject_candidate(
            conn, candidate_id=candidate_id, now=_now(),
            reviewed_by=reviewer, reason=reason,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Candidate #{candidate_id} rejected.")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

@cli.command("publish")
@click.option("--id", "candidate_id", type=int, required=True, help="Candidate ID to publish")
@click.option("--title", default=None, help="Entry title (defaults to candidate summary)")
@click.option("--body", required=True, help="Full knowledge body / detail text")
@click.option(
    "--category",
    required=True,
    type=click.Choice(["decision", "pattern", "lesson", "risk", "reference"]),
    help="Knowledge category",
)
@click.option("--tags", default=None, help='Tags as JSON array, e.g. \'["auth","lessons"]\'')
@click.pass_obj
def publish_cmd(obj, candidate_id, title, body, category, tags) -> None:
    """Promote an approved candidate to a knowledge entry."""
    conn = obj["conn"]
    try:
        entry = _publish.publish_candidate(
            conn,
            candidate_id=candidate_id,
            title=title,
            body=body,
            category=category,
            tags=tags,
            now=_now(),
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Published entry #{entry['id']}: {entry['title']}")
    click.echo(f"  Category: {entry['category']}")
    tags_str = _format_tags(entry.get("tags"))
    if tags_str:
        click.echo(f"  Tags: {tags_str}")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@cli.command("render")
@click.option(
    "--category",
    default=None,
    type=click.Choice(["decision", "pattern", "lesson", "risk", "reference"]),
    help="Filter by category",
)
@click.option("--tag", default=None, help="Filter by tag")
@click.option("--sprint-id", type=int, default=None, help="Filter by source sprint ID")
@click.option("--output", default=None, help="Write to FILE instead of stdout")
@click.pass_obj
def render_cmd(obj, category, tag, sprint_id, output) -> None:
    """Render published knowledge entries to structured markdown."""
    conn = obj["conn"]
    entries = _db.list_entries(conn, category=category, tag=tag, sprint_id=sprint_id)

    project = os.environ.get("KCTL_PROJECT", "homelab-analytics")
    lines = [
        f"# Knowledge Base — {project}",
        f"Generated: {_now()}",
        "",
    ]

    if not entries:
        lines.append("_No published entries found._")
    else:
        # Group by category
        by_category: dict[str, list[dict]] = {}
        for e in entries:
            by_category.setdefault(e["category"], []).append(e)

        category_order = ["decision", "pattern", "lesson", "risk", "reference"]
        for cat in category_order:
            if cat not in by_category:
                continue
            lines.append(f"## {cat.capitalize()}s")
            lines.append("")
            for e in by_category[cat]:
                lines.append(f"### {e['title']}")
                source_parts = []
                if e.get("source_track"):
                    source_parts.append(f"track: {e['source_track']}")
                source_parts.append(f"sprint: {e['source_sprint']}")
                lines.append(f"Source: {', '.join(source_parts)}")
                tags_str = _format_tags(e.get("tags"))
                if tags_str:
                    lines.append(f"Tags: {tags_str}")
                lines.append("")
                lines.append(e["body"])
                lines.append("")
                lines.append("---")
                lines.append("")

    content = "\n".join(lines)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        click.echo(f"Wrote {len(entries)} entry/entries to {output}")
    else:
        click.echo(content)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command("status")
@click.option("--sprint-id", type=int, default=None, help="Filter to one sprint")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def status_cmd(obj, sprint_id, output_json) -> None:
    """Show pipeline state: candidates awaiting review, approved, published."""
    conn = obj["conn"]

    pending = _db.list_candidates(conn, status="candidate", sprint_id=sprint_id)
    approved = _db.list_candidates(conn, status="approved", sprint_id=sprint_id)
    published = _db.list_candidates(conn, status="published", sprint_id=sprint_id)

    if output_json:
        payload = {
            "sprint_id": sprint_id,
            "counts": {
                "candidate": len(pending),
                "approved": len(approved),
                "published": len(published),
            },
            "approved": [
                {
                    "id": c["id"],
                    "summary": c["summary"],
                    "event_type": c["event_type"],
                    "source_track": c.get("source_track"),
                    "source_sprint_id": c["source_sprint_id"],
                }
                for c in approved
            ],
        }
        click.echo(json.dumps(payload))
        return

    scope = f" (sprint {sprint_id})" if sprint_id else ""
    click.echo(f"Pipeline status{scope}:")
    click.echo(f"  {len(pending):>4}  awaiting review  (candidate)")
    click.echo(f"  {len(approved):>4}  approved, pending publish")
    click.echo(f"  {len(published):>4}  published")

    if approved:
        click.echo("")
        click.echo("Approved (ready to publish):")
        for c in approved:
            click.echo(f"  #{c['id']:>4}  {c['summary']}")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

@cli.command("preflight")
@click.option(
    "--sprintctl-db",
    default=None,
    help="Path to sprintctl DB",
)
@click.pass_obj
def preflight_cmd(obj, sprintctl_db) -> None:
    """Run sprintctl maintain check and report results."""
    sc_db_path = Path(sprintctl_db) if sprintctl_db else _extract.get_sprintctl_db_path()
    if not sc_db_path.exists():
        click.echo(f"Error: sprintctl DB not found at {sc_db_path}", err=True)
        sys.exit(1)

    try:
        sc_conn = _db.get_sprintctl_connection(sc_db_path)
        _db.validate_sprintctl_schema(sc_conn)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    warnings = _extract.run_preflight(sc_conn)
    sc_conn.close()

    if warnings:
        for w in warnings:
            click.echo(f"Warning: {w}")
        sys.exit(1)
    else:
        click.echo("Preflight OK.")
