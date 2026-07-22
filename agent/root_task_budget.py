"""Aggregate per-user-turn model-call budget and durable call receipts.

Local :class:`agent.iteration_budget.IterationBudget` instances still protect
each agent loop.  This module adds the missing aggregate control plane shared
by the parent, delegates, background review, auxiliary tasks, and MoA slots.
It charges at the physical provider-call seam, so a retry or fallback cannot
escape merely because it belongs to the same logical loop iteration.

The final ``closure_reserve`` calls are available only to the parent/integrator
scope.  Optional fan-out therefore winds down before it can strand the answer
without capacity for integration and verification.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_receipt_file_lock = threading.Lock()
_root_call_context: ContextVar[Optional["RootCallContext"]] = ContextVar(
    "root_call_context", default=None
)


class RootBudgetExhausted(RuntimeError):
    """Raised before a provider call when the aggregate budget denies it."""

    def __init__(self, *, root_turn_id: str, scope: str, used: int, limit: int):
        self.root_turn_id = root_turn_id
        self.scope = scope
        self.used = used
        self.limit = limit
        super().__init__(
            f"root task model-call budget exhausted for {scope} "
            f"({used}/{limit}; root={root_turn_id})"
        )


def _json_safe_reasoning(value: Any) -> Any:
    """Keep reasoning metadata serializable without recording request content."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe_reasoning(item)
            for key, item in value.items()
            if str(key).lower() not in {"api_key", "token", "secret", "password"}
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_reasoning(item) for item in value]
    return str(value)


def _usage_fields(response: Any) -> Dict[str, int]:
    raw_usage = getattr(response, "usage", None)
    if raw_usage is None:
        return {}
    try:
        from agent.usage_pricing import normalize_usage

        usage = normalize_usage(raw_usage)
        return {
            "input_tokens": int(usage.input_tokens or 0),
            "output_tokens": int(usage.output_tokens or 0),
            "cache_read_tokens": int(usage.cache_read_tokens or 0),
            "cache_write_tokens": int(usage.cache_write_tokens or 0),
            "reasoning_tokens": int(usage.reasoning_tokens or 0),
            "total_tokens": int(usage.total_tokens or 0),
        }
    except Exception:
        logger.debug("Could not normalize model-call receipt usage", exc_info=True)
        return {}


def durable_receipt_sink(event: Dict[str, Any]) -> None:
    """Append one receipt event as JSONL under ``HERMES_HOME/logs``.

    The stream is event-shaped: one ``started`` record and one terminal record
    share a stable ``call_id``.  Auditors count unique started call IDs as
    physical calls and can still diagnose crashes that left only a start.
    """
    override = os.environ.get("HERMES_MODEL_CALL_RECEIPTS", "").strip()
    path = (
        Path(override).expanduser()
        if override
        else get_hermes_home() / "logs" / "model-call-receipts.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with _receipt_file_lock:
        with path.open("a", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            handle.write(line + "\n")
            handle.flush()
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


class ModelCallReservation:
    """One charged physical-call attempt with an idempotent terminal receipt."""

    def __init__(
        self,
        *,
        event: Dict[str, Any],
        sink: Optional[Callable[[Dict[str, Any]], None]],
    ):
        self._event = event
        self._sink = sink
        self._finished = False
        self._lock = threading.Lock()

    @property
    def call_id(self) -> str:
        return str(self._event["call_id"])

    def _emit(self, event: Dict[str, Any]) -> None:
        if self._sink is None:
            return
        try:
            self._sink(dict(event))
        except Exception:
            logger.warning("Model-call receipt persistence failed", exc_info=True)

    def finish(
        self,
        status: str,
        *,
        response: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            event = dict(self._event)
            event["status"] = status
            event["finished_at"] = time.time()
            if response is not None:
                actual_model = str(getattr(response, "model", "") or "").strip()
                if actual_model:
                    event["model"] = actual_model
                event.update(_usage_fields(response))
            if error is not None:
                event["error_type"] = type(error).__name__
                event["error_message"] = str(error)[:500]
            self._emit(event)


class RootTaskBudget:
    """Thread-safe aggregate model-call budget for one root user turn."""

    def __init__(
        self,
        *,
        root_turn_id: str,
        max_total: int,
        closure_reserve: int,
        receipt_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        max_total = max(1, int(max_total))
        closure_reserve = max(0, min(int(closure_reserve), max_total - 1))
        self.root_turn_id = str(root_turn_id)
        self.max_total = max_total
        self.closure_reserve = closure_reserve
        self._receipt_sink = receipt_sink
        self._used = 0
        self._sequence = 0
        self._lock = threading.Lock()

    def begin_call(
        self,
        *,
        scope: str,
        session_id: str,
        task: str,
        role: str,
        provider: str,
        model: str,
        reasoning: Any = None,
        fallback_path: str = "primary",
    ) -> ModelCallReservation:
        """Charge and start one physical provider call.

        ``scope='parent'`` may consume the closure reserve. Every other scope
        stops at ``max_total - closure_reserve``.
        """
        normalized_scope = str(scope or "auxiliary").strip().lower()
        with self._lock:
            allowed = self.max_total
            if normalized_scope != "parent":
                allowed -= self.closure_reserve
            if self._used >= allowed:
                raise RootBudgetExhausted(
                    root_turn_id=self.root_turn_id,
                    scope=normalized_scope,
                    used=self._used,
                    limit=allowed,
                )
            self._used += 1
            self._sequence += 1
            sequence = self._sequence
            used_at_reservation = self._used

        event = {
            "schema_version": 1,
            "call_id": f"{self.root_turn_id}:call:{sequence}",
            "root_turn_id": self.root_turn_id,
            "sequence": sequence,
            "session_id": str(session_id or ""),
            "task": str(task or "main"),
            "role": str(role or normalized_scope),
            "scope": normalized_scope,
            "provider": str(provider or "unknown"),
            "model": str(model or "unknown"),
            "reasoning": _json_safe_reasoning(reasoning),
            "fallback_path": str(fallback_path or "primary"),
            "status": "started",
            "started_at": time.time(),
            "root_budget_used": used_at_reservation,
            "root_budget_max": self.max_total,
            "root_closure_reserve": self.closure_reserve,
        }
        reservation = ModelCallReservation(event=event, sink=self._receipt_sink)
        reservation._emit(event)
        return reservation

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def optional_remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self.closure_reserve - self._used)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "root_turn_id": self.root_turn_id,
                "used": self._used,
                "max_total": self.max_total,
                "remaining": max(0, self.max_total - self._used),
                "optional_remaining": max(
                    0, self.max_total - self.closure_reserve - self._used
                ),
                "closure_reserve": self.closure_reserve,
            }


@dataclass(frozen=True)
class RootCallContext:
    budget: RootTaskBudget
    session_id: str
    role: str = "parent"

    def for_child(self, *, session_id: str, role: str) -> "RootCallContext":
        return replace(self, session_id=session_id, role=role)


def set_root_call_context(context: Optional[RootCallContext]):
    return _root_call_context.set(context)


def reset_root_call_context(token) -> None:
    try:
        _root_call_context.reset(token)
    except Exception:
        _root_call_context.set(None)


def get_root_call_context() -> Optional[RootCallContext]:
    return _root_call_context.get()


def resolve_root_budget_limits(
    *,
    parent_max_iterations: int,
    config: Optional[Dict[str, Any]] = None,
) -> tuple[int, int]:
    """Resolve aggregate and reserve limits from the merged Hermes config."""
    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    delegation_cfg = (
        config.get("delegation")
        if isinstance(config.get("delegation"), dict)
        else {}
    )
    parent_limit = max(1, int(parent_max_iterations or 1))
    try:
        delegate_limit = max(0, int(delegation_cfg.get("max_iterations", 50)))
    except (TypeError, ValueError):
        delegate_limit = 50

    configured_max = agent_cfg.get("root_max_iterations", "auto")
    if isinstance(configured_max, str) and configured_max.strip().lower() == "auto":
        max_total = parent_limit + delegate_limit
    else:
        try:
            max_total = max(1, int(configured_max))
        except (TypeError, ValueError):
            max_total = parent_limit + delegate_limit

    configured_reserve = agent_cfg.get("root_closure_reserve", "auto")
    if (
        isinstance(configured_reserve, str)
        and configured_reserve.strip().lower() == "auto"
    ):
        # Six percent gives the default 90+50 budget eight protected calls:
        # enough for integration/verification without becoming a second large
        # hidden allowance. Keep useful bounds for unusually small/large caps.
        reserve = max(2, min(12, round(max_total * 0.06)))
    else:
        try:
            reserve = max(0, int(configured_reserve))
        except (TypeError, ValueError):
            reserve = max(2, min(12, round(max_total * 0.06)))
    reserve = min(reserve, max_total - 1)
    return max_total, reserve


def execute_model_call(
    call: Callable[[], Any],
    *,
    context: Optional[RootCallContext] = None,
    scope: str,
    task: str,
    provider: str,
    model: str,
    reasoning: Any = None,
    fallback_path: str = "primary",
) -> Any:
    """Execute one sync or async physical call with budget + receipt handling."""
    context = context or get_root_call_context()
    if context is None:
        return call()

    reservation = context.budget.begin_call(
        scope=scope,
        session_id=context.session_id,
        task=task,
        role=context.role,
        provider=provider,
        model=model,
        reasoning=reasoning,
        fallback_path=fallback_path,
    )
    try:
        result = call()
    except BaseException as error:
        reservation.finish("failed", error=error)
        raise

    if inspect.isawaitable(result):
        async def _await_and_finish():
            try:
                response = await result
            except BaseException as error:
                reservation.finish("failed", error=error)
                raise
            reservation.finish("succeeded", response=response)
            return response

        return _await_and_finish()

    reservation.finish("succeeded", response=result)
    return result


__all__ = [
    "ModelCallReservation",
    "RootBudgetExhausted",
    "RootCallContext",
    "RootTaskBudget",
    "durable_receipt_sink",
    "execute_model_call",
    "get_root_call_context",
    "reset_root_call_context",
    "resolve_root_budget_limits",
    "set_root_call_context",
]
