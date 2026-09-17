from __future__ import annotations

from pathlib import Path
import os
import stat
from typing import Any, Mapping

from .config import BridgeConfig
from .work_contract import ActivityFact, WorkFact
from .work_projection import (
    RuntimeDeliveryForbidden,
    WorkProjection,
    WorkProjectionDisabled,
    WorkProjectionError,
)
from .work_store import ActivityAcceptance, WorkAcceptance, WorkEvaluation, WorkStore


class WorkAdapter:
    """Narrow, lazy WP-2 API for a future trusted metadata-only exporter."""

    def __init__(
        self,
        config: BridgeConfig | None = None,
        *,
        store: WorkStore | None = None,
    ):
        self.config = config or BridgeConfig.from_env()
        if self.config.work_progress_runtime_delivery:
            raise RuntimeDeliveryForbidden("work_progress_runtime_delivery_must_be_false")
        self._store = store
        self._owns_store = store is None
        self._projection: WorkProjection | None = None

    def _guard_enabled(self) -> None:
        if self.config.work_progress_runtime_delivery:
            raise RuntimeDeliveryForbidden("work_progress_runtime_delivery_must_be_false")
        if not self.config.work_progress_enabled:
            raise WorkProjectionDisabled("work_progress_disabled")

    def _guard_dedicated_ledger_path(self, store_path: str) -> Path:
        ledger = Path(store_path).expanduser().resolve(strict=False)
        try:
            parent_stat = ledger.parent.stat()
        except FileNotFoundError as exc:
            raise WorkProjectionError("work_ledger_parent_must_exist") from exc
        if (
            parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077
        ):
            raise WorkProjectionError("work_ledger_parent_must_be_owner_private")
        # Every ancestor must also be protected from non-owner pathname
        # substitution. A sticky directory (such as /tmp) is acceptable only
        # because its immediate descendant above is already owner-private.
        current = ledger.parent
        while current != current.parent:
            current_stat = os.stat(current)
            mode = stat.S_IMODE(current_stat.st_mode)
            writable_by_others = bool(mode & 0o022)
            # Root-owned system ancestors are trusted; any other owner could
            # chmod/rename its directory after our validation.
            if current_stat.st_uid not in {os.geteuid(), 0}:
                raise WorkProjectionError("work_ledger_ancestor_owner_untrusted")
            if writable_by_others and not (mode & stat.S_ISVTX):
                raise WorkProjectionError("work_ledger_ancestor_must_be_private")
            current = current.parent
        protected = {
            Path(path).expanduser().resolve(strict=False)
            for path in (
                self.config.contact_db,
                self.config.operation_db,
                self.config.cognition_db,
            )
            if path
        }
        if ledger in protected:
            raise WorkProjectionError("work_ledger_path_must_be_dedicated")
        # A different pathname can still name an existing protected database via
        # a hard link. Compare filesystem identity whenever both paths exist.
        try:
            ledger_identity = (os.stat(ledger).st_dev, os.stat(ledger).st_ino)
        except FileNotFoundError:
            return ledger
        for protected_path in protected:
            try:
                protected_identity = (
                    os.stat(protected_path).st_dev,
                    os.stat(protected_path).st_ino,
                )
            except FileNotFoundError:
                continue
            if ledger_identity == protected_identity:
                raise WorkProjectionError("work_ledger_path_must_be_dedicated")
        return ledger

    def _get_projection(self) -> WorkProjection:
        self._guard_enabled()
        if self._projection is None:
            if self._store is None:
                store_path = (
                    self.config.work_ledger_db or self.config.work_projection_db
                )
                ledger_path = self._guard_dedicated_ledger_path(store_path)
                self._store = WorkStore(str(ledger_path))
            idle_delay_seconds = (
                self.config.work_idle_seconds
                if self.config.work_ledger_db
                else self.config.work_progress_idle_seconds
            )
            self._projection = WorkProjection(
                self._store,
                enabled=True,
                runtime_delivery=False,
                idle_delay_seconds=idle_delay_seconds,
            )
        return self._projection

    def record_work_fact(self, value: Mapping[str, Any]) -> WorkAcceptance:
        self._guard_enabled()
        # Validate before lazy store creation: hostile input cannot touch DB/WAL/SHM.
        fact = WorkFact.from_mapping(value)
        return self._get_projection().accept_fact(fact)

    def note_work_activity(self, value: Mapping[str, Any]) -> ActivityAcceptance:
        self._guard_enabled()
        activity = ActivityFact.from_mapping(value)
        return self._get_projection().note_activity_fact(activity)

    def evaluate_due(
        self,
        *,
        now: str | None = None,
        limit: int = 100,
    ) -> tuple[WorkEvaluation, ...]:
        self._guard_enabled()
        return self._get_projection().evaluate_due(
            now=now,
            limit=limit,
        )

    def close(self) -> None:
        if self._owns_store and self._store is not None:
            self._store.close()
        self._store = None
        self._projection = None

    def __enter__(self) -> "WorkAdapter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def record_work_fact(
    value: Mapping[str, Any], *, config: BridgeConfig | None = None
) -> WorkAcceptance:
    with WorkAdapter(config) as adapter:
        return adapter.record_work_fact(value)


def note_work_activity(
    value: Mapping[str, Any], *, config: BridgeConfig | None = None
) -> ActivityAcceptance:
    with WorkAdapter(config) as adapter:
        return adapter.note_work_activity(value)


def evaluate_due(
    *,
    now: str | None = None,
    limit: int = 100,
    config: BridgeConfig | None = None,
) -> tuple[WorkEvaluation, ...]:
    with WorkAdapter(config) as adapter:
        return adapter.evaluate_due(
            now=now,
            limit=limit,
        )
