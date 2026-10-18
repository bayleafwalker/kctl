"""Read-only coverage of an explicitly frozen, standalone knowledge database."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any

import click


_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_ROWS = 100_000
_SCHEMA_VERSION = 9
_COLUMNS = {
    "schema_version": "version",
    "knowledge_candidate": "id source_event_id source_sprint_id source_item_id source_track source_actor source_type source_created_at source_payload source_origin event_type candidate_kind summary detail tags confidence status extracted_at reviewed_at reviewed_by review_notes",
    "knowledge_entry": "id candidate_id title body tags category source_sprint source_track created_at superseded_by source_kind",
    "extractor_state": "id sprintctl_db_path last_event_id last_run_at",
    "extractor_state_v2": "id sprintctl_db_path scope_key last_event_id last_run_at",
    "sqlite_sequence": "name seq",
}
_INTEGER_COLUMNS = {"id", "version", "source_event_id", "source_sprint_id", "source_item_id", "candidate_id", "superseded_by", "last_event_id"}


class SourcePreflightError(ValueError):
    """The supplied bytes cannot be qualified by this source-owner contract."""


def _snapshot_bytes(path: Path) -> bytes:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise SourcePreflightError("platform lacks safe regular file opens")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourcePreflightError("snapshot must be a regular file")
        if metadata.st_size > _MAX_SNAPSHOT_BYTES:
            raise SourcePreflightError("snapshot exceeds the 64 MiB byte bound")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > _MAX_SNAPSHOT_BYTES:
            raise SourcePreflightError("snapshot exceeds the 64 MiB byte bound")
        return raw
    except OSError as exc:
        raise SourcePreflightError("snapshot is unavailable or not a safe regular file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cell(value: Any) -> list[Any]:
    # Type tags retain SQLite distinctions and never parse/normalize stored JSON.
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        # Hex also represents historical infinities without non-standard JSON.
        return ["real", value.hex() if math.isfinite(value) else str(value)]
    raise SourcePreflightError("unsupported SQLite value type")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _schema(conn: sqlite3.Connection) -> str:
    objects = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name").fetchall()
    tables = {row[1] for row in objects if row[0] == "table"}
    if tables != _COLUMNS.keys() or any(row[0] not in {"table", "index"} for row in objects):
        raise SourcePreflightError("unsupported snapshot schema objects")
    for table, names in _COLUMNS.items():
        columns = conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        if {row[1] for row in columns} != set(names.split()) or any(row[6] for row in columns):
            raise SourcePreflightError("unsupported snapshot columns")
        for row in columns:
            expected = "INTEGER" if row[1] in _INTEGER_COLUMNS else "TEXT"
            if table == "sqlite_sequence":
                expected = ""
            if row[2].upper() != expected or row[5] != int(row[1] == "id"):
                raise SourcePreflightError("unsupported snapshot column types or identity")
        definition = next(row[3] for row in objects if row[0] == "table" and row[1] == table)
        if not definition.upper().startswith("CREATE TABLE"):
            raise SourcePreflightError("snapshot tables must be ordinary SQLite tables")
    if conn.execute("SELECT version FROM schema_version").fetchall() != [(_SCHEMA_VERSION,)]:
        raise SourcePreflightError("snapshot must have knowledge schema version 9")
    return _digest({"domain": "kctl-source-schema/v1", "objects": [[_cell(value) for value in row] for row in objects]})


def _report(conn: sqlite3.Connection, raw_sha256: str, byte_count: int) -> dict[str, Any]:
    schema_digest = _schema(conn)
    if conn.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
        raise SourcePreflightError("snapshot failed SQLite integrity checking")
    counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in _COLUMNS}
    if sum(counts.values()) > _MAX_ROWS:
        raise SourcePreflightError("snapshot exceeds the 100,000 row bound")
    records = []
    candidates: dict[int, dict[str, Any]] = {}
    entries: dict[int, dict[str, Any]] = {}
    origins_without_sprintctl_identity = 0
    for table, names in sorted(_COLUMNS.items()):
        columns = sorted(names.split())
        select = ",".join(f'"{name}"' for name in columns)
        ordering = '"id"' if "id" in columns else ('"name"' if table == "sqlite_sequence" else '"version"')
        identities: set[int] = set()
        for ordinal, row in enumerate(conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {ordering}'), 1):
            records.append({"table": table, "row": ordinal, "row_sha256": _digest({"domain": "kctl-source-row/v1", "table": table, "columns": columns, "values": [_cell(value) for value in row]})})
            value = dict(zip(columns, row, strict=True))
            if "id" in value:
                identity = value["id"]
                if type(identity) is not int or identity in identities:
                    raise SourcePreflightError("snapshot must have unique integer row identity")
                identities.add(identity)
            if table == "knowledge_candidate":
                candidates[value["id"]] = {key: value[key] for key in ("status", "candidate_kind")}
                if value["source_event_id"] is None or value["source_sprint_id"] is None:
                    origins_without_sprintctl_identity += 1
            elif table == "knowledge_entry":
                entries[value["id"]] = {key: value[key] for key in ("candidate_id", "superseded_by", "source_kind")}
    findings: list[dict[str, Any]] = []

    def finding(code: str, table: str, identity: int) -> None:
        findings.append({"code": code, "table": table, "id": identity})

    publications: dict[int, int] = {}
    for entry_id, entry in entries.items():
        candidate = candidates.get(entry["candidate_id"])
        if candidate is None:
            finding("missing-candidate", "knowledge_entry", entry_id)
        else:
            publications[entry["candidate_id"]] = publications.get(entry["candidate_id"], 0) + 1
            if entry["source_kind"] != candidate["candidate_kind"]:
                finding("stream-mismatch", "knowledge_entry", entry_id)
        if entry["superseded_by"] is not None and entry["superseded_by"] not in entries:
            finding("missing-supersession-target", "knowledge_entry", entry_id)
    for candidate_id, candidate in candidates.items():
        count = publications.get(candidate_id, 0)
        if count > 1:
            finding("duplicate-publications", "knowledge_candidate", candidate_id)
        if candidate["status"] == "published" and count == 0:
            finding("missing-publication", "knowledge_candidate", candidate_id)
        if candidate["status"] != "published" and count:
            finding("partial-publication", "knowledge_candidate", candidate_id)
    # Each node is visited once, even for long supersession histories.
    done: set[int] = set()
    for entry_id in entries:
        path: set[int] = set()
        cursor = entry_id
        while cursor in entries and cursor not in done:
            if cursor in path:
                finding("supersession-cycle", "knowledge_entry", cursor)
                break
            path.add(cursor)
            cursor = entries[cursor]["superseded_by"]
        done.update(path)
    return {
        "schema_version": "kctl-source-preflight/v1",
        "qualification": "source-coverage-only",
        "s4_authority_established": False,
        "cutover_authorized": False,
        "live_tail_coverage_established": False,
        "snapshot_sha256": raw_sha256,
        "snapshot_byte_count": byte_count,
        "knowledge_schema_version": _SCHEMA_VERSION,
        "schema_sha256": schema_digest,
        "schema_digest_domain": "kctl-source-schema/v1",
        "row_digest_domain": "kctl-source-row/v1",
        "row_count": sum(counts.values()),
        "table_counts": counts,
        "candidates_without_sprintctl_identity": origins_without_sprintctl_identity,
        "records": records,
        "publication_consistency": "inconsistent" if findings else "consistent",
        "findings": findings,
    }


def preflight(snapshot: Path, expected_sha256: str) -> dict[str, Any]:
    """Qualify exactly these standalone source bytes; never open KCTL_DB."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SourcePreflightError("expected SHA-256 must be lowercase hex")
    raw = _snapshot_bytes(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise SourcePreflightError("snapshot differs from its frozen SHA-256")
    if not raw.startswith(b"SQLite format 3\0") or len(raw) < 100:
        raise SourcePreflightError("snapshot must be a SQLite database")
    if raw[18:20] != b"\x01\x01":
        raise SourcePreflightError("prepare a standalone DELETE-journal snapshot; a WAL main file may omit committed records")
    try:
        with closing(sqlite3.connect(":memory:")) as conn:
            if not hasattr(conn, "deserialize"):
                raise SourcePreflightError("SQLite snapshot deserialization is unavailable")
            conn.deserialize(raw)
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA query_only=ON")
            return _report(conn, digest, len(raw))
    except (sqlite3.Error, UnicodeError) as exc:
        raise SourcePreflightError("invalid frozen knowledge snapshot") from exc


@click.command()
@click.option("--snapshot", type=click.Path(path_type=Path), required=True)
@click.option("--sha256", "expected_sha256", required=True)
def main(snapshot: Path, expected_sha256: str) -> None:
    """Report coverage of an explicit frozen SQLite source without importing it."""
    try:
        report = preflight(snapshot, expected_sha256)
    except SourcePreflightError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
