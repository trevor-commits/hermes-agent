"""Fail-closed exit codes for the live corpus arm.

A caller that reads exit 0 as "this model passed" must not be able to do that
when the arm proved nothing: missing credential, HTTP failure, attestation
mismatch, an incomplete run, or validity below --min-valid.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.gateway import live_corpus_arm as arm

_ARM = Path(__file__).resolve().parent / "live_corpus_arm.py"
_KEY_ENV = "LIVE_ARM_TEST_KEY"


def _write_packet(corpus: Path, packet_id: str) -> None:
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / f"{packet_id}.json").write_text(
        json.dumps(
            {
                "packet_id": packet_id,
                "worker_response": json.dumps({"card_content": "TEMPLATE"}),
                "prefetched_x_posts": [],
            }
        ),
        encoding="utf-8",
    )


def _valid_worker_json(cards_root: Path, filename: str) -> str:
    return json.dumps(
        {
            "card_path": str(cards_root / filename),
            "card_content": (
                f"# {filename}\n\n"
                "## Decision manifest (ER-278)\n\n"
                f"- decision-key: card:{filename}#watch\n"
            ),
        }
    )


def _serve(*, status: int = 200, served_model: str = "test-model", contents: list[str] | None = None):
    payload_contents = list(contents or ["not-json"])
    state = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if status >= 400:
                self.wfile.write(b'{"error":"unavailable"}')
                return
            content = payload_contents[min(state["n"], len(payload_contents) - 1)]
            state["n"] += 1
            body = {
                "model": served_model,
                "choices": [{"message": {"content": content}}],
            }
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _argv(tmp_path: Path, **flags) -> list[str]:
    corpus = tmp_path / "corpus"
    cards = tmp_path / "cards"
    argv = [
        str(_ARM),
        "--provider-url",
        flags.get("provider_url", "http://127.0.0.1:9/v1/chat/completions"),
        "--key-env",
        _KEY_ENV,
        "--model",
        flags.get("model", "test-model"),
        "--corpus",
        str(corpus),
        "--cards-root",
        str(cards),
    ]
    if "max_calls" in flags:
        argv.extend(["--max-calls", str(flags["max_calls"])])
    if flags.get("allow_missing"):
        argv.append("--allow-missing-credential")
    if "min_valid" in flags:
        argv.extend(["--min-valid", str(flags["min_valid"])])
    return argv


def _run(tmp_path, monkeypatch, argv, *, have_key: bool = True) -> int:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    if have_key:
        monkeypatch.setenv(_KEY_ENV, "sk-test")
    else:
        monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", argv)
    return arm.main()


def test_missing_credential_exits_2(tmp_path, monkeypatch, capsys):
    code = _run(tmp_path, monkeypatch, _argv(tmp_path), have_key=False)
    captured = capsys.readouterr()
    assert code == 2
    assert "CANNOT GATE: no LIVE_ARM_TEST_KEY" in captured.err


def test_allow_missing_credential_exits_0(tmp_path, monkeypatch, capsys):
    code = _run(
        tmp_path,
        monkeypatch,
        _argv(tmp_path, allow_missing=True),
        have_key=False,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "SKIPPED: no LIVE_ARM_TEST_KEY" in captured.out
    assert captured.err == ""


def test_http_error_exits_nonzero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    server = _serve(status=429)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        code = _run(tmp_path, monkeypatch, _argv(tmp_path, provider_url=url))
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: HTTP failure — HTTP 429" in captured.err


def test_bogus_provider_url_exits_nonzero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    code = _run(
        tmp_path,
        monkeypatch,
        _argv(tmp_path, provider_url="http://127.0.0.1:1/v1/chat/completions"),
    )
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: HTTP failure —" in captured.err


def test_attestation_mismatch_exits_nonzero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    cards = tmp_path / "cards"
    cards.mkdir()
    server = _serve(
        served_model="other-model",
        contents=[_valid_worker_json(cards, "live-p1.md")],
    )
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        code = _run(
            tmp_path,
            monkeypatch,
            _argv(tmp_path, provider_url=url, model="requested-model"),
        )
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: attestation failure" in captured.err
    assert "requested-model" in captured.err
    assert "other-model" in captured.err


def test_zero_valid_packets_exits_nonzero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    server = _serve(served_model="test-model", contents=["not-json"])
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        code = _run(tmp_path, monkeypatch, _argv(tmp_path, provider_url=url))
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: zero valid packets" in captured.err


def test_below_min_valid_exits_nonzero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    _write_packet(tmp_path / "corpus", "p2")
    cards = tmp_path / "cards"
    cards.mkdir()
    valid = _valid_worker_json(cards, "live-p1.md")
    server = _serve(served_model="test-model", contents=[valid, "not-json"])
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        code = _run(
            tmp_path,
            monkeypatch,
            _argv(tmp_path, provider_url=url, min_valid=2),
        )
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    assert code != 0
    assert "below min-valid 2" in captured.err


def test_incomplete_run_when_corpus_is_empty(tmp_path, monkeypatch, capsys):
    (tmp_path / "corpus").mkdir()
    code = _run(tmp_path, monkeypatch, _argv(tmp_path))
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: incomplete run — 0 packets found" in captured.err


def test_incomplete_run_when_max_calls_is_zero(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    code = _run(tmp_path, monkeypatch, _argv(tmp_path, max_calls=0))
    captured = capsys.readouterr()
    assert code != 0
    assert "CANNOT GATE: incomplete run" in captured.err
    assert "--max-calls 0" in captured.err


def test_matching_valid_run_exits_0(tmp_path, monkeypatch, capsys):
    _write_packet(tmp_path / "corpus", "p1")
    cards = tmp_path / "cards"
    cards.mkdir()
    server = _serve(
        served_model="zai/test-model",
        contents=[_valid_worker_json(cards, "live-p1.md")],
    )
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        code = _run(tmp_path, monkeypatch, _argv(tmp_path, provider_url=url))
    finally:
        server.shutdown()
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.err == ""
    assert "1/1" in captured.out
