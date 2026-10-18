from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from click.testing import CliRunner
import pytest

from kctl import db, publish, review, source_preflight as source


NOW = "2026-10-18T12:00:00Z"


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _candidate(conn, **overrides):
    value = {"summary": "private café 東京", "event_type": "model.claim", "source_origin": "metanarrative", "extracted_at": NOW,
             "source_payload": '{ "private": "authored spacing" }'}
    value.update(overrides)
    return db.insert_candidate(conn, value)


def _freeze(conn, tmp_path):
    path = tmp_path / "private-frozen.sqlite"
    with closing(sqlite3.connect(path)) as snapshot:
        conn.backup(snapshot)
        snapshot.execute("PRAGMA journal_mode=DELETE")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _populate(conn):
    for category in ("tenet", "direction"):
        identity = _candidate(conn)
        review.approve_candidate(conn, identity, now=NOW, reviewed_by="private-reviewer")
        publish.publish_candidate(conn, identity, title="private-title", body="private-body", category=category, tags=None, now=NOW)
    _candidate(conn, source_event_id=7, source_sprint_id=3, source_origin="sprintctl", candidate_kind="coordination")
    conn.execute("INSERT INTO extractor_state(sprintctl_db_path,last_event_id,last_run_at) VALUES ('/private/source',7,?)", (NOW,))
    conn.execute("INSERT INTO extractor_state_v2(sprintctl_db_path,scope_key,last_event_id,last_run_at) VALUES ('/private/source','scope:v1',7,?)", (NOW,))
    conn.commit()


def test_covers_all_rows_and_newer_local_shapes_without_leaking_content(tmp_path):
    with _database() as conn:
        _populate(conn)
        path, digest = _freeze(conn, tmp_path)
        expected = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in source._COLUMNS}
    before = {file: file.read_bytes() for file in tmp_path.iterdir()}
    result = source.preflight(path, digest)
    assert result["table_counts"] == expected
    assert result["row_count"] == len(result["records"]) == sum(expected.values())
    assert result["candidates_without_sprintctl_identity"] == 2
    assert result["publication_consistency"] == "consistent"
    assert result["qualification"] == "source-coverage-only"
    assert result["s4_authority_established"] is result["cutover_authorized"] is result["live_tail_coverage_established"] is False
    assert result == source.preflight(path, digest)
    output = json.dumps(result)
    for private in ("private", "metanarrative", "café", "tenet", "direction"):
        assert private not in output
    assert {file: file.read_bytes() for file in tmp_path.iterdir()} == before


def test_row_digests_bind_original_stored_text_and_all_provenance(tmp_path):
    with _database() as conn:
        identity = _candidate(conn)
        path, digest = _freeze(conn, tmp_path)
        original = source.preflight(path, digest)
        conn.execute("UPDATE knowledge_candidate SET source_payload = ?, reviewed_by = ? WHERE id = ?", ('{"private":"authored spacing"}', "different-reviewer", identity))
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
        changed = source.preflight(path, digest)
    before = [row for row in original["records"] if row["table"] == "knowledge_candidate"]
    after = [row for row in changed["records"] if row["table"] == "knowledge_candidate"]
    assert before != after
    assert original["snapshot_sha256"] != changed["snapshot_sha256"]


def test_partial_duplicate_missing_and_cyclic_publications_remain_covered(tmp_path):
    with _database() as conn:
        _populate(conn)
        conn.execute("UPDATE knowledge_candidate SET status='approved' WHERE id=1")
        conn.execute("INSERT INTO knowledge_entry(candidate_id,title,body,category,source_sprint,created_at) VALUES (1,'secret','secret','tenet','None',?)", (NOW,))
        conn.execute("UPDATE knowledge_candidate SET status='published' WHERE id=3")
        conn.execute("UPDATE knowledge_entry SET superseded_by=2 WHERE id=1")
        conn.execute("UPDATE knowledge_entry SET superseded_by=1 WHERE id=2")
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
    result = source.preflight(path, digest)
    assert result["publication_consistency"] == "inconsistent"
    assert {finding["code"] for finding in result["findings"]} >= {"partial-publication", "duplicate-publications", "missing-publication", "supersession-cycle"}
    assert result["table_counts"]["knowledge_entry"] == 3
    assert len([row for row in result["records"] if row["table"] == "knowledge_entry"]) == 3


@pytest.mark.parametrize("change", ["CREATE TABLE unexpected(secret TEXT)", "CREATE VIEW unexpected AS SELECT * FROM knowledge_candidate", "ALTER TABLE knowledge_candidate ADD COLUMN new_source TEXT", "UPDATE schema_version SET version=10"])
def test_unknown_schema_is_refused(tmp_path, change):
    with _database() as conn:
        conn.execute(change)
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
    with pytest.raises(source.SourcePreflightError, match="schema|columns"):
        source.preflight(path, digest)


def test_wal_snapshot_refused_without_discovering_or_modifying_sidecars(tmp_path):
    path = tmp_path / "wal.sqlite"
    with sqlite3.connect(path) as conn:
        db.init_db(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        _candidate(conn)
        raw = path.read_bytes()
        before = {file: file.read_bytes() for file in tmp_path.iterdir()}
        with pytest.raises(source.SourcePreflightError, match="standalone DELETE-journal"):
            source.preflight(path, hashlib.sha256(raw).hexdigest())
        assert {file: file.read_bytes() for file in tmp_path.iterdir()} == before


def test_cli_uses_only_explicit_snapshot_including_in_served_environment(tmp_path, monkeypatch):
    with _database() as conn:
        path, digest = _freeze(conn, tmp_path)
    missing_db = tmp_path / "must-not-create.sqlite"
    monkeypatch.setenv("KCTL_DB", str(missing_db))
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    result = CliRunner().invoke(source.main, ["--snapshot", str(path), "--sha256", digest])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["snapshot_sha256"] == digest
    assert not missing_db.exists()
    wrong = CliRunner().invoke(source.main, ["--snapshot", str(path), "--sha256", "0" * 64])
    assert wrong.exit_code != 0 and "frozen SHA-256" in wrong.output
    assert wrong.stdout == ""


def test_safe_regular_file_and_byte_and_row_bounds(tmp_path, monkeypatch):
    with _database() as conn:
        path, digest = _freeze(conn, tmp_path)
    link = tmp_path / "link.sqlite"
    link.symlink_to(path)
    with pytest.raises(source.SourcePreflightError, match="regular file"):
        source.preflight(link, digest)
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    with pytest.raises(source.SourcePreflightError, match="regular file"):
        source.preflight(fifo, digest)
    monkeypatch.setattr(source, "_MAX_SNAPSHOT_BYTES", path.stat().st_size - 1)
    with pytest.raises(source.SourcePreflightError, match="byte bound"):
        source.preflight(path, digest)
    monkeypatch.setattr(source, "_MAX_SNAPSHOT_BYTES", path.stat().st_size)
    result = source.preflight(path, digest)
    monkeypatch.setattr(source, "_MAX_ROWS", result["row_count"])
    assert source.preflight(path, digest) == result
    monkeypatch.setattr(source, "_MAX_ROWS", result["row_count"] - 1)
    with pytest.raises(source.SourcePreflightError, match="row bound"):
        source.preflight(path, digest)


def test_malformed_digest_or_database_refused(tmp_path):
    path = tmp_path / "bad.sqlite"
    path.write_bytes(b"not SQLite")
    with pytest.raises(source.SourcePreflightError, match="lowercase"):
        source.preflight(path, "INVALID")
    with pytest.raises(source.SourcePreflightError, match="SQLite database"):
        source.preflight(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize("field,value", [("source_origin", "external"), ("reviewed_by", "new reviewer"), ("review_notes", "authored note"), ("source_payload", b"authored blob")])
def test_source_digests_preserve_provenance_and_sqlite_types(tmp_path, field, value):
    with _database() as conn:
        _candidate(conn)
        path, digest = _freeze(conn, tmp_path)
        before = source.preflight(path, digest)
        conn.execute(f"UPDATE knowledge_candidate SET {field} = ?", (value,))
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
        after = source.preflight(path, digest)
    prior = [row for row in before["records"] if row["table"] == "knowledge_candidate"]
    current = [row for row in after["records"] if row["table"] == "knowledge_candidate"]
    assert prior != current


def test_missing_references_and_stream_mismatch_are_reported_not_removed(tmp_path):
    with _database() as conn:
        _populate(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE knowledge_entry SET candidate_id=999,superseded_by=888 WHERE id=1")
        conn.execute("UPDATE knowledge_entry SET source_kind='coordination' WHERE id=2")
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
    result = source.preflight(path, digest)
    assert {finding["code"] for finding in result["findings"]} >= {"missing-candidate", "missing-supersession-target", "stream-mismatch", "missing-publication"}
    assert result["table_counts"]["knowledge_entry"] == 2


def test_non_integer_primary_key_identities_are_refused_before_diagnostics(tmp_path):
    import re

    with _database() as conn:
        definition = conn.execute("SELECT sql FROM sqlite_schema WHERE name='knowledge_candidate'").fetchone()[0]
        definition = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "INTEGER PRIMARY KEY DESC", definition, count=1)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE knowledge_candidate")
        conn.execute(definition)
        for status in ("approved", "published"):
            conn.execute("INSERT INTO knowledge_candidate(summary,event_type,extracted_at,status) VALUES ('secret','model.claim',?,?)", (NOW, status))
        conn.commit()
        path, digest = _freeze(conn, tmp_path)
    with pytest.raises(source.SourcePreflightError, match="integer row identity"):
        source.preflight(path, digest)


def test_byte_bound_still_applies_if_source_grows_after_stat(tmp_path, monkeypatch):
    from types import SimpleNamespace

    with _database() as conn:
        path, digest = _freeze(conn, tmp_path)
    original = os.fstat

    def stale_size(descriptor):
        metadata = original(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=0)

    monkeypatch.setattr(source.os, "fstat", stale_size)
    monkeypatch.setattr(source, "_MAX_SNAPSHOT_BYTES", 16)
    with pytest.raises(source.SourcePreflightError, match="byte bound"):
        source.preflight(path, digest)


def test_real_module_entrypoint_never_bootstraps_configured_store(tmp_path, monkeypatch):
    import subprocess
    import sys

    with _database() as conn:
        path, digest = _freeze(conn, tmp_path)
    missing = tmp_path / "unused" / "default.sqlite"
    monkeypatch.setenv("KCTL_DB", str(missing))
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    result = subprocess.run([sys.executable, "-m", "kctl.source_preflight", "--snapshot", str(path), "--sha256", digest], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["snapshot_sha256"] == digest
    assert not missing.parent.exists()
