from __future__ import annotations

import sys
import types

from kctl.served_knowledge import ServedKnowledgeClient
from kctl.source import ServedProfile


def test_knowledge_reads_bind_record_arguments_to_envelope_repo(monkeypatch):
    calls = []

    class Profile:
        def __init__(self, **kwargs): self.kwargs = kwargs

    class Client:
        def __init__(self, *_args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def invoke(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            return {"candidates": [], "count": 0, "limit": 50}

    monkeypatch.setitem(sys.modules, "vuoro_client", types.SimpleNamespace(AsyncVuoroClient=Client, Profile=Profile))
    client = ServedKnowledgeClient(ServedProfile("dev", "https://vuoro/", "file:/token", "dev"), "agentops")
    assert client.list_candidates() == {"candidates": [], "count": 0, "limit": 50}
    assert calls == [("knowledge.candidate.list", {"repo_id": "agentops", "status": None, "candidate_kind": None, "limit": 50}, {"repo_id": "agentops"})]
