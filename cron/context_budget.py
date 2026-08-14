"""Pure context-budget calculations shared by cron definition and runtime gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional


USABLE_CONTEXT_FRACTION = 0.80


@dataclass(frozen=True)
class ContextBudgetEvaluation:
    """One deterministic context-budget decision."""

    estimated_tokens: int
    hard_ceiling_tokens: int
    usable_tokens: int

    @property
    def exceeded(self) -> bool:
        return self.estimated_tokens >= self.usable_tokens


def usable_context_tokens(hard_ceiling_tokens: int) -> int:
    """Return the 80% usable portion of a positive hard context ceiling."""
    if type(hard_ceiling_tokens) is not int or hard_ceiling_tokens <= 0:
        raise ValueError("hard context ceiling must be a positive integer")
    return int(hard_ceiling_tokens * USABLE_CONTEXT_FRACTION)


def evaluate_context_parts(
    parts: Iterable[object],
    *,
    hard_ceiling_tokens: int,
    token_estimator: Optional[Callable[[str], int]] = None,
) -> ContextBudgetEvaluation:
    """Estimate text parts and compare them with the usable context budget.

    The helper is intentionally pure: callers resolve stored skills, bundles,
    script output, notepad data, and other inputs before passing their text.
    """
    if token_estimator is None:
        from agent.model_metadata import estimate_tokens_rough

        token_estimator = estimate_tokens_rough
    estimated = sum(
        max(0, int(token_estimator(str(part))))
        for part in parts
        if part not in (None, "")
    )
    return ContextBudgetEvaluation(
        estimated_tokens=estimated,
        hard_ceiling_tokens=hard_ceiling_tokens,
        usable_tokens=usable_context_tokens(hard_ceiling_tokens),
    )
