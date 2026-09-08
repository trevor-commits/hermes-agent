"""Bounded evidence and trusted-path reads for the existing gateway intake route."""
from __future__ import annotations
import concurrent.futures
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from gateway.platforms.event import MessageEvent
logger = logging.getLogger("gateway.run")


class _SourceCardPrefetchError(RuntimeError):
    """A bounded X prefetch failed before any worker was dispatched."""



def _source_card_require_path(
    path: Path,
    *,
    label: str,
    directory: bool = False,
    executable: bool = False,
) -> Path:
    """Resolve one trusted worker path while rejecting symlinks and type drift."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"source-card {label} is unavailable: {exc}") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(before.st_mode) or not expected_type(before.st_mode):
        kind = "directory" if directory else "file"
        raise RuntimeError(f"source-card {label} is not a regular {kind}")
    resolved = path.resolve(strict=True)
    try:
        after = resolved.stat()
    except OSError as exc:
        raise RuntimeError(f"source-card {label} changed during validation") from exc
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError(f"source-card {label} changed during validation")
    if executable and not os.access(resolved, os.X_OK):
        raise RuntimeError(f"source-card {label} is not executable")
    return resolved



def _source_card_read_utf8(path: Path, *, label: str, max_bytes: int) -> str:
    """Read one bounded regular UTF-8 file without following a leaf symlink."""
    resolved = _source_card_require_path(path, label=label)
    try:
        before = resolved.stat()
        if before.st_size > max_bytes:
            raise RuntimeError(
                f"source-card {label} exceeds {max_bytes} UTF-8 bytes"
            )
        data = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise RuntimeError(f"source-card {label} could not be read: {exc}") from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(data) != after.st_size
    ):
        raise RuntimeError(f"source-card {label} changed while being read")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"source-card {label} is not valid UTF-8") from exc



def _resolve_source_card_worker_environment(
    event: "MessageEvent",
    source: Any,
    session_entry: Any,
    *,
    hermes_home: Optional[Path] = None,
    require_x_lookup: bool = True,
) -> dict[str, str]:
    """Resolve exact trusted paths and origin identifiers for one worker."""
    from gateway.run import (
        _gateway_config_home,
    )
    home = Path(hermes_home or _gateway_config_home()).expanduser().resolve(strict=True)
    config_path = home / "state" / "research-decision-config.json"
    raw_config = _source_card_read_utf8(
        config_path,
        label="research decision config",
        max_bytes=64_000,
    )
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("source-card research decision config is invalid JSON") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RuntimeError("source-card research decision config schema is invalid")

    cards_value = config.get("cards_root")
    transcript_value = config.get("transcript_db")
    if not isinstance(cards_value, str) or not Path(cards_value).is_absolute():
        raise RuntimeError("source-card cards root is not an absolute path")
    if not isinstance(transcript_value, str) or not Path(transcript_value).is_absolute():
        raise RuntimeError("source-card transcript database is not an absolute path")
    cards_root = _source_card_require_path(
        Path(cards_value), label="cards root", directory=True
    )
    decision_writer = _source_card_require_path(
        home / "scripts" / "hermes-research-decisions",
        label="decision writer",
        executable=True,
    )
    new_source_card = _source_card_require_path(
        cards_root.parent / "scripts" / "new-source-card",
        label="source-card template helper",
        executable=True,
    )
    source_card_validator = _source_card_require_path(
        cards_root.parent / "scripts" / "validate-touched-source-cards",
        label="source-card validator",
        executable=True,
    )
    transcript_db = _source_card_require_path(
        Path(transcript_value), label="transcript database"
    )
    source_chat_id = str(getattr(source, "chat_id", "") or "").strip()
    parent_session_id = str(getattr(session_entry, "session_id", "") or "").strip()
    platform_message_id = str(getattr(event, "message_id", "") or "").strip()
    if not source_chat_id or not parent_session_id or not platform_message_id:
        raise RuntimeError("source-card origin identity is incomplete")
    environment = {
        "cards_root": str(cards_root),
        "decision_writer": str(decision_writer),
        "new_source_card": str(new_source_card),
        "source_card_validator": str(source_card_validator),
        "transcript_db": str(transcript_db),
        "source_chat_id": source_chat_id,
        "source_thread_id": str(getattr(source, "thread_id", "") or ""),
        "parent_session_id": parent_session_id,
        "platform_message_id": platform_message_id,
    }
    if require_x_lookup:
        x_lookup = _source_card_require_path(
            cards_root.parent / "scripts" / "x-lookup",
            label="X lookup helper",
            executable=True,
        )
        environment["x_lookup"] = str(x_lookup)
    return environment



def _source_card_normalized_text(
    value: Any,
    *,
    label: str,
    max_chars: int,
    optional: bool = True,
) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > max_chars:
        raise _SourceCardPrefetchError(f"exit 0: invalid {label} in x-lookup JSON")
    return value



def _source_card_normalized_single_line(
    value: Any,
    *,
    label: str,
    max_chars: int,
    optional: bool = True,
) -> Optional[str]:
    """Bound untrusted text that may later appear in Markdown structure."""
    normalized = _source_card_normalized_text(
        value,
        label=label,
        max_chars=max_chars,
        optional=optional,
    )
    if normalized is None:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
        raise _SourceCardPrefetchError(f"exit 0: invalid {label} in x-lookup JSON")
    return normalized



def _normalize_source_card_x_post(payload: Any, status_id: str) -> dict[str, Any]:
    """Allowlist and bound the untrusted normalized JSON from x-lookup."""
    if not isinstance(payload, dict) or str(payload.get("id") or "") != status_id:
        raise _SourceCardPrefetchError("exit 0: mismatched status ID in x-lookup JSON")
    author_raw = payload.get("author")
    stats_raw = payload.get("stats")
    if not isinstance(author_raw, dict) or not isinstance(stats_raw, dict):
        raise _SourceCardPrefetchError("exit 0: invalid author or stats in x-lookup JSON")

    author_handle = _source_card_normalized_single_line(
        author_raw.get("handle"), label="author handle", max_chars=32
    )
    if author_handle is not None and not re.fullmatch(
        r"@?[A-Za-z0-9_]{1,32}", author_handle
    ):
        raise _SourceCardPrefetchError(
            "exit 0: invalid author handle in x-lookup JSON"
        )
    author = {
        "handle": author_handle,
        "name": _source_card_normalized_single_line(
            author_raw.get("name"), label="author name", max_chars=256
        ),
        "followers": author_raw.get("followers")
        if type(author_raw.get("followers")) is int
        else None,
    }
    stats = {
        key: stats_raw.get(key) if type(stats_raw.get(key)) is int else None
        for key in ("likes", "retweets", "replies", "views")
    }

    def _http_url(
        value: Any,
        label: str,
        *,
        optional: bool = False,
    ) -> Optional[str]:
        normalized = _source_card_normalized_text(
            value,
            label=f"{label} URL",
            max_chars=2_048,
            optional=optional,
        )
        if normalized is None:
            return None
        if normalized != normalized.strip() or any(
            ord(char) <= 0x20 or ord(char) == 0x7F for char in normalized
        ):
            raise _SourceCardPrefetchError(
                f"exit 0: invalid {label} URL in x-lookup JSON"
            )
        try:
            parsed = urlsplit(normalized)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise _SourceCardPrefetchError(
                f"exit 0: invalid {label} URL in x-lookup JSON"
            ) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise _SourceCardPrefetchError(
                f"exit 0: invalid {label} URL in x-lookup JSON"
            )
        return normalized

    def _url_list(value: Any, label: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 32:
            raise _SourceCardPrefetchError(f"exit 0: invalid {label} in x-lookup JSON")
        result = []
        for item in value:
            normalized = _http_url(item, label)
            result.append(normalized or "")
        return result

    quote_raw = payload.get("quote")
    quote = None
    if quote_raw is not None:
        if not isinstance(quote_raw, dict):
            raise _SourceCardPrefetchError("exit 0: invalid quote in x-lookup JSON")
        quote = {
            "author": _source_card_normalized_single_line(
                quote_raw.get("author"), label="quote author", max_chars=128
            ),
            "text": _source_card_normalized_text(
                quote_raw.get("text"), label="quote text", max_chars=4_000
            ),
            "url": _http_url(quote_raw.get("url"), "quote", optional=True),
        }
    return {
        "status_id": status_id,
        "canonical_url": f"https://x.com/i/status/{status_id}",
        "author": author,
        "text": _source_card_normalized_text(
            payload.get("text"), label="post text", max_chars=8_000
        ),
        "created_at": _source_card_normalized_single_line(
            payload.get("created_at"), label="creation time", max_chars=128
        ),
        "stats": stats,
        "links": _url_list(payload.get("links"), "links"),
        "media": _url_list(payload.get("media"), "media"),
        "quote": quote,
    }



def _prefetch_source_card_x_posts(intake_text: str, x_lookup: Path) -> list[dict]:
    """Fetch each distinct X status once through the bounded local helper."""
    statuses: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _SOURCE_CARD_X_STATUS_RE.finditer(intake_text):
        status_id = match.group(1)
        if status_id not in seen:
            seen.add(status_id)
            statuses.append((status_id, f"https://x.com/i/status/{status_id}"))
    if len(statuses) > _SOURCE_CARD_X_STATUS_MAX_COUNT:
        raise _SourceCardPrefetchError(
            f"too many X posts: maximum {_SOURCE_CARD_X_STATUS_MAX_COUNT}"
        )

    def _fetch_one(status_id: str, url: str) -> dict:
        try:
            result = subprocess.run(
                [str(x_lookup), "--json", url],
                timeout=_SOURCE_CARD_NETWORK_PREFETCH_TIMEOUT,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _SourceCardPrefetchError(
                f"timeout after {_SOURCE_CARD_NETWORK_PREFETCH_TIMEOUT} seconds"
            ) from exc
        except OSError as exc:
            raise _SourceCardPrefetchError(f"launch failed: {exc}") from exc
        if result.returncode != 0:
            reason = next(
                (
                    line.strip()
                    for line in str(result.stderr or "").splitlines()
                    if line.strip()
                ),
                "x-lookup failed",
            )
            raise _SourceCardPrefetchError(
                f"exit {result.returncode}: {reason[:240]}"
            )
        if len(str(result.stdout or "").encode("utf-8")) > 64_000:
            raise _SourceCardPrefetchError("exit 0: x-lookup JSON exceeded 64000 bytes")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise _SourceCardPrefetchError("exit 0: invalid x-lookup JSON") from exc
        post = _normalize_source_card_x_post(payload, status_id)
        if len(json.dumps(post, ensure_ascii=False).encode("utf-8")) > 12_000:
            raise _SourceCardPrefetchError(
                "exit 0: normalized x-lookup JSON exceeded 12000 bytes"
            )
        return post

    if len(statuses) <= 1:
        posts = [_fetch_one(*item) for item in statuses]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(statuses),
            thread_name_prefix="source-card-x-prefetch",
        ) as pool:
            futures = [pool.submit(_fetch_one, *item) for item in statuses]
            posts = [future.result() for future in futures]
    if len(json.dumps(posts, ensure_ascii=False).encode("utf-8")) > 24_000:
        raise _SourceCardPrefetchError(
            "exit 0: combined normalized x-lookup JSON exceeded 24000 bytes"
        )
    return posts



def _source_card_github_owner_name(value: str) -> Optional[str]:
    """Return one safe GitHub owner/repository identity from an exact URL."""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repository = parts[:2]
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    component = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
    reserved = {
        "about", "account", "apps", "codespaces", "collections", "contact",
        "enterprise", "events", "explore", "features", "issues", "marketplace",
        "new", "notifications", "organizations", "orgs", "pricing", "pulls",
        "search", "security", "settings", "site", "sponsors", "stars", "topics",
        "trending",
    }
    if (
        owner.casefold() in reserved
        or not component.fullmatch(owner)
        or not component.fullmatch(repository)
    ):
        return None
    return f"{owner}/{repository}"



def _source_card_github_repositories(
    prefetched_x_posts: list[dict],
    intake_text: str = "",
) -> tuple[list[str], list[str]]:
    """Collect unique GitHub repos; compact-prefetch only the first four."""
    candidates: list[str] = []
    for post in prefetched_x_posts:
        links = post.get("links") if isinstance(post, dict) else None
        if isinstance(links, list):
            candidates.extend(str(link) for link in links if isinstance(link, str))
    candidates.extend(
        match.group(0).rstrip(".,;:!?)]}")
        for match in _SOURCE_CARD_URL_RE.finditer(intake_text)
    )
    repositories: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        owner_name = _source_card_github_owner_name(candidate)
        if owner_name is None:
            continue
        identity = owner_name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        repositories.append(owner_name)
    kept = repositories[:_SOURCE_CARD_GITHUB_REPO_MAX_COUNT]
    omitted = repositories[_SOURCE_CARD_GITHUB_REPO_MAX_COUNT:]
    return kept, omitted



def _source_card_github_prefetch_bound_note(omitted_owner_names: list[str]) -> str:
    """Name compact-prefetch overflow so a roundup post still starts a worker."""
    if not omitted_owner_names:
        return ""
    return (
        "GITHUB PREFETCH BOUND (TRUSTED TEXT)\n"
        f"truncated: {len(omitted_owner_names)}\n"
        "omitted owner/names: "
        + ", ".join(omitted_owner_names)
        + "\n"
    )



def _normalize_source_card_github_compact(
    text: str,
    owner_name: str,
) -> dict[str, str]:
    """Parse the bounded compact helper output without trusting its fields."""
    allowed = {
        "repo",
        "url",
        "stars",
        "forks",
        "license",
        "created",
        "last_push",
        "head",
        "open_issues",
        "advisories",
        "release",
        "default_branch",
        "description",
        "topics",
        "languages",
        "signal",
    }
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        key, separator, value = raw_line.partition(":")
        key = key.strip()
        value = value.strip()
        if (
            not separator
            or key not in allowed
            or key in fields
            or not value
            or len(value) > 4_000
            or any(ord(character) < 0x20 and character != "\t" for character in value)
        ):
            raise _SourceCardPrefetchError(
                f"exit 0: invalid GitHub compact field for {owner_name}"
            )
        fields[key] = value
    required = {"repo", "url", "head", "license", "signal"}
    if not required.issubset(fields):
        raise _SourceCardPrefetchError(
            f"exit 0: incomplete GitHub compact fields for {owner_name}"
        )
    if fields["repo"].casefold() != owner_name.casefold():
        raise _SourceCardPrefetchError(
            f"exit 0: mismatched GitHub repository for {owner_name}"
        )
    url_owner_name = _source_card_github_owner_name(fields["url"])
    if url_owner_name is None or url_owner_name.casefold() != owner_name.casefold():
        raise _SourceCardPrefetchError(
            f"exit 0: invalid GitHub URL for {owner_name}"
        )
    return fields



def _prefetch_source_card_github_repositories(
    prefetched_x_posts: list[dict],
    source_card_prefetch: Path,
    *,
    intake_text: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the compact GitHub helper once per kept distinct repository."""
    repositories, omitted = _source_card_github_repositories(
        prefetched_x_posts,
        intake_text,
    )
    if omitted:
        logger.info(
            "Source-card GitHub prefetch truncated: %s omitted owner/names: %s",
            len(omitted),
            ", ".join(omitted),
        )
    if not repositories:
        return [], omitted
    helper = _source_card_require_path(
        Path(source_card_prefetch),
        label="GitHub prefetch helper",
        executable=True,
    )

    def _fetch_one(owner_name: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [str(helper), owner_name, "--compact"],
                timeout=_SOURCE_CARD_NETWORK_PREFETCH_TIMEOUT,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _SourceCardPrefetchError(
                f"GitHub prefetch timeout after "
                f"{_SOURCE_CARD_NETWORK_PREFETCH_TIMEOUT} seconds: {owner_name}"
            ) from exc
        except OSError as exc:
            raise _SourceCardPrefetchError(
                f"GitHub prefetch launch failed for {owner_name}: {exc}"
            ) from exc
        if result.returncode != 0:
            reason = next(
                (
                    line.strip()
                    for line in str(result.stderr or result.stdout or "").splitlines()
                    if line.strip()
                ),
                "source-card-prefetch failed",
            )
            raise _SourceCardPrefetchError(
                f"GitHub prefetch exit {result.returncode} for "
                f"{owner_name}: {reason[:240]}"
            )
        encoded = str(result.stdout or "").encode("utf-8")
        if len(encoded) > _SOURCE_CARD_GITHUB_COMPACT_MAX_BYTES:
            raise _SourceCardPrefetchError(
                f"GitHub prefetch output exceeded "
                f"{_SOURCE_CARD_GITHUB_COMPACT_MAX_BYTES} bytes: {owner_name}"
            )
        return {
            "owner_name": owner_name,
            "canonical_url": f"https://github.com/{owner_name}",
            "fields": _normalize_source_card_github_compact(
                str(result.stdout or ""),
                owner_name,
            ),
        }

    if len(repositories) == 1:
        prefetched = [_fetch_one(repositories[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(repositories),
            thread_name_prefix="source-card-github-prefetch",
        ) as pool:
            futures = [pool.submit(_fetch_one, item) for item in repositories]
            prefetched = [future.result() for future in futures]
    if (
        len(json.dumps(prefetched, ensure_ascii=False).encode("utf-8"))
        > _SOURCE_CARD_GITHUB_COMBINED_MAX_BYTES
    ):
        raise _SourceCardPrefetchError(
            "combined normalized GitHub prefetch exceeded "
            f"{_SOURCE_CARD_GITHUB_COMBINED_MAX_BYTES} bytes"
        )
    return prefetched, omitted



def _prefetch_source_card_template(new_source_card: Path) -> str:
    """Load one trusted generic strict-card skeleton from the repository helper."""
    from gateway.source_card_render import (
        _source_card_placeholder_defaults,
    )
    helper = _source_card_require_path(
        Path(new_source_card),
        label="source-card template helper",
        executable=True,
    )
    try:
        result = subprocess.run(
            [str(helper), "gateway/source-card", "--stdout"],
            timeout=10,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _SourceCardPrefetchError(
            "source-card template timeout after 10 seconds"
        ) from exc
    except OSError as exc:
        raise _SourceCardPrefetchError(
            f"source-card template launch failed: {exc}"
        ) from exc
    if result.returncode != 0:
        reason = next(
            (
                line.strip()
                for line in str(result.stderr or result.stdout or "").splitlines()
                if line.strip()
            ),
            "new-source-card failed",
        )
        raise _SourceCardPrefetchError(
            f"source-card template exit {result.returncode}: {reason[:240]}"
        )
    template = str(result.stdout or "")
    if (
        not template.strip()
        or len(template.encode("utf-8")) > _SOURCE_CARD_TEMPLATE_MAX_BYTES
        or template.count("- url:") != 1
        or template.count("- owner/name:") != 1
        or template.count("- by:") != 1
    ):
        raise _SourceCardPrefetchError(
            "source-card template output is missing the strict card skeleton"
        )
    lines = template.rstrip().splitlines()
    if not lines or not lines[0].startswith("# "):
        raise _SourceCardPrefetchError(
            "source-card template output is missing the strict card title"
        )
    lines[0] = "# Source card from gateway-prefetched evidence"
    for index, line in enumerate(lines):
        field = re.fullmatch(r"- ([^:]+):\s*(.*)", line)
        if field and (
            field.group(1).strip().lower() in {"url", "owner/name"}
            or not field.group(2).strip()
            or field.group(2).strip().upper().startswith("TODO:")
        ):
            lines[index] = (
                f"- {field.group(1)}: "
                + _source_card_placeholder_defaults(
                    prefetched_x_posts=[],
                    prefetched_github_repositories=[],
                ).get(
                    field.group(1).strip().lower(),
                    "not verified from gateway-prefetched evidence",
                )
            )
    neutral = "\n".join(lines).rstrip()
    if "## Decision manifest (ER-278)" not in neutral:
        neutral += (
            "\n\n## Decision manifest (ER-278)\n"
            "- no-decision-reason: watch-only"
        )
    neutral = re.sub(
        r"(?i)\bTODO:\s*",
        "not verified: ",
        neutral,
    )
    if (
        len(neutral.encode("utf-8")) > _SOURCE_CARD_TEMPLATE_MAX_BYTES
        or neutral.count("## Decision manifest (ER-278)") != 1
        or "gateway/source-card" in neutral
        or re.search(r"(?i)\bTODO:", neutral)
    ):
        raise _SourceCardPrefetchError(
            "source-card template could not be made subject-neutral"
        )
    return neutral + "\n"



def _source_card_duplicate_identifiers(intake_text: str) -> list[str]:
    """Return ordered unique X status IDs and non-X URLs for exact lookup."""
    identifiers: list[str] = []
    seen: set[str] = set()
    for match in _SOURCE_CARD_URL_RE.finditer(intake_text):
        url = match.group(0).rstrip(".,;:!?)]}")
        x_match = _SOURCE_CARD_X_STATUS_RE.search(url)
        identifier = x_match.group(1) if x_match else url
        if identifier and identifier not in seen:
            seen.add(identifier)
            identifiers.append(identifier)
    return identifiers



def _source_card_clean_subprocess_env(
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    blocked = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    environment = {
        key: value for key, value in os.environ.items() if key not in blocked
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    if extra:
        environment.update(extra)
    return environment



def _source_card_duplicate_lookup(
    intake_text: str,
    cards_root: Path,
) -> tuple[list[Path], list[str]]:
    """Run one exact fixed-string rg lookup before any model is constructed."""
    identifiers = _source_card_duplicate_identifiers(intake_text)
    if not identifiers:
        raise _SourceCardPrefetchError("source-card intake has no duplicate identifier")
    arguments = ["rg", "-l", "-F"]
    for identifier in identifiers:
        arguments.extend(("-e", identifier))
    arguments.extend(("--", str(cards_root)))
    try:
        result = subprocess.run(
            arguments,
            timeout=10,
            capture_output=True,
            text=True,
            check=False,
            env=_source_card_clean_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _SourceCardPrefetchError(
            "duplicate lookup timeout after 10 seconds"
        ) from exc
    except OSError as exc:
        raise _SourceCardPrefetchError(
            f"duplicate lookup launch failed: {exc}"
        ) from exc
    if result.returncode not in {0, 1}:
        detail = str(result.stderr or result.stdout or "").strip()
        raise _SourceCardPrefetchError(
            f"duplicate lookup exit {result.returncode}: "
            f"{detail[:240] or 'no output'}"
        )
    if len(str(result.stdout or "").encode("utf-8")) > 64_000:
        raise _SourceCardPrefetchError("duplicate lookup output exceeded 64000 bytes")
    root = cards_root.resolve(strict=True)
    matches: list[Path] = []
    seen: set[Path] = set()
    for line in str(result.stdout or "").splitlines():
        raw = Path(line.strip())
        candidate = raw if raw.is_absolute() else cards_root.parent / raw
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _SourceCardPrefetchError(
                "duplicate lookup returned a missing card"
            ) from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise _SourceCardPrefetchError(
                "duplicate lookup returned an unsafe card path"
            ) from exc
        if candidate.is_symlink() or not resolved.is_file():
            raise _SourceCardPrefetchError(
                "duplicate lookup returned an unsafe card path"
            )
        if (
            resolved.parent != root
            or resolved.suffix.lower() != ".md"
            or resolved.name == "README.md"
        ):
            continue
        if resolved not in seen:
            seen.add(resolved)
            matches.append(resolved)
    return matches, arguments



def _source_card_message_row_id(
    transcript_db: Path,
    source_session: str,
    platform_message_id: str,
) -> int:
    """Resolve exactly one durable user row for the current intake."""
    try:
        database = transcript_db.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("source-card transcript database is unavailable") from exc
    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            rows = connection.execute(
                """
                SELECT id, role
                FROM messages
                WHERE session_id = ? AND platform_message_id = ?
                ORDER BY id DESC
                LIMIT 2
                """,
                (source_session, platform_message_id),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"source-card transcript row lookup failed: {exc}"
        ) from exc
    if len(rows) != 1 or rows[0][1] != "user" or type(rows[0][0]) is not int:
        raise RuntimeError(
            "source-card intake requires exactly one durable user message row"
        )
    return rows[0][0]



def _source_card_worker_reference_context(skill_dir: Path) -> str:
    """Load the exact three source-controlled worker references once."""
    root = _source_card_require_path(
        Path(skill_dir), label="skill directory", directory=True
    )
    sections = []
    total_bytes = 0
    for relative in _SOURCE_CARD_WORKER_REFERENCES:
        body = _source_card_read_utf8(
            root / relative,
            label=f"worker reference {relative}",
            max_bytes=_SOURCE_CARD_WORKER_REFERENCE_TOTAL_MAX_BYTES,
        )
        total_bytes += len(body.encode("utf-8"))
        if total_bytes > _SOURCE_CARD_WORKER_REFERENCE_TOTAL_MAX_BYTES:
            raise RuntimeError(
                "source-card worker references exceed 5000 UTF-8 bytes"
            )
        sections.append(
            f"--- {relative} START ---\n{body.rstrip()}\n--- {relative} END ---"
        )
    return (
        "\n\n[TRUSTED PRELOADED SOURCE-CARD REFERENCES]\n"
        "These exact files are already attached. Do not call tools to read them again.\n"
        + "\n\n".join(sections)
    )



_SOURCE_CARD_INTAKE_ROUTE = "source-card-intake"



_SOURCE_CARD_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)



_SOURCE_CARD_WORKER_MAX_ITERATIONS = 2



_SOURCE_CARD_WORKER_SYSTEM_MAX_BYTES = 24_576



_SOURCE_CARD_WORKER_GOAL_MAX_BYTES = 16_384



_SOURCE_CARD_WORKER_RESULT_MAX_BYTES = 32_768



_SOURCE_CARD_WORKER_RESPONSE_TARGET_BYTES = 30_000



_SOURCE_CARD_NETWORK_PREFETCH_TIMEOUT = 25



_SOURCE_CARD_WORKER_TOOLSETS: tuple[str, ...] = ()



_SOURCE_CARD_WORKER_REFERENCES = (
    "references/research-method.md",
    "references/card-schema.md",
    "references/receipts-and-ledger.md",
)



_SOURCE_CARD_WORKER_REFERENCE_TOTAL_MAX_BYTES = 5_000



_SOURCE_CARD_X_STATUS_MAX_COUNT = 8



_SOURCE_CARD_GITHUB_REPO_MAX_COUNT = 4



_SOURCE_CARD_GITHUB_COMPACT_MAX_BYTES = 16_000



_SOURCE_CARD_GITHUB_COMBINED_MAX_BYTES = 32_000



_SOURCE_CARD_TEMPLATE_MAX_BYTES = 16_000



_SOURCE_CARD_X_STATUS_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/"
    r"(?:i/(?:web/)?status|[^/?#]+/status)/(\d+)",
    re.IGNORECASE,
)
