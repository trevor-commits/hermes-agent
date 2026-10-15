"""The committed prompt-surface snapshot is exactly what the code renders.

``tests/fixtures/prompt_surface/`` holds the system prompt and ``tools[]`` array a stock install
sends on each surface (see ``scripts/ci/prompt_surface.py``). A code change that alters either
must carry the regenerated snapshot, so the change shows up in review as real prompt text and
real schemas, and the snapshot path needs a hermes-agent-core approval to merge.

Regenerate with ``scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render``.
"""

from __future__ import annotations

import filecmp
import importlib.util
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prompt_surface", REPO / "scripts" / "ci" / "prompt_surface.py")
assert _spec is not None and _spec.loader is not None
prompt_surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prompt_surface)


def _tree_differences(left: Path, right: Path) -> list[str]:
    cmp = filecmp.dircmp(left, right)
    out = [f"only in committed: {n}" for n in cmp.left_only] + [f"not committed: {n}" for n in cmp.right_only]
    _, mismatch, errors = filecmp.cmpfiles(left, right, cmp.common_files, shallow=False)
    out += [f"stale: {n}" for n in mismatch + errors]
    for sub in cmp.common_dirs:
        out += [f"{sub}/{d}" for d in _tree_differences(left / sub, right / sub)]
    return out


def test_committed_prompt_surface_is_current(tmp_path):
    rendered = tmp_path / "prompt_surface"
    assert prompt_surface.render(rendered) == 0
    differences = _tree_differences(prompt_surface.SNAPSHOT, rendered)
    assert not differences, (
        "The system prompt or tool schemas changed. Regenerate the snapshot and commit it with the "
        "change (merging it needs a hermes-agent-core approval):\n"
        "    scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render\n"
        + "\n".join(differences[:20])
    )


def test_platform_pin_preserves_native_python_metadata_and_network_ban(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"))
    result = subprocess.run(
        [sys.executable, "-c", """
import os
import socket
import sys
from scripts.ci.prompt_surface import _pin_environment
_pin_environment()
import sysconfig
import psutil
from zoneinfo import ZoneInfo
assert sys.platform == "linux"
assert sysconfig.get_config_var("py_version")
assert str(ZoneInfo("UTC")) == "UTC"
assert psutil.Process().pid == os.getpid()
with socket.socket() as sock:
    for operation, args in (
        (sock.connect, (("127.0.0.1", 1),)),
        (socket.create_connection, (("127.0.0.1", 1),)),
        (socket.getaddrinfo, ("example.invalid", 443)),
    ):
        try:
            operation(*args)
        except OSError as exc:
            assert "network disabled while rendering" in str(exc), str(exc)
        else:
            raise AssertionError("prompt rendering allowed network access")
"""],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_render_isolates_managed_config_and_canonicalizes_home(tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.mkdir()
    monkeypatch.setattr(prompt_surface.tempfile, "TemporaryDirectory", lambda **_k: nullcontext(str(alias / "..")))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "foreign-policy"))

    def child(_argv, *, env, **_kwargs):
        home = (tmp_path / "home").resolve()
        assert env["HOME"] == str(home)
        assert env["HERMES_HOME"] == str(home / ".hermes")
        assert env["HERMES_MANAGED_DIR"] == str(home / "managed")
        assert not Path(env["HERMES_MANAGED_DIR"]).exists()
        return subprocess.CompletedProcess(_argv, 1, stderr="")

    monkeypatch.setattr(prompt_surface.subprocess, "run", child)
    assert prompt_surface.render(tmp_path / "unused-output") == 1


def test_summary_names_the_surface_and_tool_that_changed(tmp_path):
    base = prompt_surface.SNAPSHOT
    head = tmp_path / "head"
    shutil.copytree(base, head)
    prompt = head / "prompts" / "cli.claude.txt"
    text = prompt.read_text(encoding="utf-8-sig")
    prompt.write_text(text.replace("# Finishing the job", "# Finish every job"), encoding="utf-8")
    tool = head / "tools" / "write_file.json"
    text = tool.read_text(encoding="utf-8-sig")
    tool.write_text(text.replace('"description": "', '"description": "Prefer patch. ', 1), encoding="utf-8")

    summary = prompt_surface.summarize(base, head)

    assert "| cli.claude |" in summary and "+# Finish every job" in summary
    assert "`write_file`: description +" in summary
    assert prompt_surface.summarize(base, base) == ""
