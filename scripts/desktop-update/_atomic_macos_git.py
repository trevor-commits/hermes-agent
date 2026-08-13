"""Isolated Git candidate preparation for the macOS atomic updater."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Dict, Optional

from _atomic_macos_candidate import (
    reserve_source_candidate as _reserve_source_candidate,
    validate_reserved_candidate as _validate_reserved_candidate,
)
from _atomic_macos_transaction import (
    PathLike,
    TransactionReceipt,
    _redact_sensitive_text,
)


_EXACT_COMMIT_RE = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_GIT_TIMEOUT_SECONDS = 600
_GIT_EXECUTABLE = "/usr/bin/git"


class KeeperConflictError(RuntimeError):
    """The first conflicting keeper commit stopped candidate preparation."""

    def __init__(self, commit: str, subject: str, unmerged_paths: list) -> None:
        self.commit = commit
        self.subject = subject
        self.unmerged_paths = tuple(unmerged_paths)
        super().__init__(
            "keeper commit {} ({}) conflicts in: {}".format(
                commit,
                subject,
                ", ".join(unmerged_paths),
            )
        )


class GitCommandError(RuntimeError):
    """A sanitized Git subprocess failure safe for receipts and callers."""


def _git_environment() -> Dict[str, str]:
    inherited_names = (
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_PROXY",
        "SSH_AUTH_SOCK",
        "TMPDIR",
        "USER",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    )
    environment = {
        key: os.environ[key]
        for key in inherited_names
        if key in os.environ
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_COMMITTER_NAME": "Hermes Safe Updater",
            "GIT_COMMITTER_EMAIL": "hermes-updater@localhost.invalid",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }
    )
    return environment


def _run_git(
    arguments: list,
    *,
    repository: Optional[Path] = None,
    check: bool = True,
    text: bool = True,
    sensitive_values: tuple = (),
) -> subprocess.CompletedProcess:
    command = [_GIT_EXECUTABLE, "-c", "core.hooksPath=/dev/null"]
    if repository is not None:
        command.extend(["-C", str(repository)])
    command.extend(arguments)
    try:
        return subprocess.run(
            command,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr or error.stdout or str(error)
        safe_command = " ".join(
            _redact_sensitive_text(
                os.fsdecode(argument),
                sensitive_values=sensitive_values,
            )
            for argument in error.cmd
        )
        raise GitCommandError(
            "git command failed (exit {}): {}: {}".format(
                error.returncode,
                safe_command,
                _redact_sensitive_text(
                    os.fsdecode(detail),
                    sensitive_values=sensitive_values,
                ),
            )
        ) from None
    except subprocess.TimeoutExpired as error:
        safe_command = " ".join(
            _redact_sensitive_text(
                os.fsdecode(argument),
                sensitive_values=sensitive_values,
            )
            for argument in error.cmd
        )
        raise GitCommandError(
            "git command timed out after {} seconds: {}".format(
                error.timeout,
                safe_command,
            )
        ) from None
    except OSError as error:
        safe_command = " ".join(
            _redact_sensitive_text(
                os.fsdecode(argument),
                sensitive_values=sensitive_values,
            )
            for argument in command
        )
        raise GitCommandError(
            "git command could not start: {}: {}".format(
                safe_command,
                _redact_sensitive_text(
                    str(error),
                    sensitive_values=sensitive_values,
                ),
            )
        ) from None


def _git_output(
    repository: Path,
    *arguments: str,
    _run_git_command=None,
) -> str:
    runner = _run_git if _run_git_command is None else _run_git_command
    return runner(list(arguments), repository=repository).stdout.strip()


def _worktree_digest(repository: Path, *, _run_git_command=None) -> str:
    runner = _run_git if _run_git_command is None else _run_git_command
    listed = runner(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        repository=repository,
        text=False,
    ).stdout
    digest = hashlib.sha256()
    for encoded_relative in sorted(part for part in listed.split(b"\0") if part):
        relative = os.fsdecode(encoded_relative)
        path = repository / relative
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        digest.update(stat.S_IFMT(file_stat.st_mode).to_bytes(8, "big"))
        digest.update(stat.S_IMODE(file_stat.st_mode).to_bytes(8, "big"))
        if stat.S_ISLNK(file_stat.st_mode):
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(file_stat.st_mode):
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        else:
            digest.update(b"non-regular")
    return digest.hexdigest()


def _checkout_snapshot(repository: Path, *, _run_git_command=None) -> Dict[str, Any]:
    runner = _run_git if _run_git_command is None else _run_git_command
    root_stat = repository.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("live checkout must be a real directory")
    head = _git_output(
        repository,
        "rev-parse",
        "HEAD^{commit}",
        _run_git_command=runner,
    )
    tree = _git_output(
        repository,
        "rev-parse",
        "HEAD^{tree}",
        _run_git_command=runner,
    )
    status_output = runner(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        repository=repository,
    ).stdout
    index_bytes = runner(
        ["ls-files", "--stage", "-z"],
        repository=repository,
        text=False,
    ).stdout
    return {
        "path": str(repository),
        "st_dev": root_stat.st_dev,
        "st_ino": root_stat.st_ino,
        "st_uid": root_stat.st_uid,
        "st_mode": stat.S_IFMT(root_stat.st_mode) | stat.S_IMODE(root_stat.st_mode),
        "head": head,
        "status": status_output,
        "status_digest": hashlib.sha256(status_output.encode("utf-8")).hexdigest(),
        "index_digest": hashlib.sha256(index_bytes).hexdigest(),
        "tree_digest": tree,
        "worktree_digest": _worktree_digest(
            repository,
            _run_git_command=runner,
        ),
    }


def _record_candidate_failure(
    receipt: TransactionReceipt,
    *,
    phase: str,
    code: str,
    message: str,
    live_before: Optional[Dict[str, Any]],
    live_after: Optional[Dict[str, Any]],
    candidate_path: Optional[Path],
    **updates: Any,
) -> None:
    unchanged = live_before is not None and live_before == live_after
    status = "failed_unchanged" if unchanged else "manual_recovery_required"
    receipt.finish(
        status,
        phase=phase,
        switched=False,
        rolled_back=False,
        no_live_mutation=unchanged,
        failure_code=code,
        failure_message=message,
        live_before=live_before,
        live_after=live_after,
        candidate_path=(str(candidate_path) if candidate_path is not None else None),
        **updates,
    )


def prepare_keeper_candidate(
    live_checkout: PathLike,
    official_upstream: PathLike,
    target_commit: str,
    *,
    receipt: TransactionReceipt,
    _run_git_command=None,
) -> Path:
    """Clone and rebase keepers without using the live checkout as a workspace."""
    if receipt.is_terminal:
        raise RuntimeError("cannot prepare a candidate using a terminal receipt")

    live = Path(os.path.abspath(os.fspath(live_checkout)))
    raw_runner = _run_git if _run_git_command is None else _run_git_command
    sensitive_upstream = str(official_upstream)

    def runner(arguments: list, **kwargs: Any):
        inherited_sensitive = tuple(kwargs.pop("sensitive_values", ()))
        kwargs["sensitive_values"] = inherited_sensitive + (sensitive_upstream,)
        return raw_runner(arguments, **kwargs)

    def git_output(repository: Path, *arguments: str) -> str:
        return _git_output(
            repository,
            *arguments,
            _run_git_command=runner,
        )

    def checkout_snapshot(repository: Path) -> Dict[str, Any]:
        return _checkout_snapshot(repository, _run_git_command=runner)

    candidate: Optional[Path] = None
    live_before: Optional[Dict[str, Any]] = None
    live_after: Optional[Dict[str, Any]] = None

    if not isinstance(target_commit, str) or not _EXACT_COMMIT_RE.fullmatch(
        target_commit
    ):
        try:
            live_before = checkout_snapshot(live)
            live_after = checkout_snapshot(live)
        except (OSError, ValueError, subprocess.SubprocessError, GitCommandError):
            pass
        _record_candidate_failure(
            receipt,
            phase="candidate_target_invalid",
            code="invalid_target_commit",
            message="target must be an exact 40- or 64-hex commit id",
            live_before=live_before,
            live_after=live_after,
            candidate_path=None,
        )
        raise ValueError("target must be an exact commit id")

    target = target_commit.lower()
    try:
        live_before = checkout_snapshot(live)
        candidate = _reserve_source_candidate(
            live,
            receipt=receipt,
            live_before=live_before,
        )
        receipt.record_phase(
            "candidate_clone_intent",
            live_before=live_before,
            candidate_path=str(candidate),
            target_commit=target,
        )
        _validate_reserved_candidate(live, receipt=receipt)
        runner(["clone", "--no-local", "--", str(live), str(candidate)])
        _validate_reserved_candidate(live, receipt=receipt)
        git_directory_stat = (candidate / ".git").lstat()
        if stat.S_ISLNK(git_directory_stat.st_mode) or not stat.S_ISDIR(
            git_directory_stat.st_mode
        ):
            raise RuntimeError("candidate clone is not an independent Git repository")

        receipt.record_phase(
            "candidate_cloned",
            live_before=live_before,
            candidate_path=str(candidate),
            target_commit=target,
        )
        runner(
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--force",
                "--",
                str(official_upstream),
                "{}:refs/hermes-update/target".format(target),
            ],
            repository=candidate,
        )
        fetched_target = git_output(
            candidate,
            "rev-parse",
            "refs/hermes-update/target^{commit}",
        ).lower()
        if fetched_target != target:
            raise RuntimeError(
                "official upstream fetch resolved to {}, expected {}".format(
                    fetched_target,
                    target,
                )
            )

        original_head = live_before["head"]
        keeper_base = git_output(candidate, "merge-base", original_head, target)
        original_commits_output = git_output(
            candidate,
            "rev-list",
            "--reverse",
            "{}..{}".format(keeper_base, original_head),
        )
        original_commits = (
            original_commits_output.splitlines() if original_commits_output else []
        )
        original_subjects = [
            git_output(candidate, "show", "-s", "--format=%s", commit)
            for commit in original_commits
        ]
        keeper = {
            "base_commit": keeper_base,
            "target_commit": target,
            "official_upstream": _redact_sensitive_text(
                sensitive_upstream,
                sensitive_values=(sensitive_upstream,),
            ),
            "original_commits": original_commits,
            "original_subjects": original_subjects,
        }
        receipt.record_phase(
            "keeper_rebase_intent",
            live_before=live_before,
            candidate_path=str(candidate),
            keeper=keeper,
        )

        rebase = runner(
            [
                "rebase",
                "--rebase-merges",
                "--reapply-cherry-picks",
                "--empty=keep",
                "--onto",
                target,
                keeper_base,
            ],
            repository=candidate,
            check=False,
        )
        if rebase.returncode != 0:
            conflict_commit = git_output(
                candidate,
                "rev-parse",
                "REBASE_HEAD^{commit}",
            )
            conflict_subject = git_output(
                candidate,
                "show",
                "-s",
                "--format=%s",
                conflict_commit,
            )
            unmerged_output = runner(
                ["diff", "--name-only", "--diff-filter=U", "-z"],
                repository=candidate,
            ).stdout
            unmerged_paths = sorted(
                path for path in unmerged_output.split("\0") if path
            )
            runner(["rebase", "--abort"], repository=candidate)
            if git_output(candidate, "rev-parse", "HEAD^{commit}") != original_head:
                raise RuntimeError(
                    "candidate rebase abort did not restore its original HEAD"
                )
            if runner(
                ["diff", "--name-only", "--diff-filter=U"],
                repository=candidate,
            ).stdout:
                raise RuntimeError("candidate rebase abort left unmerged paths")

            live_after = checkout_snapshot(live)
            conflict = {
                "commit": conflict_commit,
                "subject": conflict_subject,
                "unmerged_paths": unmerged_paths,
            }
            _record_candidate_failure(
                receipt,
                phase="keeper_conflict",
                code="keeper_conflict",
                message="keeper commit conflicts with the exact upstream target",
                live_before=live_before,
                live_after=live_after,
                candidate_path=candidate,
                keeper=keeper,
                keeper_conflict=conflict,
            )
            raise KeeperConflictError(
                conflict_commit,
                conflict_subject,
                unmerged_paths,
            )

        rebased_output = git_output(
            candidate,
            "rev-list",
            "--reverse",
            "{}..HEAD".format(target),
        )
        rebased_commits = rebased_output.splitlines() if rebased_output else []
        rebased_subjects = [
            git_output(candidate, "show", "-s", "--format=%s", commit)
            for commit in rebased_commits
        ]
        if len(rebased_commits) != len(original_commits):
            raise RuntimeError("keeper rebase did not preserve every keeper commit")
        if rebased_subjects != original_subjects:
            raise RuntimeError("keeper rebase changed keeper order or subjects")
        keeper.update(
            {
                "rebased_commits": rebased_commits,
                "rebased_subjects": rebased_subjects,
            }
        )

        live_after = checkout_snapshot(live)
        if live_after != live_before:
            _record_candidate_failure(
                receipt,
                phase="live_checkout_changed",
                code="live_checkout_changed",
                message="live checkout changed during isolated candidate preparation",
                live_before=live_before,
                live_after=live_after,
                candidate_path=candidate,
                keeper=keeper,
            )
            raise RuntimeError("live checkout changed during candidate preparation")

        receipt.record_phase(
            "candidate_prepared",
            live_before=live_before,
            live_after=live_after,
            candidate_path=str(candidate),
            keeper=keeper,
        )
        return candidate
    except KeeperConflictError:
        raise
    except BaseException as error:
        if not receipt.is_terminal:
            try:
                live_after = checkout_snapshot(live)
            except (OSError, ValueError, subprocess.SubprocessError, GitCommandError):
                live_after = None
            failure_code = getattr(
                error,
                "failure_code",
                "candidate_preparation_failed",
            )
            failure_phase = getattr(
                error,
                "failure_phase",
                "candidate_preparation_failed",
            )
            failure_candidate = candidate or getattr(error, "candidate_path", None)
            failure_updates = {}
            candidate_artifact = getattr(error, "candidate_artifact", None)
            if candidate_artifact is not None:
                failure_updates["candidate_artifact"] = candidate_artifact
            _record_candidate_failure(
                receipt,
                phase=failure_phase,
                code=failure_code,
                message=str(error),
                live_before=live_before,
                live_after=live_after,
                candidate_path=failure_candidate,
                **failure_updates,
            )
        raise
