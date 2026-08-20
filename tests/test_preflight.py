import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

from kctl import db as _db
from kctl import extract as _extract
from kctl import source as _source
from kctl.cli import cli


def _load_sprintctl():
    try:
        return import_module("sprintctl.db")
    except ImportError:
        sibling = Path(__file__).resolve().parents[2] / "sprintctl"
        if sibling.exists():
            sys.path.insert(0, str(sibling))
            return import_module("sprintctl.db")
    pytest.skip("sprintctl source not available for integration test")


def test_run_preflight_via_cli_fallback(monkeypatch, sc_db_path):
    conn = _db.get_sprintctl_connection(sc_db_path)

    def _raise_import_error(_conn, _sprint_id):
        raise ImportError("no sprintctl module")

    class _Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "sprint": {"id": 1, "name": "Sprint 1"},
                "stale_items": [{"id": 1, "status": "active"}],
                "threshold_hours": 4.0,
                "pending_threshold_hours": None,
            }
        )
        stderr = ""

    monkeypatch.setattr(_extract, "_run_preflight_via_import", _raise_import_error)
    monkeypatch.setattr(_extract.subprocess, "run", lambda *args, **kwargs: _Proc())

    warnings = _extract.run_preflight(
        conn,
        sprintctl_db_path=sc_db_path,
    )
    conn.close()

    assert warnings == ["Sprint 'Sprint 1' has 1 stale item(s) (1 active > 4 hours)"]


def test_run_preflight_respects_pending_stale_threshold_with_real_sprintctl(monkeypatch, tmp_path):
    sdb = _load_sprintctl()

    sc_db_path = tmp_path / "sprintctl.db"
    conn = sdb.get_connection(sc_db_path)
    sdb.init_db(conn)
    sprint_id = sdb.create_sprint(conn, "Sprint A", status="active")
    track_id = sdb.get_or_create_track(conn, sprint_id, "backend")
    active_id = sdb.create_work_item(conn, sprint_id, track_id, "Implement auth")
    pending_id = sdb.create_work_item(conn, sprint_id, track_id, "Backlog item")
    conn.execute(
        "UPDATE work_item SET status = 'active', updated_at = '2026-04-01T00:00:00Z' WHERE id = ?",
        (active_id,),
    )
    conn.execute(
        "UPDATE work_item SET status = 'pending', updated_at = '2026-03-20T00:00:00Z' WHERE id = ?",
        (pending_id,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SPRINTCTL_PENDING_STALE_THRESHOLD", "24")
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    warnings = _extract.run_preflight(
        sc_conn,
        sprint_id=sprint_id,
        sprintctl_db_path=sc_db_path,
    )
    sc_conn.close()

    assert warnings == [
        "Sprint 'Sprint A' has 2 stale item(s) (1 active > 4 hours, 1 pending > 24 hours)"
    ]


def test_extract_candidates_with_real_sprintctl_payloads(tmp_path):
    sdb = _load_sprintctl()

    sc_db_path = tmp_path / "sprintctl.db"
    conn = sdb.get_connection(sc_db_path)
    sdb.init_db(conn)
    sprint_id = sdb.create_sprint(conn, "Sprint A", status="active")
    track_id = sdb.get_or_create_track(conn, sprint_id, "backend")
    item_id = sdb.create_work_item(conn, sprint_id, track_id, "Implement auth")
    sdb.create_event(
        conn,
        sprint_id,
        actor="agent-1",
        event_type="decision",
        work_item_id=item_id,
        payload={
            "summary": "Use RS256",
            "detail": "Avoid shared secret coordination",
            "tags": ["auth", "architecture"],
            "git_branch": "feat/auth",
            "git_sha": "abc123",
        },
    )
    first_reservation = sdb.reserve(
        conn,
        item_id,
        actor="agent-1",
        session_id="sess-1",
        role="execution",
    )
    overlapping_reservation = sdb.reserve(
        conn,
        item_id,
        actor="agent-2",
        session_id="sess-2",
        role="execution",
    )
    assert first_reservation["conflict"] is False
    assert overlapping_reservation["conflict"] is True
    assert overlapping_reservation["conflict_severity"] == "warning"
    assert [
        reservation["actor"]
        for reservation in overlapping_reservation["conflicting_reservations"]
    ] == ["agent-1"]
    assert {
        reservation["actor"]
        for reservation in sdb.list_reservations(conn, item_id)
    } == {"agent-1", "agent-2"}
    conn.close()

    kctl_conn = _db.get_connection(tmp_path / "kctl.db")
    _db.init_db(kctl_conn)
    sc_conn = _db.get_sprintctl_connection(sc_db_path)
    created, structured_count = _extract.extract_candidates(
        sc_conn,
        kctl_conn,
        str(sc_db_path),
        _extract.DEFAULT_EVENT_TYPES,
        0,
        None,
        "2026-04-02T00:00:00Z",
    )
    sc_conn.close()
    kctl_conn.close()

    # Routine advisory-reservation churn remains visible in Sprintctl but is
    # not promoted to durable knowledge by kctl's default extraction policy.
    assert structured_count == 1
    by_type = {row["event_type"]: row for row in created}
    assert by_type["decision"]["candidate_kind"] == "durable"
    assert json.loads(by_type["decision"]["source_payload"])["git_branch"] == "feat/auth"
    assert "reservation.reserved" not in by_type


def test_cli_preflight_json_ok(monkeypatch, sc_db_path, runner):
    monkeypatch.setattr(_extract, "run_preflight", lambda *args, **kwargs: [])

    result = runner.invoke(
        cli,
        ["preflight", "--sprintctl-db", str(sc_db_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["warnings"] == []
    assert payload["sprint_id"] is None


def test_cli_preflight_json_warnings_are_structured(monkeypatch, sc_db_path, runner):
    warning = "Sprint 'Sprint 1' has 1 stale item(s) (1 active > 4 hours)"
    monkeypatch.setattr(_extract, "run_preflight", lambda *args, **kwargs: [warning])

    result = runner.invoke(
        cli,
        ["preflight", "--sprintctl-db", str(sc_db_path), "--json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["warnings"] == [warning]
    assert payload["sprint_id"] is None


def test_cli_preflight_json_missing_db_reports_error(runner, tmp_path):
    missing = tmp_path / "missing-sprintctl.db"
    result = runner.invoke(
        cli,
        ["preflight", "--sprintctl-db", str(missing), "--json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["warnings"] == []
    assert "not found" in payload["error"]


def test_cli_preflight_json_runtime_failure_is_error(monkeypatch, sc_db_path, runner):
    monkeypatch.setattr(
        _extract,
        "run_preflight",
        lambda *args, **kwargs: ["Preflight check failed: sprintctl maintain unavailable"],
    )

    result = runner.invoke(
        cli,
        ["preflight", "--sprintctl-db", str(sc_db_path), "--json"],
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["warnings"] == []
    assert "Preflight check failed" in payload["error"]


def test_cli_served_preflight_uses_owning_maintain_contract(monkeypatch, runner):
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )
    monkeypatch.setattr(_source, "open_sprintctl_source", lambda **_kwargs: source)
    monkeypatch.setattr(
        source,
        "maintain_check",
        lambda sprint_id: {
            "repo_id": "source-repo",
            "sprint": {"id": sprint_id, "name": "Current"},
            "stale_items": [],
            "threshold_hours": 4,
            "pending_threshold_hours": None,
        },
    )

    result = runner.invoke(cli, ["preflight", "--sprint-id", "7", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "ok": True,
        "sprint_id": 7,
        "warnings": [],
        "error": None,
    }


def test_cli_served_preflight_never_opens_local_knowledge_store(monkeypatch, runner):
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setattr(
        _db,
        "get_connection",
        lambda *_args, **_kwargs: pytest.fail("served preflight opened Kctl SQLite"),
    )
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile(
            "vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"
        ),
        repo_id="source-repo",
    )
    monkeypatch.setattr(_source, "open_sprintctl_source", lambda **_kwargs: source)
    monkeypatch.setattr(
        source,
        "maintain_check",
        lambda sprint_id: {
            "repo_id": "source-repo",
            "sprint": {"id": sprint_id, "name": "Current"},
            "stale_items": [],
            "threshold_hours": 4,
            "pending_threshold_hours": None,
        },
    )

    result = runner.invoke(cli, ["preflight", "--sprint-id", "7", "--json"])

    assert result.exit_code == 0, result.output


def test_cli_served_doctor_probes_maintain_contract_without_sqlite(monkeypatch, runner):
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setattr(
        _db,
        "get_connection",
        lambda *_args, **_kwargs: pytest.fail("served doctor opened Kctl SQLite"),
    )
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile(
            "vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"
        ),
        repo_id="source-repo",
    )
    monkeypatch.setattr(_source, "open_sprintctl_source", lambda **_kwargs: source)
    calls = []

    def maintain_check(sprint_id):
        calls.append(sprint_id)
        return {
            "repo_id": "source-repo",
            "sprint": {"id": sprint_id, "name": "Current"},
            "stale_items": [],
            "threshold_hours": 4,
            "pending_threshold_hours": None,
        }

    monkeypatch.setattr(source, "maintain_check", maintain_check)

    result = runner.invoke(cli, ["doctor", "--sprint-id", "7", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [7]
    assert json.loads(result.output) == {
        "ok": True,
        "mode": "served",
        "source_id": "served://source-repo",
        "maintain_check": {
            "available": True,
            "sprint_id": 7,
            "warnings": [],
            "error": None,
        },
    }


@pytest.mark.parametrize("command", ["publish", "render", "export", "export-proposal"])
def test_cli_served_unavailable_commands_fail_before_sqlite(monkeypatch, runner, command):
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setattr(
        _db,
        "get_connection",
        lambda *_args, **_kwargs: pytest.fail("unavailable served command opened Kctl SQLite"),
    )

    result = runner.invoke(cli, [command])

    assert result.exit_code != 0
    assert "served-operation-unavailable" in result.output
    assert "will not fall back to the local knowledge store" in result.output


def test_cli_served_extract_preflight_failure_is_an_honest_served_error(monkeypatch, kctl_conn, runner):
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )
    monkeypatch.setattr(_source, "open_sprintctl_source", lambda **_kwargs: source)
    monkeypatch.setattr(source, "maintain_check", lambda _sprint_id: (_ for _ in ()).throw(RuntimeError("catalog unavailable")))

    result = runner.invoke(cli, ["extract", "--sprint-id", "7", "--basis-git-revision", "a" * 40])

    assert result.exit_code != 0
    assert "Preflight check failed: catalog unavailable" in result.output
