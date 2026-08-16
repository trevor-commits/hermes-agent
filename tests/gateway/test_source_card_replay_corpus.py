"""Run the no-push replay corpus through the production landing path.

Every landing here targets a throwaway bare git repo under pytest's tmp_path.
Nothing in this module can reach `origin`.
"""

import json
from pathlib import Path

import pytest

from tests.gateway.source_card_replay_corpus import (
    CorpusReport,
    build_corpus_fixture,
    format_report,
    load_corpus,
    run_packet,
)

_CARDS_REPO = Path("/Users/gillettes/Coding Projects/3rd Party Git Hub Repo's")
_VALIDATOR_PYTHON = "/opt/homebrew/bin/python3.14"

# The corpus runs the REAL validator, which lives in the cards repo. Skip
# rather than pass when that checkout or interpreter is absent, so a missing
# dependency can never read as a green gate.
_CORPUS_AVAILABLE = (
    (_CARDS_REPO / "src" / "chat_context_index").is_dir()
    and (_CARDS_REPO / "scripts" / "validate-touched-source-cards").is_file()
    and Path(_VALIDATOR_PYTHON).exists()
)
_requires_corpus = pytest.mark.skipif(
    not _CORPUS_AVAILABLE,
    reason="cards repo or validator interpreter unavailable",
)

_CORPUS_DIR = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "source_card_intake"
    / "replay_corpus"
)
_REQUESTED_MODEL = "glm-5.3"


def _environment(fixture):
    return {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            fixture["repo"] / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5362061841",
        "source_thread_id": "",
        "parent_session_id": "sess-corpus",
        "platform_message_id": "msg-corpus",
        "transcript_db": str(fixture["home"] / "state.db"),
    }


def test_corpus_is_non_empty_and_covers_the_live_failure_shapes():
    packets = load_corpus(_CORPUS_DIR)
    assert len(packets) >= 8
    ids = {p["packet_id"] for p in packets}
    # Each of these reproduces a shape that cost a real Telegram intake.
    assert {
        "02-routing-prose-because",
        "03-routing-prose-parens",
        "04-routing-prose-colon",
        "05-embedded-todo",
        "08-wrong-slug-only",
    } <= ids


@_requires_corpus
def test_recorded_arm_lands_every_packet(tmp_path, capsys):
    """The deterministic layer must carry every recorded failure shape."""
    packets = load_corpus(_CORPUS_DIR)
    report = CorpusReport(arm="recorded")

    for index, packet in enumerate(packets):
        fixture = build_corpus_fixture(tmp_path / f"packet-{index}", _CARDS_REPO, _VALIDATOR_PYTHON)
        environment = _environment(fixture)
        resolved = dict(packet)
        resolved["worker_response"] = packet["worker_response"].replace(
            "__CARDS_ROOT__", str(fixture["cards_root"])
        )
        report.results.append(
            run_packet(
                packet=resolved,
                fixture=fixture,
                landing_environment=environment,
                served_model=_REQUESTED_MODEL,
                requested_model=_REQUESTED_MODEL,
            )
        )

    with capsys.disabled():
        print()
        print(format_report(report))

    assert report.total == len(packets)
    assert report.gate_passed(), format_report(report)


@_requires_corpus
def test_unattested_turn_never_lands_a_card(tmp_path):
    """A substituted model blocks the landing outright."""
    packets = load_corpus(_CORPUS_DIR)
    fixture = build_corpus_fixture(tmp_path / "attestation", _CARDS_REPO, _VALIDATOR_PYTHON)
    packet = dict(packets[0])
    packet["worker_response"] = packet["worker_response"].replace(
        "__CARDS_ROOT__", str(fixture["cards_root"])
    )

    result = run_packet(
        packet=packet,
        fixture=fixture,
        landing_environment=_environment(fixture),
        served_model="glm-5.4",
        requested_model=_REQUESTED_MODEL,
    )
    assert result.attested is False
    assert result.landed is False
    assert result.landed_without_attestation is False
    assert "attestation" in (result.error or "")
