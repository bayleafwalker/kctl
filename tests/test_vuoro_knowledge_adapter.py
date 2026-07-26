from __future__ import annotations

from dataclasses import dataclass
import subprocess
import sys
from typing import Any

import pytest

from kctl.application import CentralKnowledgeApplication, MAX_READ_LIMIT
from kctl.vuoro import (
    INTAKE_AUTHORITY,
    PUBLISH_AUTHORITY,
    READ_AUTHORITY,
    REVIEW_AUTHORITY,
    SCHEMA_DIALECT,
    VuoroKnowledgeAdapter,
    catalog_operation_specs,
)


@dataclass(frozen=True)
class _Definition:
    values: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.values["name"])


class _Registry:
    def __init__(self) -> None:
        self.operations: list[tuple[_Definition, Any]] = []

    def register(self, definition: _Definition, handler: Any) -> None:
        self.operations.append((definition, handler))


def _definition(**values: Any) -> _Definition:
    return _Definition(values)


def test_catalog_is_domain_owned_strict_and_excludes_document_authority() -> None:
    specs = catalog_operation_specs()
    names = [spec["name"] for spec in specs]

    assert names == [
        "knowledge.candidate.intake",
        "knowledge.candidate.list",
        "knowledge.candidate.show",
        "knowledge.candidate.approve",
        "knowledge.candidate.reject",
        "knowledge.publication-reference.record",
        "knowledge.publication-reference.supersede",
        "knowledge.publication-reference.list",
        "knowledge.publication-reference.show",
        "knowledge.schema.compatibility",
    ]
    assert len(names) == len(set(names))
    for spec in specs:
        assert spec["owning_domain"] == "knowledge"
        assert spec["name"].startswith("knowledge.")
        assert spec["input_schema"]["$schema"] == SCHEMA_DIALECT
        assert spec["result_schema"]["$schema"] == SCHEMA_DIALECT
        assert spec["input_schema"]["additionalProperties"] is False
        assert spec["result_schema"]["additionalProperties"] is False
        assert spec["required_client_schema_features"] == ["json-schema-draft-2020-12"]
        if spec["execution_semantics"] != "read":
            assert spec["idempotency"] == "required"
    by_name = {spec["name"]: spec for spec in specs}
    assert (
        by_name["knowledge.candidate.intake"]["required_authority"] == INTAKE_AUTHORITY
    )
    assert (
        by_name["knowledge.candidate.approve"]["required_authority"] == REVIEW_AUTHORITY
    )
    assert by_name["knowledge.candidate.list"]["required_authority"] == READ_AUTHORITY
    publication = by_name["knowledge.publication-reference.record"]
    assert publication["required_authority"] == PUBLISH_AUTHORITY
    publication_properties = publication["input_schema"]["properties"]["publication"][
        "properties"
    ]
    assert {"title", "body", "document_body", "ratified"}.isdisjoint(
        publication_properties
    )
    assert publication_properties["git_revision"]["pattern"]
    assert publication_properties["content_digest"]["pattern"]
    assert "inline_supersedes" not in publication_properties
    assert "supersedes_publication_id" in publication_properties
    publication_result = publication["result_schema"]["properties"]["publication"]
    assert "inline_supersedes" in publication_result["properties"]
    assert not any("ratif" in name or "migrat" in name for name in names)
    assert (
        by_name["knowledge.candidate.list"]["input_schema"]["properties"]["limit"][
            "maximum"
        ]
        == MAX_READ_LIMIT
    )


def test_catalog_specs_are_fresh_data_and_handlers_register_without_vuoro() -> None:
    first = catalog_operation_specs()
    first[0]["input_schema"]["properties"].clear()
    assert "candidate" in catalog_operation_specs()[0]["input_schema"]["properties"]

    registry = _Registry()
    application = CentralKnowledgeApplication(connection_factory=lambda: None)
    VuoroKnowledgeAdapter(application).register(
        registry, definition_factory=_definition
    )

    assert [definition.name for definition, _handler in registry.operations] == [
        spec["name"] for spec in catalog_operation_specs()
    ]
    assert all(callable(handler) for _definition, handler in registry.operations)


def test_repository_bearing_operations_require_authorized_envelope_scope() -> None:
    specs = {spec["name"]: spec for spec in catalog_operation_specs()}
    assert specs["knowledge.schema.compatibility"]["repo_scoped"] is False
    for name, spec in specs.items():
        if name != "knowledge.schema.compatibility":
            assert spec["repo_scoped"] is True

    for name in (
        "knowledge.candidate.show",
        "knowledge.candidate.approve",
        "knowledge.candidate.reject",
        "knowledge.publication-reference.show",
        "knowledge.publication-reference.supersede",
    ):
        assert "repo_id" in specs[name]["input_schema"]["required"]


def test_adapter_rejects_argument_scope_mismatch_before_application_call() -> None:
    class _Application:
        def list_candidates(self, **_kwargs: Any) -> dict[str, Any]:
            pytest.fail("cross-repository request reached the knowledge application")

    def reject(code: str, message: str, status: int) -> BaseException:
        return RuntimeError(f"{code}:{status}:{message}")

    adapter = VuoroKnowledgeAdapter(_Application(), rejection_factory=reject)  # type: ignore[arg-type]
    context = type("Context", (), {"repo_id": "agentops"})()
    with pytest.raises(RuntimeError, match="knowledge-repo-mismatch:403"):
        adapter.list_candidates(
            {"repo_id": "another-repository", "status": None, "candidate_kind": None},
            context,
        )


def test_legacy_cli_import_does_not_load_served_or_postgres_dependencies() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import kctl.cli; "
                "assert 'vuoro_service' not in sys.modules; "
                "assert 'psycopg' not in sys.modules; "
                "assert 'kctl.vuoro' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
