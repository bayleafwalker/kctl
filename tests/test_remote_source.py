import json
import sys
import types
from datetime import datetime, timezone

import pytest

from kctl import db as _db
from kctl import extract as _extract
from kctl import source as _source


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.conn.calls.append((query, params))

    def fetchall(self):
        return self.conn.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def test_remote_source_extracts_tenant_scoped_rows_read_only(tmp_path):
    conn = _Connection(
        [
            {
                "id": 41,
                "sprint_id": 9,
                "work_item_id": 7,
                "source_type": "actor",
                "actor": "codex",
                "event_type": "decision",
                "payload": {"summary": "Use the remote source", "tags": ["remote"]},
                "created_at": datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
                "item_title": "Remote extraction",
                "track_name": "remote-sprintctl",
            }
        ]
    )
    source = _source.RemoteSprintctlSource(conn=conn, repo_id="source-repo")
    kctl_conn = _db.get_connection(tmp_path / "kctl.db")
    _db.init_db(kctl_conn)

    created, structured = _extract.extract_candidates(
        source,
        kctl_conn,
        source.source_id,
        {"decision"},
        0,
        None,
        "2026-07-17T12:01:00Z",
    )

    assert structured == 1
    assert created[0]["source_created_at"] == "2026-07-17T12:00:00Z"
    assert _db.list_candidates(kctl_conn, status="candidate")[0]["summary"] == "Use the remote source"
    query, params = conn.calls[0]
    assert "e.repo_id = %s" in query
    assert params == ["source-repo", 0, ["decision"]]
    assert all(call[0].lstrip().upper().startswith("SELECT") for call in conn.calls)


def test_remote_source_requires_url(monkeypatch):
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)

    with pytest.raises(_source.SprintctlSourceError, match="requires SPRINTCTL_URL"):
        _source.open_sprintctl_source(remote_repo_id="source-repo")


def test_served_source_paginates_then_filters_with_durable_event_id_watermark(monkeypatch):
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )
    first_page = [
        {
            "id": event_id,
            "event_type": "decision",
            "payload": {"summary": f"event-{event_id}"},
            "created_at": "2026-07-26T12:00:00Z",
        }
        for event_id in range(1, 101)
    ]
    second_page = [
        {
            "id": 101,
            "event_type": "pattern-noted",
            "payload": {"summary": "new"},
            "created_at": "2026-07-26T12:01:00Z",
        },
        {
            "id": 102,
            "event_type": "ignored",
            "payload": {"summary": "skip"},
            "created_at": "2026-07-26T12:02:00Z",
        },
    ]
    calls = []

    def invoke(_operation, arguments):
        calls.append(arguments)
        return {"repo_id": "source-repo", "events": first_page if arguments["after_offset"] == 0 else second_page}

    monkeypatch.setattr(source, "_invoke", invoke)

    events = source.fetch_events(
        since_event_id=99,
        event_types={"decision", "pattern-noted"},
        sprint_id=7,
    )

    assert [event["id"] for event in events] == [100, 101]
    assert events[0]["payload"] == '{"summary": "event-100"}'
    assert calls == [
        {"sprint_id": 7, "work_item_id": None, "after_offset": 0, "limit": 100},
        {"sprint_id": 7, "work_item_id": None, "after_offset": 100, "limit": 100},
    ]


def test_served_source_requires_sprint_scope():
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )

    with pytest.raises(_source.SprintctlSourceError, match="requires --sprint-id"):
        source.fetch_events(since_event_id=0, event_types={"decision"}, sprint_id=None)


def test_served_source_invokes_vuoro_client_with_repository_scope(monkeypatch):
    invocations = []

    class _Profile:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Client:
        def __init__(self, profile, credential_resolver):
            self.profile = profile
            self.credential_resolver = credential_resolver

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def invoke(self, operation, arguments, **kwargs):
            invocations.append((operation, arguments, kwargs, self.profile))
            return {"repo_id": "source-repo", "events": []}

    monkeypatch.setitem(sys.modules, "vuoro_client", types.SimpleNamespace(AsyncVuoroClient=_Client, Profile=_Profile))
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )

    result = source._invoke("work.read.events", {"sprint_id": 7})

    assert result == {"repo_id": "source-repo", "events": []}
    assert invocations[0][:3] == (
        "work.read.events",
        {"sprint_id": 7},
        {"repo_id": "source-repo"},
    )
    assert invocations[0][3].expected_environment == "vuoro-dev"


def test_open_served_source_resolves_vuoro_profile(monkeypatch, tmp_path):
    profile = tmp_path / "vuoro-profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": "vuoro-client-profile/v1",
                "id": "vuoro-dev",
                "target": {"endpoint": "https://vuoro.example/", "environment_id": "vuoro-dev"},
                "credential_ref": "file:/tmp/vuoro-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)

    source = _source.open_sprintctl_source(remote_repo_id="source-repo")

    assert isinstance(source, _source.ServedSprintctlSource)
    assert source.source_id == "served://source-repo"
    assert source.profile.endpoint == "https://vuoro.example/"


def test_served_source_lists_preflight_targets_through_read_sprints(monkeypatch):
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )
    calls = []

    def invoke(operation, arguments):
        calls.append((operation, arguments))
        return {
            "repo_id": "source-repo",
            "sprints": [
                {"id": 7, "name": "Current", "status": "active"},
                {"id": 8, "name": "Closed", "status": "closed"},
            ],
        }

    monkeypatch.setattr(source, "_invoke", invoke)

    assert source.list_preflight_targets(7) == [{"id": 7, "name": "Current", "status": "active"}]
    assert calls == [
        (
            "work.read.sprints",
            {"include_backlog": True, "include_archive": True, "active_only": False},
        )
    ]


def test_served_preflight_fails_closed_without_composing_an_unserved_diagnostic(monkeypatch):
    source = _source.ServedSprintctlSource(
        profile=_source.ServedProfile("vuoro-dev", "https://vuoro.example/", "file:/token", "vuoro-dev"),
        repo_id="source-repo",
    )
    def unexpected_target_lookup(_sprint_id):
        pytest.fail("served preflight must not compose maintain.check from sprint reads")

    monkeypatch.setattr(source, "list_preflight_targets", unexpected_target_lookup)

    warnings = _extract.run_preflight_for_source(source, sprint_id=7)

    assert warnings == [
        _extract.SERVED_PREFLIGHT_UNAVAILABLE
    ]
