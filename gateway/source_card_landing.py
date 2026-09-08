"""Land exact validated source-card bytes through an isolated checkout."""
from __future__ import annotations
import logging
import json
import os
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager as _contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
logger = logging.getLogger("gateway.run")


def _source_card_push_rejected_authenticated(
    porcelain_output: str,
    destination_ref: str,
) -> bool:
    """Report an authenticated concurrent-writer rejection for this exact ref.

    Ported from ``scripts/source-card-commit-push`` in the cards repo, which
    encodes the distinction after analysing 103 capture sessions: only Git's
    machine-readable status for this exact destination proves the server
    refused the push. A generic nonzero may mean the server accepted it before
    the client lost its acknowledgement, so treating that as a safe retry could
    duplicate work.

    Only the parser is reused. The helper itself is not a drop-in here: it
    acquires a GLOBAL checkout mutex and presumes a staged card plus a staged
    ``todo.md``, while this route deliberately lands from an isolated clone
    that stages only the card.
    """
    statuses = 0
    proved = 0
    exact = f"{destination_ref}:refs/heads/main"
    for line in str(porcelain_output or "").splitlines():
        if "\t" not in line:
            continue
        fields = line.split("\t")
        statuses += 1
        if (
            len(fields) >= 3
            and fields[0] == "!"
            and fields[1] == exact
            and _SOURCE_CARD_PUSH_REJECT_RE.fullmatch(fields[2].strip())
        ):
            proved += 1
    return statuses == 1 and proved == 1



def _source_card_safe_landing_detail(detail: Any) -> str:
    """Redact a landing failure before it reaches durable async state."""
    from gateway.run import (
        _redact_gateway_user_facing_secrets,
    )
    safe = _redact_gateway_user_facing_secrets(str(detail or ""))
    safe = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@",
        r"\1[REDACTED]@",
        safe,
    )
    safe = re.sub(
        r"\S*hermes-source-card-landing-\S*",
        "[temporary landing checkout]",
        safe,
    )
    safe = re.sub(
        r"(?:/[^\s:]+)*/touched-source-cards\.[^/\s:]+/",
        "[temporary card validation]/",
        safe,
    )
    return safe.strip()[:800] or "unknown error"



class _SourceCardLandingError(RuntimeError):
    """One deterministic card landing step failed after a draft was available."""

    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = _source_card_safe_landing_detail(detail)
        super().__init__(f"{step}: {self.detail}")



class _SourceCardPostLandingError(_SourceCardLandingError):
    """A card is contained on origin/main, but a later receipt step failed."""

    def __init__(
        self,
        step: str,
        detail: str,
        *,
        path: str,
        commit: str,
    ):
        self.path = path
        self.commit = commit
        super().__init__(step, detail)



class _SourceCardLandingOutcomeUnknownError(_SourceCardLandingError):
    """A push failed ambiguously and remote containment could not be checked."""

    def __init__(
        self,
        step: str,
        detail: str,
        *,
        path: str,
        commit: str,
    ):
        self.path = path
        self.commit = commit
        super().__init__(step, detail)



def _source_card_run_step(
    step: str,
    arguments: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    extra_env: Optional[dict[str, str]] = None,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    from gateway.source_card_prefetch import (
        _source_card_clean_subprocess_env,
    )
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
            env=_source_card_clean_subprocess_env(extra_env),
        )
    except subprocess.TimeoutExpired as exc:
        raise _SourceCardLandingError(
            step,
            f"timeout after {timeout} seconds",
        ) from exc
    except OSError as exc:
        raise _SourceCardLandingError(step, f"launch failed: {exc}") from exc
    if result.returncode not in accepted_returncodes:
        detail = str(result.stderr or result.stdout or "").strip()
        raise _SourceCardLandingError(
            step,
            f"exit {result.returncode}: {detail[:700] or 'no output'}",
        )
    return result



@_contextmanager
def _source_card_isolated_landing_repository(repository: Path):
    """Clone current origin/main without mutating the shared source checkout."""
    remote_output = _source_card_run_step(
        "git_remote",
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        ],
        cwd=repository,
    ).stdout
    remote_urls = [line.strip() for line in remote_output.splitlines() if line.strip()]
    if len(remote_urls) != 1:
        raise _SourceCardLandingError(
            "git_remote",
            "origin must have exactly one push URL",
        )
    remote_url = remote_urls[0]
    parsed_remote = urlsplit(remote_url)
    if parsed_remote.scheme.lower() in {"http", "https"} and (
        parsed_remote.username is not None
        or parsed_remote.password is not None
        or bool(parsed_remote.query)
        or bool(parsed_remote.fragment)
    ):
        raise _SourceCardLandingError(
            "git_remote",
            "origin push URL contains embedded credentials or query parameters",
        )

    identity: dict[str, str] = {}
    for key in ("user.name", "user.email"):
        result = _source_card_run_step(
            "git_identity",
            ["git", "-C", str(repository), "config", "--get", key],
            cwd=repository,
            accepted_returncodes=(0, 1),
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value or "\n" in value or "\r" in value:
            raise _SourceCardLandingError(
                "git_identity",
                f"{key} is missing or ambiguous",
            )
        identity[key] = value

    with tempfile.TemporaryDirectory(prefix="hermes-source-card-landing-") as root:
        checkout = Path(root) / "repo"
        _source_card_run_step(
            "git_clone",
            [
                "git",
                "clone",
                "--no-local",
                "--no-tags",
                "--single-branch",
                "--branch",
                "main",
                "--depth",
                "1",
                "--",
                remote_url,
                str(checkout),
            ],
            cwd=repository,
            # 300s, not 120s: the shallow clone pulls ~40 MiB from GitHub and
            # timed out at 120s on 2026-08-28 during a degraded-network window
            # (same window flapped Telegram polling), killing an otherwise
            # finished card at the landing step.
            timeout=300,
        )
        for key, value in identity.items():
            _source_card_run_step(
                "git_identity",
                ["git", "-C", str(checkout), "config", "--local", key, value],
                cwd=checkout,
            )
        head = _source_card_run_step(
            "git_clone",
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            cwd=checkout,
        ).stdout.strip()
        origin_head = _source_card_run_step(
            "git_clone",
            ["git", "-C", str(checkout), "rev-parse", "origin/main"],
            cwd=checkout,
        ).stdout.strip()
        if head != origin_head:
            raise _SourceCardLandingError(
                "git_clone",
                "isolated checkout does not equal origin/main",
            )
        yield checkout



def _land_source_card(
    *,
    card_path: Path,
    card_content: Optional[str] = None,
    intake_text: str,
    environment: dict[str, str],
    source_message_row_id: int,
) -> dict[str, Any]:
    """Validate, commit, push, receipt, and re-verify one exact card."""
    from gateway.source_card_prefetch import (
        _SOURCE_CARD_WORKER_RESULT_MAX_BYTES,
        _source_card_clean_subprocess_env,
    )
    from gateway.source_card_render import (
        _source_card_fields_and_manifest,
        _source_card_receipt_commands,
        _source_card_render_card_routing,
    )
    cards_root = Path(environment["cards_root"]).resolve(strict=True)
    try:
        candidate_parent = card_path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _SourceCardLandingError(
            "validate", "card parent is unavailable"
        ) from exc
    if candidate_parent != cards_root or card_path.is_symlink():
        raise _SourceCardLandingError("validate", "card path is outside the cards root")
    repository = cards_root.parent
    relative = card_path.relative_to(repository).as_posix()
    shared_card = card_path if card_content is None else None
    if card_content is None:
        try:
            if not card_path.is_file():
                raise OSError("card is not a regular file")
            card_bytes = card_path.read_bytes()
        except OSError as exc:
            raise _SourceCardLandingError(
                "validate", f"card could not be read: {exc}"
            ) from exc
    else:
        if (
            not isinstance(card_content, str)
            or not card_content.strip()
            or "\x00" in card_content
        ):
            raise _SourceCardLandingError(
                "validate", "card_content is empty or unsafe"
            )
        card_bytes = card_content.encode("utf-8")
        if len(card_bytes) > _SOURCE_CARD_WORKER_RESULT_MAX_BYTES:
            raise _SourceCardLandingError(
                "validate", "card_content exceeded the configured byte limit"
            )
    # Bytes exactly as read from the shared checkout, kept so the read-time
    # TOCTOU guard below still compares like with like after rendering.
    shared_source_bytes = card_bytes if shared_card is not None else None
    # Render the token-only routing fields here, not only in the draft
    # finalizer, so the duplicate path cannot land un-rendered prose. The
    # duplicate path runs no model turn at all, so every model-output guard is
    # skipped for it by construction.
    try:
        rendered_bytes = _source_card_render_card_routing(
            card_bytes.decode("utf-8")
        ).encode("utf-8")
    except UnicodeDecodeError as exc:
        raise _SourceCardLandingError(
            "validate", "card is not valid UTF-8"
        ) from exc
    validator = Path(environment["source_card_validator"]).resolve(strict=True)
    try:
        validator_relative = validator.relative_to(repository)
    except ValueError as exc:
        raise _SourceCardLandingError(
            "validate", "source-card validator is outside the cards repository"
        ) from exc
    writer_home = str(Path(environment["decision_writer"]).parent.parent)

    with _SOURCE_CARD_LANDING_LOCK:
        tracked = _source_card_run_step(
            "git_clean",
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "--error-unmatch",
                "--",
                relative,
            ],
            cwd=repository,
            accepted_returncodes=(0, 1),
        ).returncode == 0
        if tracked:
            dirty = _source_card_run_step(
                "git_clean",
                [
                    "git",
                    "-C",
                    str(repository),
                    "diff",
                    "--quiet",
                    "HEAD",
                    "--",
                    relative,
                ],
                cwd=repository,
                accepted_returncodes=(0, 1),
            ).returncode == 1
            if dirty:
                raise _SourceCardLandingError(
                    "git_clean",
                    "dirty tracked card requires operator reconciliation",
                )
            if rendered_bytes != card_bytes:
                # Already committed and outside the validator's routing
                # grammar. Rewriting another actor's landed card during an
                # unrelated intake is out of scope, so fail closed and name it.
                raise _SourceCardLandingError(
                    "validate",
                    "tracked card is outside the validator routing grammar; "
                    "operator reconciliation required",
                )
        card_bytes = rendered_bytes
        with _source_card_isolated_landing_repository(repository) as landing_repository:
            landing_cards_root = landing_repository / cards_root.name
            if landing_cards_root.exists():
                if landing_cards_root.is_symlink() or not landing_cards_root.is_dir():
                    raise _SourceCardLandingError(
                        "git_clone",
                        "isolated cards root is not a regular directory",
                    )
            else:
                try:
                    landing_cards_root.mkdir(mode=0o755)
                except OSError as exc:
                    raise _SourceCardLandingError(
                        "git_clone",
                        f"could not create isolated cards root: {exc}",
                    ) from exc
            landing_card = landing_repository / relative
            if landing_card.exists():
                if landing_card.is_symlink() or not landing_card.is_file():
                    raise _SourceCardLandingError(
                        "git_sync",
                        "origin/main card path is not a regular file",
                    )
                if landing_card.read_bytes() != card_bytes:
                    raise _SourceCardLandingError(
                        "git_sync",
                        "origin/main already contains a different card",
                    )
            else:
                if tracked:
                    raise _SourceCardLandingError(
                        "git_clean",
                        "tracked card is absent from origin/main",
                    )
                try:
                    landing_card.write_bytes(card_bytes)
                except OSError as exc:
                    raise _SourceCardLandingError(
                        "git_clone",
                        f"could not write isolated card: {exc}",
                    ) from exc

            landing_validator = landing_repository / validator_relative
            if (
                landing_validator.is_symlink()
                or not landing_validator.is_file()
                or not os.access(landing_validator, os.X_OK)
            ):
                raise _SourceCardLandingError(
                    "validate",
                    "isolated source-card validator is unavailable",
                )
            # Receipt parsing and strict touched-card validation both operate on
            # the exact immutable candidate that may be committed. A rejected
            # model draft never appears in the shared checkout.
            _source_card_fields_and_manifest(landing_card)
            _source_card_run_step(
                "validate",
                [str(landing_validator), "--card", relative, "--no-full-backlog"],
                cwd=landing_repository,
                timeout=120,
            )
            if landing_card.read_bytes() != card_bytes:
                raise _SourceCardLandingError(
                    "validate",
                    "isolated card bytes changed during validation",
                )
            if (
                shared_card is not None
                and shared_card.read_bytes() != shared_source_bytes
            ):
                raise _SourceCardLandingError(
                    "validate",
                    "shared card bytes changed during validation",
                )

            _source_card_run_step(
                "git_add",
                [
                    "git",
                    "-C",
                    str(landing_repository),
                    "add",
                    "--",
                    relative,
                ],
                cwd=landing_repository,
            )
            changed = _source_card_run_step(
                "git_diff",
                [
                    "git",
                    "-C",
                    str(landing_repository),
                    "diff",
                    "--cached",
                    "--quiet",
                    "HEAD",
                    "--",
                    relative,
                ],
                cwd=landing_repository,
                accepted_returncodes=(0, 1),
            ).returncode == 1
            if changed:
                _source_card_run_step(
                    "git_commit",
                    [
                        "git",
                        "-C",
                        str(landing_repository),
                        "commit",
                        "--only",
                        "-m",
                        f"docs(research): capture {card_path.stem}",
                        "--",
                        relative,
                    ],
                    cwd=landing_repository,
                    timeout=120,
                )
            commit = _source_card_run_step(
                "git_commit",
                ["git", "-C", str(landing_repository), "rev-parse", "HEAD"],
                cwd=landing_repository,
            ).stdout.strip()
            push_error: Optional[_SourceCardLandingError] = None
            # Sync onto the current origin/main BEFORE pushing. The starred-repo
            # drain pushes card commits to this same branch every ~5 minutes, so
            # origin routinely advances inside the clone->validate->commit->push
            # window. Two things then break: the push is stale, and — because the
            # landing clone is `--depth 1` — the machine's global pre-push
            # credential gate cannot resolve `remote_sha..local_sha` and fails
            # the push with a generic error carrying no porcelain status at all.
            # Fetching the new tip (not unshallowing) is enough: after rebase the
            # gate's range is the card commit, which is present locally. A full
            # `--unshallow` of this research repo (~2400 commits, >1 GiB)
            # exceeds the 120s step budget (live 2026-08-18 AYi_AInotes intake).
            _source_card_run_step(
                "git_push_sync",
                ["git", "-C", str(landing_repository), "fetch", "origin", "main"],
                cwd=landing_repository,
                timeout=120,
            )
            rebase = _source_card_run_step(
                "git_push_sync",
                ["git", "-C", str(landing_repository), "rebase", "origin/main"],
                cwd=landing_repository,
                timeout=120,
                accepted_returncodes=(0, 1, 128),
            )
            if rebase.returncode != 0:
                _source_card_run_step(
                    "git_push_sync",
                    ["git", "-C", str(landing_repository), "rebase", "--abort"],
                    cwd=landing_repository,
                    timeout=60,
                    accepted_returncodes=(0, 1, 128),
                )
                raise _SourceCardLandingError(
                    "git_push_sync",
                    "could not rebase the card commit onto current origin/main",
                )
            commit = _source_card_run_step(
                "git_commit",
                ["git", "-C", str(landing_repository), "rev-parse", "HEAD"],
                cwd=landing_repository,
            ).stdout.strip()
            # Only an AUTHENTICATED rejection for this exact destination is safe
            # to retry: a generic nonzero may mean the server accepted the push
            # before the client lost its acknowledgement.
            for attempt in range(_SOURCE_CARD_PUSH_MAX_ATTEMPTS):
                push_error = None
                try:
                    push_result = _source_card_run_step(
                        "git_push",
                        [
                            "git",
                            "-C",
                            str(landing_repository),
                            "push",
                            "--porcelain",
                            "origin",
                            "HEAD:refs/heads/main",
                        ],
                        cwd=landing_repository,
                        timeout=120,
                        accepted_returncodes=(0, 1),
                    )
                except _SourceCardLandingError as exc:
                    # A timeout or launch failure is precisely the ambiguous
                    # case: the server may have accepted the push before the
                    # client lost its acknowledgement. Never retry it.
                    push_error = exc
                    break
                if push_result.returncode == 0:
                    break
                combined = f"{push_result.stdout or ''}\n{push_result.stderr or ''}"
                push_error = _SourceCardLandingError(
                    "git_push",
                    _source_card_safe_landing_detail(combined) or "push rejected",
                )
                if attempt == _SOURCE_CARD_PUSH_MAX_ATTEMPTS - 1:
                    break
                if not _source_card_push_rejected_authenticated(
                    push_result.stdout or "", "HEAD"
                ):
                    break
                logger.info(
                    "Source-card push rejected by a concurrent writer; "
                    "rebasing onto origin/main (attempt %d)",
                    attempt + 1,
                )
                try:
                    _source_card_run_step(
                        "git_push_retry",
                        ["git", "-C", str(landing_repository), "fetch", "origin", "main"],
                        cwd=landing_repository,
                        timeout=120,
                    )
                    _source_card_run_step(
                        "git_push_retry",
                        [
                            "git",
                            "-C",
                            str(landing_repository),
                            "rebase",
                            "origin/main",
                        ],
                        cwd=landing_repository,
                        timeout=120,
                    )
                except _SourceCardLandingError:
                    break
                commit = _source_card_run_step(
                    "git_commit",
                    ["git", "-C", str(landing_repository), "rev-parse", "HEAD"],
                    cwd=landing_repository,
                ).stdout.strip()

            try:
                _source_card_run_step(
                    "git_verify",
                    [
                        "git",
                        "-C",
                        str(landing_repository),
                        "fetch",
                        "origin",
                        "main",
                    ],
                    cwd=landing_repository,
                    timeout=60,
                )
                ancestry = _source_card_run_step(
                    "git_verify",
                    [
                        "git",
                        "-C",
                        str(landing_repository),
                        "merge-base",
                        "--is-ancestor",
                        commit,
                        "origin/main",
                    ],
                    cwd=landing_repository,
                    accepted_returncodes=(0, 1),
                )
            except _SourceCardLandingError as exc:
                detail = f"remote verification failed: {exc.step}: {exc.detail}"
                step = "git_verify"
                if push_error is not None:
                    step = "git_push"
                    detail = f"{push_error.detail}; {detail}"
                raise _SourceCardLandingOutcomeUnknownError(
                    step,
                    detail,
                    path=relative,
                    commit=commit,
                ) from exc

            if ancestry.returncode != 0:
                if push_error is not None:
                    raise push_error
                raise _SourceCardLandingOutcomeUnknownError(
                    "git_verify",
                    "push reported success but origin/main does not contain the commit",
                    path=relative,
                    commit=commit,
                )

            try:
                try:
                    committed = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(landing_repository),
                            "show",
                            f"origin/main:{relative}",
                        ],
                        cwd=landing_repository,
                        timeout=30,
                        capture_output=True,
                        check=False,
                        env=_source_card_clean_subprocess_env(),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise _SourceCardLandingError(
                        "git_verify",
                        f"card byte verification failed: {exc}",
                    ) from exc
                if committed.returncode != 0 or committed.stdout != card_bytes:
                    raise _SourceCardLandingError(
                        "git_verify",
                        "origin/main card bytes do not match the validated card",
                    )
                receipt_environment = dict(environment)
                receipt_environment["cards_root"] = str(landing_cards_root)
                receipt_commands = _source_card_receipt_commands(
                    card_path=landing_card,
                    commit=commit,
                    intake_text=intake_text,
                    environment=receipt_environment,
                    source_message_row_id=source_message_row_id,
                )
                receipt_results: list[dict[str, Any]] = []
                for command in receipt_commands:
                    result = _source_card_run_step(
                        "receipt",
                        command,
                        cwd=landing_repository,
                        timeout=180,
                        extra_env={"HERMES_HOME": writer_home},
                    )
                    try:
                        receipt_results.append(json.loads(result.stdout))
                    except (TypeError, ValueError) as exc:
                        raise _SourceCardLandingError(
                            "receipt",
                            "decision writer returned invalid JSON",
                        ) from exc
                _source_card_run_step(
                    "git_verify",
                    [
                        "git",
                        "-C",
                        str(landing_repository),
                        "fetch",
                        "origin",
                        "main",
                    ],
                    cwd=landing_repository,
                    timeout=60,
                )
                _source_card_run_step(
                    "git_verify",
                    [
                        "git",
                        "-C",
                        str(landing_repository),
                        "merge-base",
                        "--is-ancestor",
                        commit,
                        "origin/main",
                    ],
                    cwd=landing_repository,
                )
            except _SourceCardLandingError as exc:
                raise _SourceCardPostLandingError(
                    exc.step,
                    exc.detail,
                    path=relative,
                    commit=commit,
                ) from exc
            return {
                "path": relative,
                "commit": commit,
                "receipt_results": receipt_results,
            }



_SOURCE_CARD_LANDING_LOCK = threading.Lock()



_SOURCE_CARD_PUSH_MAX_ATTEMPTS = 4



_SOURCE_CARD_PUSH_REJECT_RE = re.compile(
    r"^\[rejected\] \((stale info|fetch first|non-fast-forward)\)$"
)
