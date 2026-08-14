"""Structural guardrails for the central knowledge application boundary."""

from kctl.application import CentralKnowledgeApplication
from kctl.application_candidates import CandidateService
from kctl.application_core import KnowledgeApplicationCore
from kctl.application_publications import PublicationService


def test_application_is_composed_from_candidate_and_publication_services():
    assert CentralKnowledgeApplication.__mro__[1:4] == (
        PublicationService,
        CandidateService,
        KnowledgeApplicationCore,
    )


def test_application_methods_live_in_focused_services():
    assert CentralKnowledgeApplication.intake_candidate.__module__ == CandidateService.__module__
    assert CentralKnowledgeApplication.approve_candidate.__module__ == CandidateService.__module__
    assert CentralKnowledgeApplication.record_publication_reference.__module__ == PublicationService.__module__
    assert CentralKnowledgeApplication.supersede_publication.__module__ == PublicationService.__module__
    assert CentralKnowledgeApplication.compatibility.__module__ == KnowledgeApplicationCore.__module__
