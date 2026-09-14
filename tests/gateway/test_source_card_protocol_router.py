import pytest
from gateway.source_card_prefetch import _source_card_worker_protocol_payload, _source_card_worker_reference_context

START = "<!-- SOURCE-CARD WORKER MODE START -->"
END = "<!-- SOURCE-CARD WORKER MODE END -->"
ROUTER = {"content": "Read references/intake-protocol.md", "name": "source-card-intake"}


def test_legacy_payload_is_unchanged(tmp_path):
    legacy = {"content": "old complete protocol"}
    assert _source_card_worker_protocol_payload(legacy, tmp_path) is legacy


def test_router_loads_worker_only_and_preserves_metadata(tmp_path):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "intake-protocol.md").write_text("Parent directives\n" + START + "\nworker-only\n" + END + "\nreceipt directives")
    result = _source_card_worker_protocol_payload(ROUTER, tmp_path)
    assert result == {**ROUTER, "content": START + "\nworker-only\n" + END}


@pytest.mark.parametrize("text", ["missing", END + START, START + START + END, START + "\n " + END])
def test_bad_markers_fail_closed(tmp_path, text):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "intake-protocol.md").write_text(text)
    with pytest.raises(RuntimeError, match="markers_invalid"):
        _source_card_worker_protocol_payload(ROUTER, tmp_path)


def test_missing_or_linked_protocol_is_rejected(tmp_path):
    refs = tmp_path / "references"
    refs.mkdir()
    with pytest.raises(RuntimeError):
        _source_card_worker_protocol_payload(ROUTER, tmp_path)
    outside = tmp_path / "elsewhere"
    outside.write_text(START + END)
    (refs / "intake-protocol.md").symlink_to(outside)
    with pytest.raises(RuntimeError):
        _source_card_worker_protocol_payload(ROUTER, tmp_path)


@pytest.mark.parametrize("reader", [
    lambda root: _source_card_worker_protocol_payload(ROUTER, root),
    _source_card_worker_reference_context,
])
def test_worker_reference_directory_cannot_redirect_outside_skill(tmp_path, reader):
    skill = tmp_path / "skill"
    skill.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "intake-protocol.md").write_text(START + "\nworker\n" + END)
    for name in ("research-method.md", "card-schema.md", "receipts-and-ledger.md"):
        (external / name).write_text("external instructions")
    (skill / "references").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a regular directory"):
        reader(skill)


@pytest.mark.parametrize("content", [b"\xff", b"x" * 14_001])
def test_protocol_rejects_invalid_encoding_or_oversized_input(tmp_path, content):
    references = tmp_path / "references"
    references.mkdir()
    (references / "intake-protocol.md").write_bytes(content)
    with pytest.raises(RuntimeError):
        _source_card_worker_protocol_payload(ROUTER, tmp_path)
