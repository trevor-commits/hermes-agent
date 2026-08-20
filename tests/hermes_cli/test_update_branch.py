"""Tests for hermes_cli.update_branch — the single update-branch resolver.

Covers the resolution order (explicit > env > origin-verified HEAD branch >
main), the ls-remote self-heal semantics (exit 2 heals to main; transient
errors keep the branch), and the surface-agreement wiring: the banner check,
the dashboard changelog, the POST /api/hermes/update spawn, and the
``.update_check`` cache key all following the same resolved branch.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import update_branch


def _fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _completed(rc=0, stdout=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


class TestResolveUpdateBranch:
    def test_explicit_branch_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "envbranch")
        assert update_branch.resolve_update_branch(explicit="feature-x") == "feature-x"

    def test_env_override_wins_over_head(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "envbranch")
        repo = _fake_repo(tmp_path)
        assert update_branch.resolve_update_branch(repo_dir=repo) == "envbranch"

    def _patch_git(self, monkeypatch, head_branch, ls_remote_rc):
        def fake_run(cmd, **kwargs):
            if "symbolic-ref" in cmd:
                if head_branch is None:
                    return _completed(1)
                return _completed(0, head_branch + "\n")
            if "ls-remote" in cmd:
                return _completed(ls_remote_rc)
            raise AssertionError(f"unexpected git call: {cmd}")

        monkeypatch.setattr(update_branch.subprocess, "run", fake_run)

    def test_head_branch_kept_when_origin_has_it(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_UPDATE_BRANCH", raising=False)
        self._patch_git(monkeypatch, "keeper", ls_remote_rc=0)
        assert update_branch.resolve_update_branch(
            repo_dir=_fake_repo(tmp_path)) == "keeper"

    def test_heals_to_main_when_ref_definitively_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_UPDATE_BRANCH", raising=False)
        self._patch_git(monkeypatch, "keeper", ls_remote_rc=2)
        assert update_branch.resolve_update_branch(
            repo_dir=_fake_repo(tmp_path)) == "main"

    def test_transient_probe_error_keeps_branch(self, monkeypatch, tmp_path):
        # A network failure (rc 128) must NOT strand the install on main.
        monkeypatch.delenv("HERMES_UPDATE_BRANCH", raising=False)
        self._patch_git(monkeypatch, "keeper", ls_remote_rc=128)
        assert update_branch.resolve_update_branch(
            repo_dir=_fake_repo(tmp_path)) == "keeper"

    def test_detached_head_defaults_to_main(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_UPDATE_BRANCH", raising=False)
        self._patch_git(monkeypatch, None, ls_remote_rc=0)
        assert update_branch.resolve_update_branch(
            repo_dir=_fake_repo(tmp_path)) == "main"

    def test_main_head_skips_probe(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_UPDATE_BRANCH", raising=False)

        def fake_run(cmd, **kwargs):
            if "symbolic-ref" in cmd:
                return _completed(0, "main\n")
            raise AssertionError("main HEAD must not probe origin")

        monkeypatch.setattr(update_branch.subprocess, "run", fake_run)
        assert update_branch.resolve_update_branch(
            repo_dir=_fake_repo(tmp_path)) == "main"


class TestBranchPinnedUpdateCommand:
    def test_main_stays_bare(self, monkeypatch):
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "main")
        monkeypatch.setattr(update_branch, "_resolved", None)
        assert update_branch.branch_pinned_update_command(
            "hermes update") == "hermes update"

    def test_carried_branch_is_pinned(self, monkeypatch):
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "keeper")
        monkeypatch.setattr(update_branch, "_resolved", None)
        assert update_branch.branch_pinned_update_command(
            "hermes update") == "hermes update --branch keeper"

    def test_non_git_guidance_passes_through(self, monkeypatch):
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "keeper")
        monkeypatch.setattr(update_branch, "_resolved", None)
        assert update_branch.branch_pinned_update_command(
            "docker pull nousresearch/hermes-agent:latest"
        ) == "docker pull nousresearch/hermes-agent:latest"


class TestUpdateSurfaceAgreement:
    def test_banner_local_git_check_follows_branch(self, monkeypatch, tmp_path):
        """The banner fetches and counts against origin/<branch>, not main."""
        from hermes_cli import banner

        repo = _fake_repo(tmp_path)
        recorded = []

        def fake_run(cmd, **kwargs):
            recorded.append(list(cmd))
            joined = " ".join(cmd)
            if "get-url" in joined:
                return _completed(0, "git@github.com:someone/fork.git\n")
            if "--is-shallow-repository" in joined:
                return _completed(0, "false\n")
            if "fetch" in joined:
                return _completed(0)
            if "rev-list" in joined:
                return _completed(0, "3\n")
            return _completed(0, "")

        monkeypatch.setattr(banner.subprocess, "run", fake_run)
        assert banner._check_via_local_git(repo, "keeper") == 3
        fetches = [c for c in recorded if "fetch" in c]
        counts = [c for c in recorded if "rev-list" in c]
        assert fetches and fetches[0][-2:] == ["keeper", "--quiet"]
        assert counts and counts[0][-1] == "HEAD..origin/keeper"

    def test_update_check_cache_keyed_on_branch(self, monkeypatch, tmp_path):
        """A cached count for one branch is a MISS for another branch."""
        from hermes_cli import banner

        monkeypatch.setattr(banner, "get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_REVISION", raising=False)
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "keeper")
        monkeypatch.setattr(
            "hermes_cli.update_branch._resolved", None, raising=False)
        monkeypatch.setattr(
            banner, "_check_via_local_git", lambda repo, branch: 5)

        import time as _time
        cache = tmp_path / ".update_check"
        cache.write_text(json.dumps({
            "ts": _time.time(), "behind": 42, "rev": None,
            "ver": banner.VERSION, "branch": "main",
        }))

        behind = banner.check_for_updates()
        assert behind == 5  # fresh check ran; main-branch cache row ignored
        written = json.loads(cache.read_text())
        assert written["branch"] == "keeper"
        assert written["behind"] == 5

    def test_recent_upstream_commits_follows_branch(self, monkeypatch):
        from hermes_cli import web_server

        recorded = []

        def fake_run(cmd, **kwargs):
            recorded.append(list(cmd))
            return _completed(0, "")

        monkeypatch.setattr(web_server.subprocess, "run", fake_run)
        web_server._recent_upstream_commits("keeper")
        assert recorded and any(
            "HEAD..origin/keeper" in part for part in recorded[0])

    def test_resolve_delegate_honors_explicit_branch(self, monkeypatch):
        """main._resolve_update_branch: --branch wins; default follows resolver."""
        from hermes_cli import main as cli_main

        assert cli_main._resolve_update_branch(
            SimpleNamespace(branch="feature-y")) == "feature-y"
        monkeypatch.setenv("HERMES_UPDATE_BRANCH", "keeper")
        monkeypatch.setattr(
            "hermes_cli.update_branch._resolved", None, raising=False)
        assert cli_main._resolve_update_branch(
            SimpleNamespace(branch=None)) == "keeper"
