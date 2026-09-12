"""Pin kctl's declared authority vocabulary.

The ecosystem's authority grammar is per-domain and is matched exactly, with
no normalization anywhere: vuoro-service tests plain set membership of
``operation.definition.required_authority`` against the identity's granted
authorities. Work uses colon-separated names (``work:read``) while knowledge,
execution and audit use dots, so a colon-style rename of a knowledge authority
does not fail at registration -- it silently authorizes nothing at invocation
and surfaces only as ``authority-required`` after a successful authenticate.

These tests therefore pin the literal strings, not the constants, and pin the
closure between declared authorities and registered operations in both
directions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kctl import vuoro
from kctl.application import CentralKnowledgeApplication
from kctl.vuoro import (
    INTAKE_AUTHORITY,
    PUBLISH_AUTHORITY,
    READ_AUTHORITY,
    REVIEW_AUTHORITY,
    VuoroKnowledgeAdapter,
    catalog_operation_specs,
)

DECLARED_AUTHORITIES = frozenset(
    {
        "knowledge.read",
        "knowledge.candidate.intake",
        "knowledge.review",
        "knowledge.publication-reference.write",
    }
)

# Every registered operation, grouped by the authority it requires. A rename on
# either side of this mapping must fail here rather than at invocation time.
EXPECTED_AUTHORITY_USE = {
    "knowledge.read": (
        "knowledge.candidate.list",
        "knowledge.candidate.show",
        "knowledge.publication-reference.list",
        "knowledge.publication-reference.show",
        "knowledge.schema.compatibility",
    ),
    "knowledge.candidate.intake": ("knowledge.candidate.intake",),
    "knowledge.review": (
        "knowledge.candidate.approve",
        "knowledge.candidate.reject",
    ),
    "knowledge.publication-reference.write": (
        "knowledge.publication-reference.record",
        "knowledge.publication-reference.supersede",
    ),
}


@dataclass(frozen=True)
class _Definition:
    values: dict[str, Any]


def _definition(**values: Any) -> _Definition:
    return _Definition(values)


class _Registry:
    def __init__(self) -> None:
        self.definitions: list[_Definition] = []

    def register(self, definition: _Definition, handler: Any) -> None:
        self.definitions.append(definition)


def _module_authority_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(vuoro).items()
        if name.endswith("_AUTHORITY") and isinstance(value, str)
    }


def test_declared_authority_constants_have_exact_literal_values() -> None:
    assert READ_AUTHORITY == "knowledge.read"
    assert INTAKE_AUTHORITY == "knowledge.candidate.intake"
    assert REVIEW_AUTHORITY == "knowledge.review"
    assert PUBLISH_AUTHORITY == "knowledge.publication-reference.write"

    constants = _module_authority_constants()
    assert set(constants) == {
        "READ_AUTHORITY",
        "INTAKE_AUTHORITY",
        "REVIEW_AUTHORITY",
        "PUBLISH_AUTHORITY",
    }
    assert set(constants.values()) == set(DECLARED_AUTHORITIES)
    for name in sorted(constants):
        assert name in vuoro.__all__


def test_declared_authorities_use_the_knowledge_dot_grammar() -> None:
    for authority in sorted(DECLARED_AUTHORITIES):
        # Matching is exact set membership with no normalization anywhere, so a
        # colon-style name (work's grammar) would authorize nothing.
        assert ":" not in authority
        assert authority == authority.strip()
        assert authority == authority.lower()
        assert authority.startswith("knowledge.")
        assert "." in authority.removeprefix("knowledge.") or authority.count(".") == 1


def test_every_operation_requires_a_declared_authority() -> None:
    specs = catalog_operation_specs()
    assert specs

    observed: dict[str, list[str]] = {}
    for spec in specs:
        authority = spec["required_authority"]
        assert isinstance(authority, str) and authority
        assert authority in DECLARED_AUTHORITIES, (
            f"operation {spec['name']} requires undeclared authority {authority!r}"
        )
        observed.setdefault(authority, []).append(spec["name"])

    assert {key: tuple(value) for key, value in observed.items()} == (
        EXPECTED_AUTHORITY_USE
    )


def test_no_declared_authority_is_unused() -> None:
    used = {spec["required_authority"] for spec in catalog_operation_specs()}
    assert used == set(DECLARED_AUTHORITIES), (
        "declared but unused: "
        f"{sorted(DECLARED_AUTHORITIES - used)}; "
        f"used but undeclared: {sorted(used - DECLARED_AUTHORITIES)}"
    )


def test_registered_definitions_carry_the_same_authorities() -> None:
    registry = _Registry()
    VuoroKnowledgeAdapter(
        CentralKnowledgeApplication(connection_factory=lambda: None)
    ).register(registry, definition_factory=_definition)

    registered = {
        definition.values["name"]: definition.values["required_authority"]
        for definition in registry.definitions
    }
    expected = {
        name: authority
        for authority, names in EXPECTED_AUTHORITY_USE.items()
        for name in names
    }
    assert registered == expected
