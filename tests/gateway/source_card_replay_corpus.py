"""No-push gateway replay corpus for the Telegram source-card route.

Why this exists
---------------
The pre-existing end-to-end test builds a real bare git remote and asserts a
real commit, which is good, but it feeds the provider a single canned
known-good card. It therefore tests plumbing and **cannot sample model
variance**, which is the thing that actually broke live intakes.

`scripts/card-draft-eval` in the cards repo cannot serve as this gate either:
it exits 2 without `--live`/`--dry-run`, its `LIVE_ARMS` are hardcoded
DeepSeek with no `--provider`/`--model` flag, it measures its own prompt pair
rather than the gateway's worker prompt, and it validates through
`validate-researched-repos` into a fresh temp dir rather than through
`validate-touched-source-cards`.

So this harness runs the PRODUCTION path — `_parse_source_card_worker_draft`
(typed analysis included), `_finalize_source_card_worker_draft`,
`_source_card_render_card_routing`, `validate-touched-source-cards`, and
`_land_source_card` into a temporary git remote. Nothing here can reach
`origin`: every landing targets a throwaway bare repo under pytest's tmp_path.

Two arms
--------
`recorded`  Replays drafts captured from, or shaped exactly like, the intakes
            that failed live. Deterministic, no provider, no cost. This is the
            arm that measures the deterministic layer where the fix lives.
`live`      One pinned provider arm carrying raw response-model attestation.
            Skipped when the provider is unreachable; the skip reason is
            recorded rather than silently passing.

Six gate rows are reported as numbers, including first-pass structured-payload
validity, which is recorded and never used as a blocker.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class PacketResult:
    packet_id: str
    first_pass_payload_valid: bool
    landed: bool
    strict_validation_passed: bool
    protected_facts_preserved: bool
    contradictions_clean: bool
    attested: bool
    landed_without_attestation: bool
    commit: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CorpusReport:
    arm: str
    results: list[PacketResult] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def total(self) -> int:
        return len(self.results)

    def rows(self) -> dict[str, str]:
        n = self.total
        return {
            "first-pass structured-payload validity": (
                f"{sum(r.first_pass_payload_valid for r in self.results)}/{n}"
                " (recorded, not a blocker)"
            ),
            "final strict source-card validation": (
                f"{sum(r.strict_validation_passed for r in self.results)}/{n}"
            ),
            "protected-fact preservation": (
                f"{sum(r.protected_facts_preserved for r in self.results)}/{n}"
            ),
            "contradiction scan": (
                f"{sum(r.contradictions_clean for r in self.results)}/{n} clean"
            ),
            "provider/model attestation": (
                f"{sum(r.attested for r in self.results)}/{n}"
            ),
            "landings on missing/mismatched attestation": (
                f"{sum(r.landed_without_attestation for r in self.results)}"
            ),
        }

    def gate_passed(self) -> bool:
        n = self.total
        if not n:
            return False
        return (
            sum(r.strict_validation_passed for r in self.results) == n
            and sum(r.protected_facts_preserved for r in self.results) == n
            and sum(r.contradictions_clean for r in self.results) == n
            and sum(r.attested for r in self.results) == n
            and sum(r.landed_without_attestation for r in self.results) == 0
        )


def _missing_protected_facts(card: str, protected: list) -> list[str]:
    """Facts from the prefetched evidence that must survive into the card.

    Declared per packet rather than scraped from the draft, because the
    finalizer legitimately REPLACES `url`/`owner/name` with an evidence-safe
    default when prefetch produced nothing: refusing to assert an unverified
    identity is correct behaviour, not a lost fact. What must never be lost is
    a fact the gateway actually prefetched.
    """
    return [fact for fact in (protected or []) if str(fact) not in card]


_CONTRADICTION_PAIRS = (
    # (claim, contradicting claim) - both present in one card is a contradiction
    ("not installed", "installed and running"),
    ("no repository identified", "current pinned sha: "),
)


def _contradictions(card: str) -> list[str]:
    lowered = card.lower()
    found = []
    for left, right in _CONTRADICTION_PAIRS:
        if left in lowered and right in lowered:
            # `current pinned sha: none/n/a` is not a repository claim
            if right == "current pinned sha: " and any(
                token in lowered
                for token in ("current pinned sha: none", "current pinned sha: n/a")
            ):
                continue
            found.append(f"{left} vs {right}")
    return found


def run_packet(
    *,
    packet: dict[str, Any],
    fixture: dict,
    landing_environment: dict[str, str],
    served_model: Optional[str],
    requested_model: str,
) -> PacketResult:
    """Run one packet through the production render/validate/land path."""
    from gateway.run import (
        _finalize_source_card_worker_draft,
        _land_source_card,
        _parse_source_card_worker_draft,
        _source_card_attestation_error,
    )

    packet_id = str(packet["packet_id"])
    response = str(packet["worker_response"])
    cards_root = Path(landing_environment["cards_root"])

    receipt = [{"call": 1, "requested": requested_model, "served": served_model}]
    attestation_error = _source_card_attestation_error(receipt, ran_turn=True)
    attested = attestation_error is None

    first_pass_valid = True
    try:
        card_path, card_content = _parse_source_card_worker_draft(response, cards_root)
    except Exception as exc:  # draft rejected before any write
        return PacketResult(
            packet_id=packet_id,
            first_pass_payload_valid=False,
            landed=False,
            strict_validation_passed=False,
            protected_facts_preserved=False,
            contradictions_clean=False,
            attested=attested,
            landed_without_attestation=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    protected = packet.get("protected_facts") or []

    card_content = _finalize_source_card_worker_draft(
        card_path=card_path,
        content=card_content,
        prefetched_x_posts=packet.get("prefetched_x_posts") or [],
        prefetched_github_repositories=packet.get("prefetched_github_repositories")
        or [],
    )

    if not attested:
        # The production route refuses to land an unattested turn. Prove it here
        # rather than asserting it only in a unit test.
        return PacketResult(
            packet_id=packet_id,
            first_pass_payload_valid=first_pass_valid,
            landed=False,
            strict_validation_passed=False,
            protected_facts_preserved=True,
            contradictions_clean=not _contradictions(card_content),
            attested=False,
            landed_without_attestation=False,
            error=attestation_error,
        )

    card_path.write_text(card_content, encoding="utf-8")
    try:
        landing = _land_source_card(
            card_path=card_path,
            card_content=card_content,
            intake_text=str(packet.get("intake_text") or ""),
            environment=landing_environment,
            source_message_row_id=int(packet.get("source_message_row_id") or 1),
        )
    except Exception as exc:
        return PacketResult(
            packet_id=packet_id,
            first_pass_payload_valid=first_pass_valid,
            landed=False,
            strict_validation_passed=False,
            protected_facts_preserved=not _missing_protected_facts(
                card_content, protected
            ),
            contradictions_clean=not _contradictions(card_content),
            attested=True,
            landed_without_attestation=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    committed = subprocess.run(
        [
            "git",
            "-C",
            str(fixture["repo"]),
            "show",
            f"{landing['commit']}:{landing['path']}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    committed_text = committed.stdout if committed.returncode == 0 else card_content

    return PacketResult(
        packet_id=packet_id,
        first_pass_payload_valid=first_pass_valid,
        landed=True,
        # Landing runs validate-touched-source-cards internally and raises on
        # a non-zero exit, so reaching here IS the strict pass.
        strict_validation_passed=True,
        protected_facts_preserved=not _missing_protected_facts(
            committed_text, protected
        ),
        contradictions_clean=not _contradictions(committed_text),
        attested=True,
        landed_without_attestation=False,
        commit=landing["commit"],
    )


def format_report(report: CorpusReport) -> str:
    lines = [f"arm: {report.arm}  packets: {report.total}"]
    if report.skipped_reason:
        lines.append(f"SKIPPED: {report.skipped_reason}")
        return "\n".join(lines)
    width = max(len(k) for k in report.rows())
    for name, value in report.rows().items():
        lines.append(f"  {name.ljust(width)}  {value}")
    failures = [r for r in report.results if r.error]
    for failure in failures:
        lines.append(f"  ! {failure.packet_id}: {failure.error}")
    lines.append(f"  GATE: {'PASS' if report.gate_passed() else 'FAIL'}")
    return "\n".join(lines)


def load_corpus(corpus_dir: Path) -> list[dict[str, Any]]:
    packets = []
    for path in sorted(corpus_dir.glob("*.json")):
        packets.append(json.loads(path.read_text(encoding="utf-8")))
    return packets


def build_corpus_fixture(tmp_path: Path, cards_repo: Path, python_exe: str) -> dict:
    """Temp git repo carrying the REAL source-card validator.

    The route suite's offline fixture stubs `validate-touched-source-cards`
    with a script hardcoded to one filename, which is fine for asserting argv
    but makes "strict validation passed" meaningless as a corpus row. Here the
    real validator and its `chat_context_index` package are copied in, so a
    packet that lands has genuinely satisfied the schema.
    """
    import shutil

    repo = tmp_path / "cards-repo"
    remote = tmp_path / "cards-remote.git"
    cards_root = repo / "researched-repos"
    home = tmp_path / "hermes-home"
    (home / "scripts").mkdir(parents=True)
    cards_root.mkdir(parents=True)

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (
        ("user.name", "Source Card Corpus"),
        ("user.email", "source-card-corpus@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value], check=True, capture_output=True
        )

    shutil.copytree(cards_repo / "src" / "chat_context_index", repo / "src" / "chat_context_index")
    (repo / "scripts").mkdir(exist_ok=True)
    for name in ("validate-touched-source-cards", "validate-researched-repos"):
        shutil.copy2(cards_repo / "scripts" / name, repo / "scripts" / name)
        (repo / "scripts" / name).chmod(0o755)
    # `validate-researched-repos` execs bare `python3`; pin it to an interpreter
    # new enough for the package's typing syntax.
    wrapper = repo / "scripts" / "validate-researched-repos"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
        'export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"\n'
        f'exec {python_exe} -m chat_context_index.source_card_validator "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    (repo / "README.md").write_text("corpus replay\n", encoding="utf-8")
    (repo / "todo.md").write_text("# todo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "corpus: base"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    decision_log = tmp_path / "decision-argv.jsonl"
    writer = home / "scripts" / "hermes-research-decisions"
    writer.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(decision_log)!r}).open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True, 'command': sys.argv[1]}))\n",
        encoding="utf-8",
    )
    writer.chmod(0o755)

    import sqlite3

    transcript_db = home / "state.db"
    with sqlite3.connect(transcript_db) as connection:
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
            " role TEXT NOT NULL, platform_message_id TEXT)"
        )
        connection.execute(
            "INSERT INTO messages (id, session_id, role, platform_message_id)"
            " VALUES (1, 'sess-corpus', 'user', 'msg-corpus')"
        )

    return {
        "repo": repo,
        "remote": remote,
        "cards_root": cards_root,
        "home": home,
        "decision_writer": writer,
        "decision_log": decision_log,
        "transcript_db": transcript_db,
    }
