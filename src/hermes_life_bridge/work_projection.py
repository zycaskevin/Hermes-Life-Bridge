from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .work_contract import ActivityFact, WorkFact
from .work_store import (
    ActivityAcceptance,
    WorkAcceptance,
    WorkEvaluation,
    WorkStore,
)


class WorkProjectionError(RuntimeError):
    pass


class WorkProjectionDisabled(WorkProjectionError):
    pass


class RuntimeDeliveryForbidden(WorkProjectionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class WorkProjection:
    """Content-free WP-2 state transitions with an explicit no-send gate."""

    def __init__(
        self,
        store: WorkStore,
        *,
        enabled: bool,
        runtime_delivery: bool,
        idle_delay_seconds: float = 900.0,
    ):
        if not isinstance(enabled, bool) or not isinstance(runtime_delivery, bool):
            raise WorkProjectionError("work_projection_flags_must_be_bool")
        if runtime_delivery:
            raise RuntimeDeliveryForbidden("work_progress_runtime_delivery_must_be_false")
        if isinstance(idle_delay_seconds, bool) or not isinstance(
            idle_delay_seconds, (int, float)
        ):
            raise WorkProjectionError("idle_delay_seconds_must_be_number")
        if idle_delay_seconds < 0 or idle_delay_seconds > 604800:
            raise WorkProjectionError("idle_delay_seconds_out_of_range")
        self._store = store
        self._enabled = enabled
        self._runtime_delivery = runtime_delivery
        self._idle_delay_seconds = float(idle_delay_seconds)

    def _guard(self) -> None:
        if self._runtime_delivery:
            raise RuntimeDeliveryForbidden("work_progress_runtime_delivery_must_be_false")
        if not self._enabled:
            raise WorkProjectionDisabled("work_progress_disabled")

    def accept(self, value: Mapping[str, Any]) -> WorkAcceptance:
        self._guard()
        fact = WorkFact.from_mapping(value)
        return self.accept_fact(fact)

    def accept_fact(self, fact: WorkFact) -> WorkAcceptance:
        self._guard()
        return self._store.accept(
            fact,
            idle_delay_seconds=self._idle_delay_seconds,
        )

    def note_activity(self, value: Mapping[str, Any]) -> ActivityAcceptance:
        self._guard()
        activity = ActivityFact.from_mapping(value)
        return self.note_activity_fact(activity)

    def note_activity_fact(self, activity: ActivityFact) -> ActivityAcceptance:
        self._guard()
        return self._store.note_activity(activity)

    def evaluate_due(
        self,
        *,
        now: str | None = None,
        limit: int = 100,
    ) -> tuple[WorkEvaluation, ...]:
        self._guard()
        return self._store.evaluate_due(
            now=now or _now(),
            limit=limit,
        )
