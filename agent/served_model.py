"""Provider-attested served model, recorded per API call.

Nothing previously carried the provider-reported model out of the conversation
loop. The only read of ``response.model`` sat behind a ``has_hook`` guard, so
with no hook registered every source-card delegation row recorded the
CONFIGURED model echoed straight back. A silent provider substitution was
therefore invisible to the gateway that has to decide whether to land a card.

Four properties this module exists to get right:

* The receipt is a **list**, not a scalar. Live rows show 16-20 API calls in a
  single turn and the pre-existing per-turn stash keeps only the last call by
  design, so a scalar cannot represent "which models actually answered".
* A mismatch is judged **against the route as of that call**. ``agent.model``
  is legitimately reassigned on a provider switch, so a turn-level scalar
  comparison would flag every legitimate fallback.
* A provider that reports **no** model is `unattested`, which is a mismatch —
  never a clean match by omission.
* Recording is best-effort and must never raise into the conversation loop.
"""

from __future__ import annotations

from typing import Any, Optional

_ATTR = "_served_model_receipt"


def _normalize(name: Any) -> Optional[str]:
    """Reduce a model id to a comparable form.

    Provider-qualified (`zai/glm-5.3`) and bare (`glm-5.3`) names refer to the
    same model, and case and surrounding whitespace are not signal. Comparing
    raw strings would manufacture mismatches that then fail closed on healthy
    traffic.
    """
    if not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    if not cleaned:
        return None
    return cleaned.rsplit("/", 1)[-1]


def reset_served_models(agent: Any) -> None:
    """Clear the accumulator at the start of a turn."""
    try:
        setattr(agent, _ATTR, [])
    except Exception:
        pass


def record_served_model(agent: Any, *, requested: Any, response: Any) -> None:
    """Record what was asked for and what the provider said it served."""
    try:
        served = getattr(response, "model", None)
    except Exception:
        served = None
    try:
        rows = getattr(agent, _ATTR, None)
        if not isinstance(rows, list):
            rows = []
            setattr(agent, _ATTR, rows)
        rows.append(
            {
                "call": len(rows) + 1,
                "requested": requested if isinstance(requested, str) else None,
                "served": served if isinstance(served, str) else None,
            }
        )
    except Exception:
        # Attestation is evidence, not control flow. Never break a turn.
        pass


def served_model_receipt(agent: Any) -> list[dict]:
    """Return the per-call receipt for the current turn."""
    rows = getattr(agent, _ATTR, None)
    return list(rows) if isinstance(rows, list) else []


def served_model_mismatches(agent: Any) -> list[dict]:
    """Return the rows whose served model is absent or not what was requested."""
    mismatches = []
    for row in served_model_receipt(agent):
        served = _normalize(row.get("served"))
        requested = _normalize(row.get("requested"))
        if served is None or requested is None or served != requested:
            mismatches.append(row)
    return mismatches
