"""Validate and render source-card drafts and their receipt arguments."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Optional


def _source_card_candidate_path(cards_root: Path, raw_path: str) -> Path:
    """Validate one new top-level lowercase source-card destination."""
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise _SourceCardLandingError(
            "worker_output",
            "card_path must be an absolute path",
        )
    candidate = Path(raw_path)
    root = cards_root.resolve(strict=True)
    if (
        candidate.parent.resolve(strict=True) != root
        or candidate != root / candidate.name
    ):
        raise _SourceCardLandingError(
            "worker_output",
            "card_path must be directly inside the configured cards root",
        )
    if (
        candidate.name == "README.md"
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.md", candidate.name) is None
    ):
        raise _SourceCardLandingError(
            "worker_output",
            "card_path must use one lowercase flat Markdown filename",
        )
    return candidate



def _source_card_typed_target_slug(item: str) -> str:
    """Return one validator-legal slug, or reject the typed target.

    Live 2026-08-18 DAIEvolutionHub: the worker returned `copilotkit/aimock`.
    The card validator only accepts ``[a-z0-9][a-z0-9-]*``, so owner/name is
    stored as owner-name. Random punctuation is still rejected.
    """
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    value = item.strip().lower()
    if _SOURCE_CARD_DOWNSTREAM_TARGET_RE.fullmatch(value):
        return value
    collapsed = re.sub(r"[/.]+", "-", value).strip("-")
    if collapsed != value and _SOURCE_CARD_DOWNSTREAM_TARGET_RE.fullmatch(collapsed):
        return collapsed
    raise _SourceCardLandingError(
        "worker_output",
        "analysis.downstream_learning_targets contains an invalid slug",
    )



def _source_card_load_worker_json(final_response: str) -> Any:
    """Load the worker JSON object, ignoring a leading non-JSON prefix.

    Live 2026-08-18 DAIEvolutionHub: DeepSeek prefixed a complete card JSON
    with a Referenced Chat block because the packet contains parent_session_id.
    """
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    if not isinstance(final_response, str):
        raise _SourceCardLandingError(
            "worker_output",
            "worker did not return valid JSON",
        )
    text = final_response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?[ \t]*\n?", "", text, count=1)
        text = re.sub(r"\n?```[ \t]*$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    if start < 0:
        raise _SourceCardLandingError(
            "worker_output",
            "worker did not return valid JSON",
        )
    try:
        payload, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError as exc:
        raise _SourceCardLandingError(
            "worker_output",
            "worker did not return valid JSON",
        ) from exc
    if text[end:].strip():
        raise _SourceCardLandingError(
            "worker_output",
            "worker did not return valid JSON",
        )
    return payload



def _source_card_typed_analysis(raw: Any) -> Optional[dict[str, Any]]:
    """Validate the worker's typed routing decision, or reject it outright.

    Rendering can only salvage a readable leading token from prose. A value
    such as `probably worth a look someday` has none, so the routing decision
    is taken as typed data instead: an enum and a slug list, checked here
    against the validator's grammar and never guessed at.
    """
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "hermes_relevance",
        "downstream_learning_targets",
    }:
        raise _SourceCardLandingError(
            "worker_output",
            "analysis must contain only hermes_relevance and "
            "downstream_learning_targets",
        )
    relevance = raw.get("hermes_relevance")
    if not isinstance(relevance, str):
        raise _SourceCardLandingError(
            "worker_output", "analysis.hermes_relevance must be a string"
        )
    relevance = relevance.strip()
    none_match = _SOURCE_CARD_NONE_PREFIX_RE.fullmatch(relevance)
    if not none_match and relevance not in _SOURCE_CARD_HERMES_RELEVANCE_VALUES:
        raise _SourceCardLandingError(
            "worker_output",
            "analysis.hermes_relevance must be direct, adjacent, "
            "upgrade-candidate, or none: <reason>",
        )
    targets = raw.get("downstream_learning_targets")
    if not isinstance(targets, list) or len(targets) > 16:
        raise _SourceCardLandingError(
            "worker_output",
            "analysis.downstream_learning_targets must be a list of 0-16 slugs",
        )
    slugs: list[str] = []
    for item in targets:
        if not isinstance(item, str):
            raise _SourceCardLandingError(
                "worker_output",
                "analysis.downstream_learning_targets contains an invalid slug",
            )
        slug = _source_card_typed_target_slug(item)
        if slug not in slugs:
            slugs.append(slug)
    none_match = _SOURCE_CARD_NONE_PREFIX_RE.fullmatch(relevance)
    if not slugs:
        # Live 2026-08-18 natebjones intake: DeepSeek returned a correct
        # `none: <reason>` decision with `[]`. That is a valid "no repo"
        # routing value, not a missing field. Enum relevance still needs
        # the documented bare `hermes` token.
        if none_match:
            return {
                "hermes relevance": f"none: {none_match.group(1).strip()}",
                "downstream learning targets": [],
            }
        if relevance in _SOURCE_CARD_HERMES_RELEVANCE_VALUES:
            return {
                "hermes relevance": relevance,
                "downstream learning targets": ["hermes"],
            }
        raise _SourceCardLandingError(
            "worker_output",
            "analysis.downstream_learning_targets must be a list of 0-16 slugs",
        )
    return {"hermes relevance": relevance, "downstream learning targets": slugs}



def _source_card_apply_typed_analysis(
    content: str,
    analysis: dict[str, Any],
) -> str:
    """Overwrite the card's routing field lines from the typed decision."""
    relevance = str(analysis["hermes relevance"])
    slugs = analysis["downstream learning targets"]
    if slugs:
        rendered_targets = ", ".join(slugs)
    elif relevance.lower().startswith("none:"):
        rendered_targets = relevance
    else:
        rendered_targets = "hermes"
    rendered = {
        "hermes relevance": relevance,
        "downstream learning targets": rendered_targets,
    }
    field_re = re.compile(
        r"(?im)^(-\s*(hermes relevance|downstream learning targets)\s*:\s*)(.*)$"
    )

    def _substitute(match: "re.Match[str]") -> str:
        return match.group(1) + rendered[match.group(2).strip().lower()]

    return field_re.sub(_substitute, content)



def _parse_source_card_worker_draft(
    final_response: str,
    cards_root: Path,
) -> tuple[Path, str]:
    """Parse one no-tool worker response into a bounded new-card draft."""
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    from gateway.source_card_prefetch import (
        _SOURCE_CARD_WORKER_RESULT_MAX_BYTES,
    )
    if len(final_response.encode("utf-8")) > _SOURCE_CARD_WORKER_RESULT_MAX_BYTES:
        raise _SourceCardLandingError(
            "worker_output",
            "worker JSON exceeded the configured byte limit",
        )
    payload = _source_card_load_worker_json(final_response)
    if not isinstance(payload, dict) or not {"card_path", "card_content"} <= set(
        payload
    ) or set(payload) - {"card_path", "card_content", "analysis"}:
        raise _SourceCardLandingError(
            "worker_output",
            "worker JSON must contain only card_path, card_content and analysis",
        )
    analysis = _source_card_typed_analysis(payload.get("analysis"))
    path = _source_card_candidate_path(cards_root, payload.get("card_path"))
    content = payload.get("card_content")
    if (
        not isinstance(content, str)
        or not content.strip()
        or "\x00" in content
        or len(content.encode("utf-8")) > _SOURCE_CARD_WORKER_RESULT_MAX_BYTES
    ):
        raise _SourceCardLandingError(
            "worker_output",
            "card_content is empty, unsafe, or oversized",
        )
    if content.count("## Decision manifest (ER-278)") != 1:
        raise _SourceCardLandingError(
            "worker_output",
            "card_content must contain exactly one ER-278 decision manifest",
        )
    if path.exists() or path.is_symlink():
        raise _SourceCardLandingError(
            "write",
            "worker attempted to overwrite an existing card",
        )
    if analysis is not None:
        content = _source_card_apply_typed_analysis(content, analysis)
    return path, content



def _source_card_placeholder_defaults(
    *,
    prefetched_x_posts: list[dict[str, Any]],
    prefetched_github_repositories: list[dict[str, Any]],
) -> dict[str, str]:
    """Return evidence-safe values for fields a worker could not verify."""
    unavailable = "not verified from gateway-prefetched evidence"
    defaults = {
        "url": unavailable,
        "owner/name": unavailable,
        "drift risk": "high: unresolved facts require verification before use",
        "freshness threshold": "refresh before any trust, pilot, or adoption decision",
        "current pinned sha": "n/a (no Git revision was prefetched)",
        "execution/pilot status": (
            "NOT installed, run, built, tested, or connected; no credentials or "
            "private data were sent"
        ),
        "license/use boundary": (
            "not verified from gateway-prefetched evidence; no repository license "
            "was available"
        ),
        "security policy / vulnerability reporting": (
            "not inspected; no verified repository was prefetched"
        ),
        "credential and secret surfaces": (
            "none identified in gateway-prefetched evidence; not inspected"
        ),
        "network/api/webhook/tunnel/browser surface": (
            "none identified in gateway-prefetched evidence; not inspected"
        ),
        "local file/config mutation surface": (
            "none identified in gateway-prefetched evidence; not inspected"
        ),
        "sensitive data classes": (
            "none identified in gateway-prefetched evidence; not inspected"
        ),
        "install/run blast radius": (
            "high until the referenced artifact and execution behavior are verified"
        ),
        "risk signal": (
            "insufficient evidence for a code or runtime safety assessment"
        ),
        "disposition": "watch-until: missing source evidence is verified",
        "downstream learning targets": (
            "none: no verified implementation target was prefetched"
        ),
        "hermes relevance": (
            "none: no verified Hermes integration surface was prefetched"
        ),
        "by": "Hermes gateway source-card worker",
    }

    github = prefetched_github_repositories[0] if prefetched_github_repositories else None
    if isinstance(github, dict):
        owner_name = str(github.get("owner_name") or "").strip()
        canonical_url = str(github.get("canonical_url") or "").strip()
        fields = github.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        if owner_name:
            defaults["owner/name"] = owner_name
        if canonical_url:
            defaults["url"] = canonical_url
        head = str(fields.get("head") or "").strip()
        if head:
            defaults["current pinned sha"] = head
        license_value = str(fields.get("license") or "").strip()
        if license_value:
            defaults["license/use boundary"] = (
                f"{license_value} in prefetched GitHub metadata; file-level and "
                "dependency terms were not verified"
            )

    post = prefetched_x_posts[0] if prefetched_x_posts else None
    if isinstance(post, dict):
        status_id = str(post.get("status_id") or "").strip()
        canonical_url = str(post.get("canonical_url") or "").strip()
        author = post.get("author")
        author = author if isinstance(author, dict) else {}
        handle = str(author.get("handle") or "").strip().lstrip("@").lower()
        if canonical_url and defaults["url"] == unavailable:
            defaults["url"] = canonical_url
        if status_id and defaults["owner/name"] == unavailable:
            safe_handle = re.sub(r"[^a-z0-9_.-]+", "-", handle).strip("-._")
            defaults["owner/name"] = f"{safe_handle or 'x-post'}/x-{status_id}"
            defaults["current pinned sha"] = (
                "n/a (social-source card; stable X status ID "
                f"{status_id}; no Git revision was prefetched)"
            )
            defaults["license/use boundary"] = (
                "not verified from gateway-prefetched evidence; no repository "
                "license was available"
            )
    return defaults



def _source_card_selected_github_repository(
    content: str,
    prefetched_github_repositories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind a multi-repository draft to exactly one prefetched subject."""
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    from gateway.source_card_prefetch import (
        _SOURCE_CARD_X_STATUS_RE,
    )
    if len(prefetched_github_repositories) <= 1:
        return prefetched_github_repositories

    field_values: dict[str, set[str]] = {"url": set(), "owner/name": set()}
    for line in content.splitlines():
        field = re.fullmatch(r"- ([^:]+):\s*(.*)", line)
        if not field:
            continue
        field_name = field.group(1).strip().lower()
        if field_name in field_values:
            value = field.group(2).strip()
            if value:
                field_values[field_name].add(value)
    if any(len(values) > 1 for values in field_values.values()):
        raise _SourceCardLandingError(
            "worker_output",
            "card draft identified conflicting prefetched GitHub repositories",
        )

    draft_url = next(iter(field_values["url"]), "").rstrip("/")
    draft_owner = next(iter(field_values["owner/name"]), "").casefold()
    matches: list[dict[str, Any]] = []
    for repository in prefetched_github_repositories:
        owner_name = str(repository.get("owner_name") or "").strip()
        canonical_url = str(repository.get("canonical_url") or "").strip()
        if (
            draft_owner
            and owner_name
            and draft_owner == owner_name.casefold()
        ) or (
            draft_url
            and canonical_url
            and draft_url == canonical_url.rstrip("/")
        ):
            matches.append(repository)
    if len(matches) == 1:
        return matches
    if _SOURCE_CARD_X_STATUS_RE.search(draft_url):
        return []
    raise _SourceCardLandingError(
        "worker_output",
        "card draft did not identify exactly one prefetched GitHub repository",
    )



def _source_card_selected_x_post(
    content: str,
    prefetched_x_posts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind a multi-post social draft to exactly one prefetched status."""
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    if len(prefetched_x_posts) <= 1:
        return prefetched_x_posts

    field_values: dict[str, set[str]] = {"url": set(), "owner/name": set()}
    for line in content.splitlines():
        field = re.fullmatch(r"- ([^:]+):\s*(.*)", line)
        if not field:
            continue
        field_name = field.group(1).strip().lower()
        if field_name in field_values:
            value = field.group(2).strip()
            if value:
                field_values[field_name].add(value)
    if any(len(values) > 1 for values in field_values.values()):
        raise _SourceCardLandingError(
            "worker_output",
            "card draft identified conflicting prefetched X posts",
        )

    draft_url = next(iter(field_values["url"]), "").rstrip("/")
    draft_owner = next(iter(field_values["owner/name"]), "").casefold()
    matches: list[dict[str, Any]] = []
    for post in prefetched_x_posts:
        status_id = str(post.get("status_id") or "").strip()
        canonical_url = str(post.get("canonical_url") or "").strip()
        owner_suffix = f"/x-{status_id}".casefold() if status_id else ""
        if (
            draft_url
            and canonical_url
            and draft_url == canonical_url.rstrip("/")
        ) or (
            draft_owner
            and owner_suffix
            and draft_owner.endswith(owner_suffix)
        ):
            matches.append(post)
    if len(matches) != 1:
        raise _SourceCardLandingError(
            "worker_output",
            "card draft did not identify exactly one prefetched X post",
        )
    return matches



def _source_card_render_routing_fields(
    relevance: str,
    targets: str,
) -> tuple[str, str]:
    """Render the two token-only routing fields into the validator's grammar.

    The grammar is closed: `hermes relevance` is `direct`, `adjacent`,
    `upgrade-candidate`, or `none: <reason>`, and every downstream target is a
    ``[a-z0-9][a-z0-9-]*`` slug unless the whole value is `none: <reason>`.

    Prose is removed by reading the LEADING token of each value, not by
    matching a separator. The previous separator-shaped form only stripped
    text after `:`, ` - `, or `. `, so `adjacent because ...`,
    `adjacent (...)`, and `Adjacent, ...` all reached the validator intact and
    each cost one live intake. Anything with no readable leading token is
    returned untouched so the validator reports it, rather than being guessed.

    The cross-field rule is enforced here as well: an enum relevance requires
    the bare `hermes` target, and a `none:` relevance forbids it.
    """
    rendered_relevance = str(relevance or "").strip()
    none_match = _SOURCE_CARD_NONE_PREFIX_RE.fullmatch(rendered_relevance)
    if none_match:
        rendered_relevance = f"none: {none_match.group(1).strip()}"
    else:
        for token in _SOURCE_CARD_HERMES_RELEVANCE_VALUES:
            if re.match(
                rf"{re.escape(token)}(?![a-z0-9-])",
                rendered_relevance,
                flags=re.IGNORECASE,
            ):
                rendered_relevance = token
                break

    raw_targets = str(targets or "").strip()
    targets_none = _SOURCE_CARD_NONE_PREFIX_RE.fullmatch(raw_targets)
    slugs: list[str] = []
    if not targets_none:
        for chunk in raw_targets.split(","):
            match = _SOURCE_CARD_DOWNSTREAM_TARGET_RE.match(chunk.strip().lower())
            if match and match.group(0) not in slugs:
                slugs.append(match.group(0))

    is_enum = rendered_relevance in _SOURCE_CARD_HERMES_RELEVANCE_VALUES
    is_none = rendered_relevance.lower().startswith("none:")
    if is_enum:
        if "hermes" in slugs:
            slugs.remove("hermes")
        slugs.insert(0, "hermes")
    elif is_none and "hermes" in slugs:
        slugs.remove("hermes")

    if targets_none and not is_enum:
        rendered_targets = f"none: {targets_none.group(1).strip()}"
    elif slugs:
        rendered_targets = ", ".join(slugs)
    elif is_none:
        reason = rendered_relevance.split(":", 1)[1].strip() or "no downstream target"
        rendered_targets = f"none: {reason}"
    else:
        rendered_targets = raw_targets
    return rendered_relevance, rendered_targets



def _source_card_render_card_routing(content: str) -> str:
    """Apply :func:`_source_card_render_routing_fields` to one card's text.

    Called on every landing path \u2014 generated draft and pre-existing duplicate
    alike \u2014 so a card can never reach the validator with prose in a token-only
    field just because no model turn ran for it.
    """
    field_re = re.compile(r"(?im)^(-\s*(hermes relevance|downstream learning targets)\s*:\s*)(.*)$")
    found: dict[str, str] = {}
    for match in field_re.finditer(content):
        found.setdefault(match.group(2).strip().lower(), match.group(3).strip())
    if not found:
        return content
    rendered_relevance, rendered_targets = _source_card_render_routing_fields(
        found.get("hermes relevance", ""),
        found.get("downstream learning targets", ""),
    )
    rendered = {
        "hermes relevance": rendered_relevance,
        "downstream learning targets": rendered_targets,
    }

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(2).strip().lower()
        value = rendered.get(name)
        if value is None or name not in found:
            return match.group(0)
        return match.group(1) + value

    return field_re.sub(_substitute, content)



def _source_card_normalize_routing_field(field_name: str, value: str) -> str:
    """Render one routing field in isolation, preserving the other."""
    if field_name == "hermes relevance":
        return _source_card_render_routing_fields(value, "hermes")[0]
    if field_name == "downstream learning targets":
        return _source_card_render_routing_fields("adjacent", value)[1]
    return value



def _source_card_repair_expanded_fields(
    content: str,
    defaults: dict[str, str],
) -> str:
    """Repair live worker field names the expanded-schema validator requires.

    Live 2026-08-18 DAIEvolutionHub: the card used `freshness trigger` and
    `watch-until <reason>` without the required colon after `watch-until`.
    """
    seen: set[str] = set()
    output: list[str] = []
    for line in content.splitlines():
        field = re.fullmatch(r"(- ([^:]+):\s*)(.*)", line)
        if field:
            name = field.group(2).strip().lower()
            value = field.group(3).strip()
            if name == "freshness trigger":
                name = "freshness threshold"
                line = f"- freshness threshold: {value}"
            if name == "disposition":
                lower = value.lower()
                if not lower.startswith(_SOURCE_CARD_APPROVED_DISPOSITION_PREFIXES):
                    if lower.startswith("watch-until"):
                        rest = value.split("watch-until", 1)[-1].lstrip(" :-")
                        rest = (
                            rest
                            or "a specific inspection or date is recorded"
                        )
                        line = f"- disposition: watch-until: {rest}"
                    elif re.match(r"^(watch|defer|monitor)\b", lower):
                        line = f"- disposition: watch-until: {value}"
            seen.add(name)
        output.append(line)
    if "freshness threshold" not in seen:
        threshold = defaults.get(
            "freshness threshold",
            "refresh before any trust, pilot, or adoption decision",
        )
        insert_at = next(
            (
                index
                for index, line in enumerate(output)
                if line.strip() == "## Decision manifest (ER-278)"
            ),
            len(output),
        )
        output.insert(insert_at, f"- freshness threshold: {threshold}")
    return "\n".join(output).rstrip() + "\n"



def _finalize_source_card_worker_draft(
    *,
    card_path: Path,
    content: str,
    prefetched_x_posts: list[dict[str, Any]],
    prefetched_github_repositories: list[dict[str, Any]],
) -> str:
    """Replace known template residue with bounded evidence-safe statements."""
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    from gateway.source_card_prefetch import (
        _SOURCE_CARD_WORKER_RESULT_MAX_BYTES,
    )
    selected_github_repositories = _source_card_selected_github_repository(
        content,
        prefetched_github_repositories,
    )
    selected_x_posts = (
        []
        if selected_github_repositories
        else _source_card_selected_x_post(content, prefetched_x_posts)
    )
    defaults = _source_card_placeholder_defaults(
        prefetched_x_posts=selected_x_posts,
        prefetched_github_repositories=selected_github_repositories,
    )
    neutral_defaults = _source_card_placeholder_defaults(
        prefetched_x_posts=[],
        prefetched_github_repositories=[],
    )
    unavailable = "not verified from gateway-prefetched evidence"
    post = selected_x_posts[0] if selected_x_posts else {}
    status_id = str(post.get("status_id") or "").strip() if isinstance(post, dict) else ""
    author = post.get("author") if isinstance(post, dict) else {}
    author = author if isinstance(author, dict) else {}
    handle = str(author.get("handle") or "").strip().lstrip("@")
    locked_fields = {"url", "owner/name", "current pinned sha"}
    if selected_x_posts and not selected_github_repositories:
        locked_fields.update(
            {
                "license/use boundary",
                "security policy / vulnerability reporting",
            }
        )
    if selected_github_repositories:
        github_owner = str(
            selected_github_repositories[0].get("owner_name") or ""
        ).strip()
        title = github_owner or f"Source card: {card_path.stem}"
    elif status_id:
        title = f"X post {status_id} by @{handle}"
    else:
        title = f"Source card: {card_path.stem}"

    output: list[str] = []
    in_manifest = False
    for line in content.splitlines():
        if line.strip() == "## Decision manifest (ER-278)":
            in_manifest = True
            output.append(line)
            continue
        if in_manifest and line.startswith("## "):
            in_manifest = False
        if line.startswith("# ") and (
            bool(_SOURCE_CARD_EMBEDDED_TODO_RE.search(line[2:].strip()))
            or line[2:].strip()
            in {
                "Source card from gateway-prefetched evidence",
                "gateway/source-card",
            }
        ):
            output.append(f"# {title}")
            continue
        field = re.fullmatch(r"(- ([^:]+):\s*)(.*)", line)
        if field:
            field_name = field.group(2).strip().lower()
            value = field.group(3).strip()
            # Routing fields are rendered once over the whole card below, not
            # per line: the two are cross-dependent, so rendering one without
            # the other's value guesses at the cross-field rule.
            if in_manifest and field_name == "decision-key" and (
                not value or _SOURCE_CARD_EMBEDDED_TODO_RE.search(value)
            ):
                output.append("- no-decision-reason: watch-only")
                continue
            if in_manifest and field_name == "no-decision-reason" and (
                not value or _SOURCE_CARD_EMBEDDED_TODO_RE.search(value)
            ):
                output.append("- no-decision-reason: watch-only")
                continue
            if field_name in locked_fields and defaults.get(field_name) != unavailable:
                output.append(field.group(1) + defaults[field_name])
                continue
            if (
                not value
                # Matched anywhere, not only as a prefix: a value such as
                # `no advisories found - TODO: verify` is just as unfilled, and
                # a prefix-only test sent it to the fail-closed guard below
                # instead of substituting an evidence-safe default.
                or _SOURCE_CARD_EMBEDDED_TODO_RE.search(value)
                or value == unavailable
                or value == neutral_defaults.get(field_name)
                or (
                    field_name == "url"
                    and value == "https://github.com/gateway/source-card"
                )
                or (field_name == "owner/name" and value == "gateway/source-card")
            ):
                output.append(
                    field.group(1)
                    + defaults.get(field_name, unavailable)
                )
                continue
        output.append(line)

    repaired = _source_card_repair_expanded_fields(
        "\n".join(output).rstrip() + "\n",
        defaults,
    )
    finalized = _source_card_render_card_routing(repaired)
    if re.search(r"(?i)\bTODO:", finalized):
        raise _SourceCardLandingError(
            "worker_output",
            "card_content retained a forbidden template placeholder",
        )
    if len(finalized.encode("utf-8")) > _SOURCE_CARD_WORKER_RESULT_MAX_BYTES:
        raise _SourceCardLandingError(
            "worker_output",
            "finalized card_content exceeded the configured byte limit",
        )
    return finalized



def _source_card_fields_and_manifest(
    card_path: Path,
) -> tuple[dict[str, str], list[str], Optional[str]]:
    from gateway.source_card_landing import (
        _SourceCardLandingError,
    )
    try:
        text = card_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _SourceCardLandingError(
            "receipt",
            f"card could not be read: {exc}",
        ) from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"- ([^:]+):\s*(.+)", line)
        if match:
            fields.setdefault(match.group(1).strip().lower(), match.group(2).strip())
    manifest = re.search(
        r"^## Decision manifest \(ER-278\)\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if manifest is None:
        if "## Decision manifest (ER-278)" not in text:
            # Legacy card: 1,988 of the 2,073 cards on origin/main predate the
            # ER-278 manifest requirement entirely.  Re-submitting such a
            # source direct-lands the existing card with no model turn, and a
            # hard failure here made every one of those re-submissions die in
            # seconds (observed 2026-08-22, two intakes).  A wholly absent
            # manifest on an already-landed card is legacy data, not a
            # malformed authoring attempt — record an explicit no-decision
            # receipt instead of failing.  A PRESENT-but-unparseable manifest
            # still fails below, and new worker drafts are separately required
            # to contain exactly one manifest before this code runs.
            return fields, [], "legacy-card-predates-er278"
        raise _SourceCardLandingError("receipt", "card decision manifest is malformed")
    decision_keys: list[str] = []
    no_decision_reason: Optional[str] = None
    for line in manifest.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        decision_match = re.fullmatch(
            r"[-*]\s*decision-key\s*:\s*[\x60]?([^\x60\s]+)[\x60]?",
            stripped,
            flags=re.IGNORECASE,
        )
        reason_match = re.fullmatch(
            r"[-*]\s*no-decision-reason\s*:\s*[\x60]?([^\x60\s]+)[\x60]?",
            stripped,
            flags=re.IGNORECASE,
        )
        if bool(decision_match) == bool(reason_match):
            raise _SourceCardLandingError(
                "receipt",
                "card decision manifest contains an invalid line",
            )
        if decision_match:
            decision_key = decision_match.group(1).lower()
            key_match = re.fullmatch(
                r"card:([a-z0-9][a-z0-9._-]*\.md)#[a-z0-9][a-z0-9._-]*",
                decision_key,
            )
            if (
                key_match is None
                or key_match.group(1) != card_path.name.lower()
            ):
                raise _SourceCardLandingError(
                    "receipt",
                    "card decision key must match "
                    "card:<flat-card.md>#<choice> and the touched card filename",
                )
            decision_keys.append(decision_key)
        else:
            assert reason_match is not None
            if no_decision_reason is not None:
                raise _SourceCardLandingError(
                    "receipt",
                    "card decision manifest has multiple no-decision reasons",
                )
            no_decision_reason = reason_match.group(1).lower()
    if bool(decision_keys) == bool(no_decision_reason):
        raise _SourceCardLandingError(
            "receipt",
            "card decision manifest must use exactly one mode",
        )
    return fields, decision_keys, no_decision_reason



def _source_card_receipt_commands(
    *,
    card_path: Path,
    commit: str,
    intake_text: str,
    environment: dict[str, str],
    source_message_row_id: int,
) -> list[list[str]]:
    """Build stable decision and intake receipt commands from the landed card."""
    from gateway.source_card_prefetch import (
        _SOURCE_CARD_URL_RE,
    )
    fields, decision_keys, no_decision_reason = _source_card_fields_and_manifest(
        card_path
    )
    card_name = card_path.name
    owner_name = fields.get("owner/name", card_path.stem)
    source_url = fields.get("url", "")
    conclusion = fields.get("specific conclusion for this lookup", "")
    disposition = fields.get("disposition", conclusion)
    signal = fields.get("latest source signal", conclusion)
    original_urls = [
        match.group(0).rstrip(".,;:!?)]}")
        for match in _SOURCE_CARD_URL_RE.finditer(intake_text)
    ]
    intake_url = original_urls[0] if original_urls else source_url
    writer = environment["decision_writer"]
    commands: list[list[str]] = []
    for key in decision_keys:
        choice = key.rsplit("#", 1)[-1].replace("-", " ")
        priority = "P3" if disposition.lower().startswith("watch-until") else "P2"
        commands.append(
            [
                writer,
                "add",
                "--key",
                key,
                "--title",
                f"Source-card decision: {owner_name} — {choice}"[:240],
                "--question",
                f"Should Trevor {choice} for {owner_name}?"[:500],
                "--recommendation",
                disposition[:4_000] or conclusion[:4_000] or choice,
                "--rationale",
                signal[:4_000] or conclusion[:4_000] or disposition[:4_000],
                "--priority",
                priority,
                "--source-kind",
                "source-card",
                "--source-ref",
                card_name,
                "--source-url",
                source_url,
                "--source-chat",
                environment["source_chat_id"],
                "--source-session",
                environment["parent_session_id"],
                "--source-message",
                str(source_message_row_id),
                "--actor",
                "Hermes gateway source-card intake",
                "--json",
            ]
        )
    if no_decision_reason:
        commands.append(
            [
                writer,
                "ignore-source",
                "--source-ref",
                card_name,
                "--reason",
                no_decision_reason,
                "--note",
                (disposition or conclusion or no_decision_reason)[:4_000],
                "--cards-root",
                environment["cards_root"],
                "--json",
            ]
        )
    commands.append(
        [
            writer,
            "receipt-intake",
            "--source-chat",
            environment["source_chat_id"],
            "--source-session",
            environment["parent_session_id"],
            "--source-message",
            str(source_message_row_id),
            "--source-url",
            intake_url,
            "--card",
            card_name,
            "--cards-root",
            environment["cards_root"],
            "--commit",
            commit,
            "--remote",
            "origin",
            "--remote-ref",
            "refs/heads/main",
            "--transcript-db",
            environment["transcript_db"],
            "--note",
            f"Gateway source-card landing validated {card_name} at {commit}",
            "--json",
        ]
    )
    return commands



_SOURCE_CARD_EMBEDDED_TODO_RE = re.compile(r"(?i)\bTODO:")



_SOURCE_CARD_HERMES_RELEVANCE_VALUES = ("upgrade-candidate", "adjacent", "direct")



_SOURCE_CARD_DOWNSTREAM_TARGET_RE = re.compile(r"[a-z0-9][a-z0-9-]*")



_SOURCE_CARD_NONE_PREFIX_RE = re.compile(r"(?i)^none\s*:\s*(\S.*)$")



_SOURCE_CARD_APPROVED_DISPOSITION_PREFIXES = (
    "adopt-as-pattern:",
    "pilot-scoped:",
    "skip-with-evidence:",
    "watch-until:",
)
