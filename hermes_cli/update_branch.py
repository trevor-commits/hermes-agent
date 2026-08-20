"""Resolve the branch every update surface measures and pulls against.

One source of truth for "which branch is this install updated from", shared
by the CLI banner, the dashboard check/apply endpoints, `hermes update`'s
--branch default, and the recommended-command strings. Before this module,
each surface hardcoded its own answer (`origin/main` in the banner and
dashboard, `main` as the CLI default, `origin/keeper` in the desktop badge)
and they disagreed on any carried-branch checkout — the nag suggested a bare
`hermes update`, which resolved `main` and refused on a parked branch.

Resolution order (first hit wins):
  1. an explicit ``--branch`` value passed by the caller
  2. ``$HERMES_UPDATE_BRANCH``
  3. the current HEAD branch, when it exists on ``origin`` — probed with
     ``git ls-remote --exit-code --heads``; only a definitive "ref absent"
     (exit code 2) falls through, so a transient network error can never
     silently flip a keeper install to ``main``. Mirrors the desktop's
     ``resolveHealedBranch`` (apps/desktop/electron/main.ts).
  4. ``"main"``

The non-explicit resolution is cached for the process lifetime: callers
(banner, TUI session-info, dashboard) may ask several times per process and
the answer cannot usefully change under a running CLI.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

_resolved: Optional[str] = None


def _repo_dir() -> Path:
    return Path(__file__).parent.parent.resolve()


def _git_stdout(args: list[str], cwd: Path, timeout: float = 5) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(cwd),
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def resolve_update_branch(
    explicit: Optional[str] = None, repo_dir: Optional[Path] = None
) -> str:
    """Return the branch updates should target. Never raises."""
    explicit = (explicit or "").strip()
    if explicit:
        return explicit

    global _resolved
    if repo_dir is None and _resolved is not None:
        return _resolved

    branch = _compute(repo_dir or _repo_dir())
    if repo_dir is None:
        _resolved = branch
    return branch


def _compute(repo_dir: Path) -> str:
    env_branch = (os.environ.get("HERMES_UPDATE_BRANCH") or "").strip()
    if env_branch:
        return env_branch

    if not (repo_dir / ".git").exists():
        return "main"
    head_branch = _git_stdout(["symbolic-ref", "--short", "-q", "HEAD"], repo_dir)
    if not head_branch or head_branch == "main":
        return "main"

    # Probe origin for the branch. Exit 2 = definitively absent -> heal to
    # main. Any other failure (network, auth, timeout) keeps the branch:
    # a flaky connection must not strand a carried-branch install on main.
    try:
        probe = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", head_branch],
            capture_output=True, timeout=10, cwd=str(repo_dir),
        )
    except Exception:
        return head_branch
    if probe.returncode == 2:
        return "main"
    return head_branch


def branch_pinned_update_command(base: str) -> str:
    """Append ``--branch <b>`` to a plain ``hermes update`` suggestion.

    Only the bare git-install command gets pinned: managed/docker/nix/pkg
    guidance strings pass through untouched, and ``main`` stays bare so the
    common case reads clean.
    """
    if base != "hermes update":
        return base
    branch = resolve_update_branch()
    if branch == "main":
        return base
    return f"{base} --branch {branch}"
