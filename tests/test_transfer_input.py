import hashlib
import json
import os
from unittest.mock import patch

import pytest

from kctl import transfer


def _artifact():
    value = {
        "schema_version": transfer.SCHEMA_VERSION,
        "repo_id": "kctl",
        "git_revision": "a" * 40,
        "exported_at": "2026-10-18T12:00:00Z",
        "candidates": [],
        "publications": [],
    }
    value["artifact_digest"] = transfer._artifact_digest(value)
    return value


@pytest.mark.parametrize("duplicate", ['"repo_id":"wrong",', '"extra":{"key":1,"key":2},'])
def test_duplicate_keys_are_refused_even_when_effective_digest_matches(tmp_path, duplicate):
    value = _artifact()
    if "extra" in duplicate:
        value["extra"] = {"key": 2}
        value["artifact_digest"] = transfer._artifact_digest(value)
    raw = json.dumps(value)
    if "extra" in duplicate:
        raw = raw.replace('"extra": {"key": 2}', '"extra":{"key":1,"key":2}')
    else:
        raw = "{" + duplicate + raw[1:]
    path = tmp_path / "transfer.json"
    path.write_text(raw)
    with pytest.raises(transfer.TransferArtifactError, match="duplicate"):
        transfer.read_artifact(path)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_nonfinite_json_numbers_are_refused(tmp_path, number):
    value = _artifact()
    value["extra"] = float(number)
    # Match the legacy digest algorithm so the old reader would accept this.
    unsigned = {key: item for key, item in value.items() if key != "artifact_digest"}
    value["artifact_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    raw = json.dumps(value).replace(json.dumps(value["extra"]), number)
    path = tmp_path / "transfer.json"
    path.write_text(raw)
    with pytest.raises(transfer.TransferArtifactError, match="finite"):
        transfer.read_artifact(path)


@pytest.mark.parametrize("raw", [b'\xff', b'[' * 2000 + b']' * 2000])
def test_decode_and_nesting_errors_use_transfer_error(tmp_path, raw):
    path = tmp_path / "transfer.json"
    path.write_bytes(raw)
    with pytest.raises(transfer.TransferArtifactError):
        transfer.read_artifact(path)


def test_regular_file_size_bound_and_boundary_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "transfer.json"
    value = _artifact()
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(transfer, "_MAX_ARTIFACT_BYTES", len(raw), raising=False)
    assert transfer.read_artifact(path) == value
    monkeypatch.setattr(transfer, "_MAX_ARTIFACT_BYTES", len(raw) - 1)
    with pytest.raises(transfer.TransferArtifactError, match="byte bound"):
        transfer.read_artifact(path)


def test_symlink_and_fifo_are_refused_without_reading(tmp_path):
    target = tmp_path / "regular.json"
    target.write_text(json.dumps(_artifact()))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(transfer.TransferArtifactError, match="regular file"):
        transfer.read_artifact(link)
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    # Detect an unsafe open before it could block the test process.
    original_open = os.open

    def checked_open(path, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    with patch.object(transfer.os, "open", checked_open):
        with pytest.raises(transfer.TransferArtifactError, match="regular file"):
            transfer.read_artifact(fifo)


def test_programmatic_nonfinite_artifact_is_refused():
    value = _artifact()
    value["extra"] = float("nan")
    with pytest.raises(transfer.TransferArtifactError, match="finite"):
        transfer._artifact_digest(value)


def test_oversize_write_leaves_existing_destination_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "transfer.json"
    path.write_bytes(b"original")
    monkeypatch.setattr(transfer, "_MAX_ARTIFACT_BYTES", 1, raising=False)
    with pytest.raises(transfer.TransferArtifactError, match="byte bound"):
        transfer.write_artifact(path, _artifact())
    assert path.read_bytes() == b"original"


def test_read_enforces_byte_bound_when_file_grows_after_stat(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "transfer.json"
    path.write_text(json.dumps(_artifact()))
    monkeypatch.setattr(transfer, "_MAX_ARTIFACT_BYTES", 10)
    original_fstat = os.fstat

    def stale_size(descriptor):
        metadata = original_fstat(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=0)

    monkeypatch.setattr(transfer.os, "fstat", stale_size)
    with pytest.raises(transfer.TransferArtifactError, match="byte bound"):
        transfer.read_artifact(path)


@pytest.mark.parametrize("raw", ['{"key":1,"key":2}', '{"key":NaN}', '{"key":1e400}'])
def test_export_source_payloads_use_strict_json(raw):
    with pytest.raises(transfer.TransferArtifactError, match="source.payload must contain valid JSON"):
        transfer._json_value(raw, "source.payload")


def test_unicode_canonical_bytes_and_digest_are_unchanged(tmp_path):
    value = _artifact()
    value["extra"] = {"summary": "café 東京", "number": 1.5}
    unsigned = {key: item for key, item in value.items() if key != "artifact_digest"}
    value["artifact_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    path = tmp_path / "transfer.json"
    transfer.write_artifact(path, value)
    assert path.read_bytes() == expected
    assert transfer.read_artifact(path) == value
