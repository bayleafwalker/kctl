import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import artifact as _artifact
from . import db as _db
from . import extract as _extract
from . import proposal as _proposal
from . import publish as _publish
from . import review as _review
from . import source as _source
from .served_knowledge import ServedKnowledgeClient


SERVED_COMMAND_DISPOSITIONS = {
    "doctor": "source",
    "extract": "knowledge",
    "preflight": "source",
    "review": "knowledge",
    "status": "knowledge",
    "export": "unavailable",
    "export-proposal": "unavailable",
    "publish": "unavailable",
    "render": "unavailable",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_default_project() -> str:
    """Best-effort project name when KCTL_PROJECT isn't set: the name of the
    enclosing git repo's directory, or "unknown" outside any repo.

    Dependency-free (no sprintctl import) so this stays correct even when
    kctl is used standalone, mirroring how local-mode backend resolution
    never requires the sprintctl package either.
    """
    current = Path.cwd()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory.name
    return "unknown"


def _format_tags(tags_json: str | None) -> str:
    if not tags_json:
        return ""
    try:
        return ", ".join(json.loads(tags_json))
    except (json.JSONDecodeError, TypeError):
        return tags_json or ""


def _decode_json_field(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _payload_dict(raw: str | None) -> dict:
    payload = _decode_json_field(raw)
    return payload if isinstance(payload, dict) else {}


def _extract_provenance(raw: str | None) -> dict:
    payload = _payload_dict(raw)
    keys = (
        "evidence_item_id",
        "evidence_event_id",
        "git_branch",
        "git_sha",
        "git_worktree",
    )
    return {key: payload[key] for key in keys if payload.get(key) is not None}


def _extract_coordination_context(raw: str | None) -> dict:
    payload = _payload_dict(raw)
    keys = (
        "operation",
        "mode",
        "reason",
        "legacy_adopted",
        "token_rotated",
        "from_identity",
        "to_identity",
        "claim",
        "attempted_by",
    )
    return {key: payload[key] for key in keys if payload.get(key) is not None}


def _candidate_json(c: dict) -> dict:
    return {
        "id": c["id"],
        "status": c["status"],
        "candidate_kind": c.get("candidate_kind", "durable"),
        "event_type": c["event_type"],
        "summary": c["summary"],
        "detail": c.get("detail"),
        "tags": json.loads(c.get("tags") or "[]"),
        "source_sprint_id": c["source_sprint_id"],
        "source_track": c.get("source_track"),
        "source_item_id": c.get("source_item_id"),
        "source_actor": c.get("source_actor"),
        "source_type": c.get("source_type"),
        "source_created_at": c.get("source_created_at"),
        "source_payload": _decode_json_field(c.get("source_payload")),
        "provenance": _extract_provenance(c.get("source_payload")),
        "coordination_context": _extract_coordination_context(c.get("source_payload")),
        "confidence": c.get("confidence"),
        "extracted_at": c.get("extracted_at"),
        "reviewed_at": c.get("reviewed_at"),
        "reviewed_by": c.get("reviewed_by"),
        "review_notes": c.get("review_notes"),
    }


def _print_candidate(c: dict) -> None:
    tags = _format_tags(c.get("tags"))
    click.echo(
        f"  #{c['id']:>4}  [{c['status']:>9}]  "
        f"[{c.get('candidate_kind', 'durable'):>12}]  {c['event_type']:20}  {c['summary']}"
    )
    if tags:
        click.echo(f"           tags: {tags}")


def _entry_json(e: dict) -> dict:
    source_sprint = e.get("source_sprint")
    source_sprint_id = source_sprint
    try:
        source_sprint_id = int(source_sprint) if source_sprint is not None else None
    except (TypeError, ValueError):
        source_sprint_id = source_sprint

    return {
        "id": e["id"],
        "candidate_id": e["candidate_id"],
        "title": e["title"],
        "body": e["body"],
        "category": e["category"],
        "tags": json.loads(e.get("tags") or "[]"),
        "source_sprint": source_sprint,
        "source_sprint_id": source_sprint_id,
        "source_track": e.get("source_track"),
        "source_kind": e.get("source_kind", "durable"),
        "created_at": e["created_at"],
        "superseded_by": e.get("superseded_by"),
    }


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)
    mode = os.environ.get("SPRINTCTL_BACKEND", "local").lower()
    if mode == "served":
        disposition = SERVED_COMMAND_DISPOSITIONS[ctx.invoked_subcommand]
        if disposition == "unavailable":
            raise click.ClickException(
                "served-operation-unavailable: "
                f"'kctl {ctx.invoked_subcommand}' has no served operation and "
                "will not fall back to the local knowledge store"
            )
        # Source-only diagnostics open neither Kctl SQLite nor the central
        # knowledge facade. Their callbacks construct the Sprintctl source.
        if disposition == "source":
            return
        # Served review reads must never bootstrap/open the local knowledge
        # store. Candidate state remains owned by the knowledge catalog.
        try:
            source = _source.open_sprintctl_source()
        except _source.SprintctlSourceError as exc:
            raise click.ClickException(str(exc)) from exc
        if not isinstance(source, _source.ServedSprintctlSource):
            raise click.ClickException("served knowledge review requires a served Sprintctl source")
        ctx.obj["served_knowledge"] = ServedKnowledgeClient(source.profile, source.repo_id)
        ctx.call_on_close(source.close)
        return
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
    help=(
        "Comma-separated event types to extract "
        "(default: KCTL_EVENT_TYPES env var or built-in durable + coordination set)"
    ),
)
@click.option(
    "--sprintctl-db",
    default=None,
    help="Path to a local sprintctl DB (overrides SPRINTCTL_BACKEND)",
)
@click.option(
    "--sprintctl-repo-id",
    default=None,
    help="Remote sprintctl repo ID (default: KCTL_SPRINTCTL_REPO_ID or current repository)",
)
@click.option("--no-preflight", is_flag=True, default=False, help="Skip sprintctl maintain check")
@click.option("--basis-git-revision", default=None, help="Required full 40/64-hex Git commit in served mode")
@click.pass_obj
def extract_cmd(obj, sprint_id, full, event_types, sprintctl_db, sprintctl_repo_id, no_preflight, basis_git_revision) -> None:
    """Extract knowledge candidates from sprintctl events."""
    now = _now()

    try:
        source = _source.open_sprintctl_source(
            sprintctl_db=sprintctl_db,
            remote_repo_id=sprintctl_repo_id,
        )
    except _source.SprintctlSourceError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if isinstance(source, _source.ServedSprintctlSource):
        if not basis_git_revision:
            click.echo("Error: served extraction requires --basis-git-revision <full-commit>", err=True)
            sys.exit(1)
        client = ServedKnowledgeClient(source.profile, source.repo_id)
        try:
            if not no_preflight:
                warnings = _extract.run_preflight_for_source(source, sprint_id=sprint_id)
                failure = next((warning for warning in warnings if warning.startswith("Preflight check failed:")), None)
                if failure:
                    click.echo(f"Warning: {failure}", err=True)
                    sys.exit(1)
            event_type_set = _extract.resolve_event_types(event_types)
            events = source.fetch_events(since_event_id=0, event_types=event_type_set, sprint_id=sprint_id)
            results = [client.intake_candidate(_extract.build_candidate(event, now)[0], basis_git_revision=basis_git_revision) for event in events]
        except (ValueError, _source.SprintctlSourceError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        finally:
            source.close()
        click.echo(f"Extracted {sum(not result.get('replayed', False) for result in results)} new candidate(s) through served intake.")
        return

    kctl_conn = obj["conn"]

    try:
        if not no_preflight:
            warnings = _extract.run_preflight_for_source(
                source,
                sprint_id=sprint_id,
            )
            for w in warnings:
                click.echo(f"Warning: {w}", err=True)
            if any(warning.startswith("Preflight check failed:") for warning in warnings):
                # A diagnostic failure is not a stale-item warning. Continue
                # only when the caller explicitly accepts --no-preflight.
                sys.exit(1)

        try:
            event_type_set = _extract.resolve_event_types(event_types)
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

        scope_key = _extract.build_scope_key(event_type_set, sprint_id)
        state = _db.get_extractor_state(kctl_conn, source.source_id, scope_key=scope_key)
        since_event_id = 0 if full or state is None else state["last_event_id"]

        created, structured_count = _extract.extract_candidates(
            sprintctl_conn=source,
            kctl_conn=kctl_conn,
            sprintctl_db_path=source.source_id,
            event_types=event_type_set,
            since_event_id=since_event_id,
            sprint_id=sprint_id,
            now=now,
        )
    finally:
        source.close()

    total = len(created)
    bare_count = total - structured_count
    durable_count = sum(1 for row in created if row.get("candidate_kind") == "durable")
    coordination_count = total - durable_count
    if total == 0:
        click.echo("Extracted 0 new candidates.")
    else:
        click.echo(
            f"Extracted {total} candidate(s) "
            f"({structured_count} with structured payloads, {bare_count} from bare event, "
            f"{durable_count} durable, {coordination_count} coordination)."
        )
    pending_durable = len(
        _db.list_candidates(kctl_conn, status="candidate", candidate_kind="durable")
    )
    pending_coordination = len(
        _db.list_candidates(kctl_conn, status="candidate", candidate_kind="coordination")
    )
    click.echo(f"{pending_durable} durable candidate(s) awaiting review.")
    if pending_coordination:
        click.echo(f"{pending_coordination} coordination candidate(s) awaiting review.")


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
@click.option(
    "--kind",
    default="durable",
    type=click.Choice(["durable", "coordination", "all"]),
    help="Filter by candidate stream (default: durable)",
)
@click.option("--tag", default=None, help="Filter by tag")
@click.option("--sprint-id", type=int, default=None, help="Filter by source sprint ID")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def review_list(obj, status, kind, tag, sprint_id, output_json) -> None:
    """List candidates."""
    served = obj.get("served_knowledge")
    if served is not None:
        if tag is not None or sprint_id is not None:
            raise click.UsageError("--tag and --sprint-id are not available for served candidate lists")
        result = served.list_candidates(
            status=None if status == "all" else status,
            candidate_kind=None if kind == "all" else kind,
        )
        candidates = result["candidates"]
        if output_json:
            click.echo(json.dumps(candidates))
            return
        if not candidates:
            click.echo("No candidates found.")
            return
        for candidate in candidates:
            click.echo(f"  {candidate['candidate_id']}  [{candidate['status']}]  {candidate['summary']}")
        return
    conn = obj["conn"]
    effective_status = None if status == "all" else status
    effective_kind = None if kind == "all" else kind
    candidates = _db.list_candidates(
        conn,
        status=effective_status,
        tag=tag,
        sprint_id=sprint_id,
        candidate_kind=effective_kind,
    )

    if output_json:
        rows = [_candidate_json(c) for c in candidates]
        click.echo(json.dumps(rows))
        return

    if not candidates:
        click.echo("No candidates found.")
        return

    click.echo(f"{'ID':>6}  {'Status':>9}  {'Kind':>12}  {'Event type':20}  Summary")
    click.echo("-" * 96)
    for c in candidates:
        _print_candidate(c)


@review_group.command("show")
@click.option("--id", "candidate_id", type=str, required=True, help="Candidate ID (UUID in served mode)")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def review_show(obj, candidate_id, output_json) -> None:
    """Show a candidate in detail."""
    served = obj.get("served_knowledge")
    if served is not None:
        result = served.show_candidate(candidate_id)
        candidate = result.get("candidate")
        if candidate is None:
            raise click.ClickException(f"Candidate {candidate_id} not found.")
        if output_json:
            click.echo(json.dumps(result))
            return
        click.echo(json.dumps(result, indent=2))
        return
    conn = obj["conn"]
    try:
        candidate_id = int(candidate_id)
    except ValueError as exc:
        raise click.UsageError("local candidate IDs must be integers") from exc
    c = _db.get_candidate(conn, candidate_id)
    if c is None:
        if output_json:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"Candidate #{candidate_id} not found.",
                    }
                )
            )
        else:
            click.echo(f"Candidate #{candidate_id} not found.", err=True)
        sys.exit(1)

    if output_json:
        click.echo(json.dumps(_candidate_json(c)))
        return

    click.echo(f"Candidate #{c['id']}")
    click.echo(f"  Status:      {c['status']}")
    click.echo(f"  Kind:        {c.get('candidate_kind', 'durable')}")
    click.echo(f"  Event type:  {c['event_type']}")
    click.echo(f"  Source type: {c.get('source_type') or '(none)'}")
    click.echo(f"  Source by:   {c.get('source_actor') or '(none)'}")
    click.echo(f"  Source at:   {c.get('source_created_at') or '(none)'}")
    click.echo(f"  Track:       {c.get('source_track') or '(none)'}")
    click.echo(f"  Item ID:     {c['source_item_id'] or '(none)'}")
    click.echo(f"  Summary:     {c['summary']}")
    click.echo(f"  Detail:      {c['detail'] or '(none)'}")
    click.echo(f"  Tags:        {_format_tags(c.get('tags')) or '(none)'}")
    click.echo(f"  Confidence:  {c['confidence'] or '(none)'}")
    click.echo(f"  Sprint:      {c['source_sprint_id']} (container ref)")
    click.echo(f"  Extracted:   {c['extracted_at']}")
    provenance = _extract_provenance(c.get("source_payload"))
    if provenance:
        click.echo(f"  Provenance:  {json.dumps(provenance)}")
    coordination_context = _extract_coordination_context(c.get("source_payload"))
    if coordination_context:
        click.echo(f"  Coordination:{json.dumps(coordination_context)}")
    if c.get("source_payload"):
        click.echo(f"  Source payload: {json.dumps(_decode_json_field(c['source_payload']))}")
    if c.get("reviewed_at"):
        click.echo(f"  Reviewed:    {c['reviewed_at']} by {c['reviewed_by']}")
    if c.get("review_notes"):
        click.echo(f"  Notes:       {c['review_notes']}")


@review_group.command("approve")
@click.option("--id", "candidate_id", type=str, required=True, help="Candidate ID (UUID in served mode)")
@click.option("--title", default=None, help="Override summary/title")
@click.option("--detail", default=None, help="Override detail/body draft")
@click.option("--tags", default=None, help='Override tags as JSON array, e.g. \'["auth","lessons"]\'')
@click.option("--reviewer", default="human", help="Reviewer identifier")
@click.pass_obj
def review_approve(obj, candidate_id, title, detail, tags, reviewer) -> None:
    """Approve a candidate."""
    served = obj.get("served_knowledge")
    if served is not None:
        if title is not None or detail is not None or tags is not None:
            raise click.UsageError("--title, --detail, and --tags are local-only; served approval accepts the immutable candidate and optional notes")
        shown = served.show_candidate(str(candidate_id))
        candidate = shown.get("candidate")
        if candidate is None:
            raise click.ClickException(f"Candidate {candidate_id} not found.")
        served.review_candidate(str(candidate_id), decision="approve", note=None, basis_revision=candidate["basis_git_revision"])
        click.echo(f"Candidate {candidate_id} approved.")
        return
    conn = obj["conn"]
    try:
        candidate_id = int(candidate_id)
    except ValueError as exc:
        raise click.UsageError("local candidate IDs must be integers") from exc
    try:
        updated = _review.approve_candidate(
            conn, candidate_id=candidate_id, now=_now(),
            reviewed_by=reviewer, title=title, detail=detail, tags=tags,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Candidate #{candidate_id} approved.")
    click.echo(f"  Summary: {updated['summary']}")


@review_group.command("reject")
@click.option("--id", "candidate_id", type=str, required=True, help="Candidate ID (UUID in served mode)")
@click.option("--reason", default=None, help="Reason for rejection")
@click.option("--reviewer", default="human", help="Reviewer identifier")
@click.pass_obj
def review_reject(obj, candidate_id, reason, reviewer) -> None:
    """Reject a candidate."""
    served = obj.get("served_knowledge")
    if served is not None:
        shown = served.show_candidate(str(candidate_id))
        candidate = shown.get("candidate")
        if candidate is None:
            raise click.ClickException(f"Candidate {candidate_id} not found.")
        served.review_candidate(str(candidate_id), decision="reject", note=reason, basis_revision=candidate["basis_git_revision"])
        click.echo(f"Candidate {candidate_id} rejected.")
        return
    conn = obj["conn"]
    try:
        candidate_id = int(candidate_id)
    except ValueError as exc:
        raise click.UsageError("local candidate IDs must be integers") from exc
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
@click.option("--supersedes", "supersedes_entry_id", type=int, default=None, help="Mark an older entry as superseded by this new entry")
@click.option(
    "--category",
    required=True,
    type=click.Choice(["decision", "pattern", "lesson", "risk", "reference"]),
    help="Knowledge category",
)
@click.option("--tags", default=None, help='Tags as JSON array, e.g. \'["auth","lessons"]\'')
@click.option(
    "--coordination",
    is_flag=True,
    default=False,
    help="Allow publishing an approved coordination candidate into the coordination knowledge base",
)
@click.pass_obj
def publish_cmd(obj, candidate_id, title, body, supersedes_entry_id, category, tags, coordination) -> None:
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
            supersedes_entry_id=supersedes_entry_id,
            coordination=coordination,
            now=_now(),
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Published entry #{entry['id']}: {entry['title']}")
    click.echo(f"  Stream: {entry.get('source_kind', 'durable')}")
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
@click.option(
    "--coordination",
    is_flag=True,
    default=False,
    help="Render the coordination knowledge base instead of durable knowledge",
)
@click.option("--tag", default=None, help="Filter by tag")
@click.option("--sprint-id", type=int, default=None, help="Filter by source sprint ID")
@click.option("--output", default=None, help="Write to FILE instead of stdout")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def render_cmd(obj, category, coordination, tag, sprint_id, output, output_json) -> None:
    """Render published knowledge entries to structured markdown."""
    conn = obj["conn"]
    source_kind = "coordination" if coordination else "durable"
    entries = _db.list_entries(
        conn,
        category=category,
        tag=tag,
        sprint_id=sprint_id,
        source_kind=source_kind,
    )

    project = os.environ.get("KCTL_PROJECT") or _resolve_default_project()
    if output_json:
        counts_by_category: dict[str, int] = {}
        for entry in entries:
            counts_by_category[entry["category"]] = counts_by_category.get(entry["category"], 0) + 1
        payload = {
            "project": project,
            "generated_at": _now(),
            "filters": {
                "source_kind": source_kind,
                "category": category,
                "tag": tag,
                "sprint_id": sprint_id,
            },
            "source_kind": source_kind,
            "count": len(entries),
            "counts_by_category": counts_by_category,
            "entries": [_entry_json(entry) for entry in entries],
        }
        content = json.dumps(payload)
        if output:
            out_path = Path(output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            click.echo(f"Wrote {len(entries)} entry/entries to {output}")
        else:
            click.echo(content)
        return

    title = "Knowledge Base Ops" if coordination else "Knowledge Base"
    lines = [
        f"# {title} — {project}",
        f"Generated: {_now()}",
        "",
    ]

    if not entries:
        lines.append(f"_No published {source_kind} entries found._")
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
                if e.get("superseded_by"):
                    lines.append(f"Superseded by: entry #{e['superseded_by']}")
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
# export
# ---------------------------------------------------------------------------

@cli.command("export")
@click.option(
    "--artifacts-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=lambda: os.environ.get("KCTL_ARTIFACTS_ROOT"),
    help="Artifact root (default: KCTL_ARTIFACTS_ROOT)",
)
@click.option(
    "--repo-id",
    default=lambda: os.environ.get("KCTL_PROJECT"),
    help="Repository scope (default: KCTL_PROJECT)",
)
@click.pass_obj
def export_cmd(obj, artifacts_root, repo_id) -> None:
    """Write the complete read-only knowledge-artifact/v1 snapshot."""
    if artifacts_root is None:
        raise click.UsageError("--artifacts-root or KCTL_ARTIFACTS_ROOT is required")
    if not repo_id:
        raise click.UsageError("--repo-id or KCTL_PROJECT is required")
    try:
        output_path, count = _artifact.export_snapshot(
            obj["conn"],
            artifacts_root=artifacts_root,
            repo_id=repo_id,
            rendered_at=_now(),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {count} published entry/entries to {output_path}")


# ---------------------------------------------------------------------------
# export-proposal
# ---------------------------------------------------------------------------

@cli.command("export-proposal")
@click.option(
    "--artifacts-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=lambda: os.environ.get("KCTL_ARTIFACTS_ROOT"),
    help="Artifact root (default: KCTL_ARTIFACTS_ROOT)",
)
@click.option(
    "--repo-id",
    default=lambda: os.environ.get("KCTL_PROJECT"),
    help="Repository scope this kctl store belongs to (default: KCTL_PROJECT)",
)
@click.option(
    "--suggested-owner-repo",
    default=None,
    help="Repo to suggest as the sprintctl item owner for every proposal (default: --repo-id)",
)
@click.pass_obj
def export_proposal_cmd(obj, artifacts_root, repo_id, suggested_owner_repo) -> None:
    """Write a read-only proposal snapshot of approved-but-unpublished knowledge.

    This is a decision-support artifact only: it never creates or mutates a
    sprintctl item and never treats a proposal as accepted state. A human or
    a separately authorized agent must run an explicit sprintctl command to
    act on any proposal it contains.
    """
    if artifacts_root is None:
        raise click.UsageError("--artifacts-root or KCTL_ARTIFACTS_ROOT is required")
    if not repo_id:
        raise click.UsageError("--repo-id or KCTL_PROJECT is required")
    owner = suggested_owner_repo or repo_id
    try:
        output_path, count = _proposal.export_snapshot(
            obj["conn"],
            artifacts_root=artifacts_root,
            repo_id=repo_id,
            suggested_owner_repo=owner,
            rendered_at=_now(),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {count} approved knowledge proposal(s) to {output_path}")
    click.echo(
        "This is a proposal only — run the suggested sprintctl command yourself "
        "to act on it. kctl does not create or modify sprintctl items."
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command("status")
@click.option("--sprint-id", type=int, default=None, help="Filter to one sprint")
@click.option(
    "--kind",
    default="durable",
    type=click.Choice(["durable", "coordination", "all"]),
    help="Filter by candidate stream (default: durable)",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def status_cmd(obj, sprint_id, kind, output_json) -> None:
    """Show pipeline state: candidates awaiting review, approved, published."""
    served = obj.get("served_knowledge")
    if served is not None:
        if sprint_id is not None:
            raise click.UsageError("--sprint-id is not available for served status")
        kinds = [kind] if kind != "all" else ["durable", "coordination"]
        grouped = {
            candidate_kind: {
                status: served.list_candidates(status=status, candidate_kind=candidate_kind)["candidates"]
                for status in ("candidate", "approved", "published")
            }
            for candidate_kind in kinds
        }
        if output_json:
            click.echo(json.dumps(grouped))
        else:
            for candidate_kind, states in grouped.items():
                click.echo(f"{candidate_kind}: {len(states['candidate'])} pending, {len(states['approved'])} approved, {len(states['published'])} published")
        return
    conn = obj["conn"]

    def _counts_for(candidate_kind: str | None) -> dict:
        pending = _db.list_candidates(
            conn,
            status="candidate",
            sprint_id=sprint_id,
            candidate_kind=candidate_kind,
        )
        approved = _db.list_candidates(
            conn,
            status="approved",
            sprint_id=sprint_id,
            candidate_kind=candidate_kind,
        )
        published = _db.list_candidates(
            conn,
            status="published",
            sprint_id=sprint_id,
            candidate_kind=candidate_kind,
        )
        return {
            "pending": pending,
            "approved": approved,
            "published": published,
        }

    grouped = {
        "durable": _counts_for("durable"),
        "coordination": _counts_for("coordination"),
    }
    selected = (
        grouped[kind]
        if kind in grouped
        else {
            "pending": grouped["durable"]["pending"] + grouped["coordination"]["pending"],
            "approved": grouped["durable"]["approved"] + grouped["coordination"]["approved"],
            "published": grouped["durable"]["published"] + grouped["coordination"]["published"],
        }
    )

    if output_json:
        payload = {"sprint_id": sprint_id, "kind": kind}
        if kind == "all":
            payload["counts_by_kind"] = {
                candidate_kind: {
                    "candidate": len(rows["pending"]),
                    "approved": len(rows["approved"]),
                    "published": len(rows["published"]),
                }
                for candidate_kind, rows in grouped.items()
            }
        payload["counts"] = {
            "candidate": len(selected["pending"]),
            "approved": len(selected["approved"]),
            "published": len(selected["published"]),
        }
        payload["approved"] = [
            {
                "id": c["id"],
                "candidate_kind": c.get("candidate_kind", "durable"),
                "summary": c["summary"],
                "event_type": c["event_type"],
                "source_track": c.get("source_track"),
                "source_sprint_id": c["source_sprint_id"],
            }
            for c in selected["approved"]
        ]
        click.echo(json.dumps(payload))
        return

    scope = f" (sprint {sprint_id})" if sprint_id else ""
    click.echo(f"Pipeline status{scope}:")
    if kind == "all":
        for candidate_kind in ("durable", "coordination"):
            rows = grouped[candidate_kind]
            click.echo(f"  {candidate_kind.capitalize()}:")
            click.echo(f"    {len(rows['pending']):>4}  awaiting review  (candidate)")
            click.echo(f"    {len(rows['approved']):>4}  approved, pending publish")
            click.echo(f"    {len(rows['published']):>4}  published")
    else:
        click.echo(f"  Stream: {kind}")
        click.echo(f"  {len(selected['pending']):>4}  awaiting review  (candidate)")
        click.echo(f"  {len(selected['approved']):>4}  approved, pending publish")
        click.echo(f"  {len(selected['published']):>4}  published")

    if selected["approved"]:
        click.echo("")
        click.echo("Approved (ready to publish):")
        for c in selected["approved"]:
            click.echo(
                f"  #{c['id']:>4}  [{c.get('candidate_kind', 'durable')}]  {c['summary']}"
            )


# ---------------------------------------------------------------------------
# doctor and preflight
# ---------------------------------------------------------------------------

@cli.command("doctor")
@click.option("--sprintctl-repo-id", default=None, help="Served Sprintctl repository ID")
@click.option("--sprint-id", type=int, default=None, help="Scope maintain.check to one sprint")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def doctor_cmd(sprintctl_repo_id, sprint_id, output_json) -> None:
    """Check source selection and the owning Sprintctl maintenance contract."""
    mode = os.environ.get("SPRINTCTL_BACKEND", "local").lower()
    try:
        source = _source.open_sprintctl_source(remote_repo_id=sprintctl_repo_id)
    except _source.SprintctlSourceError as exc:
        payload = {"ok": False, "mode": mode, "source_id": None, "error": str(exc)}
        if output_json:
            click.echo(json.dumps(payload))
        else:
            click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1)

    try:
        warnings = _extract.run_preflight_for_source(source, sprint_id=sprint_id)
        failure = next(
            (warning for warning in warnings if warning.startswith("Preflight check failed:")),
            None,
        )
        payload = {
            "ok": not warnings,
            "mode": mode,
            "source_id": source.source_id,
            "maintain_check": {
                "available": failure is None,
                "sprint_id": sprint_id,
                "warnings": [] if failure else warnings,
                "error": failure,
            },
        }
    finally:
        source.close()

    if output_json:
        click.echo(json.dumps(payload))
    elif payload["ok"]:
        click.echo(f"Doctor OK ({mode}, {payload['source_id']}).")
    else:
        click.echo(f"Error: {failure or warnings[0]}", err=True)
    if not payload["ok"]:
        raise click.exceptions.Exit(1)


@cli.command("preflight")
@click.option(
    "--sprintctl-db",
    default=None,
    help="Path to a local sprintctl DB (overrides SPRINTCTL_BACKEND)",
)
@click.option("--sprintctl-repo-id", default=None, help="Remote sprintctl repo ID")
@click.option("--sprint-id", type=int, default=None, help="Scope check to one sprint")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON (for agent consumption)")
@click.pass_obj
def preflight_cmd(obj, sprintctl_db, sprintctl_repo_id, sprint_id, output_json) -> None:
    """Run sprintctl maintain check and report results."""
    try:
        source = _source.open_sprintctl_source(
            sprintctl_db=sprintctl_db,
            remote_repo_id=sprintctl_repo_id,
        )
    except _source.SprintctlSourceError as exc:
        if output_json:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "sprint_id": sprint_id,
                        "warnings": [],
                        "error": str(exc),
                    }
                )
            )
        else:
            click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        warnings = _extract.run_preflight_for_source(source, sprint_id=sprint_id)
    finally:
        source.close()
    preflight_failure = next(
        (warning for warning in warnings if warning.startswith("Preflight check failed:")),
        None,
    )

    if output_json:
        click.echo(
            json.dumps(
                {
                    "ok": not warnings and preflight_failure is None,
                    "sprint_id": sprint_id,
                    "warnings": [] if preflight_failure else warnings,
                    "error": preflight_failure,
                }
            )
        )
        if warnings:
            sys.exit(1)
        return

    if warnings:
        for w in warnings:
            click.echo(f"Warning: {w}")
        sys.exit(1)
    else:
        click.echo("Preflight OK.")


assert set(cli.commands) == set(SERVED_COMMAND_DISPOSITIONS), (
    "SERVED_COMMAND_DISPOSITIONS must classify every top-level Kctl command; "
    f"unclassified={sorted(set(cli.commands) - set(SERVED_COMMAND_DISPOSITIONS))}, "
    f"stale={sorted(set(SERVED_COMMAND_DISPOSITIONS) - set(cli.commands))}"
)
