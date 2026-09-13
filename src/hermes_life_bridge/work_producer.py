from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Mapping, Sequence
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from .config import BridgeConfig


REPORT_TOOL_NAME = "hlb_report_work_event"
REPORT_TOOLSET = "safe"
CREDENTIALS_SCHEMA_VERSION = "hlb-work-producer-credentials.v0.1"
WORK_EVENT_SCHEMA_VERSION = "work-event.v0.1"
CONTEXT_ACTIVITY_SCHEMA_VERSION = "work-context-activity.v0.1"

_ELIGIBLE_EVENT_TYPES = frozenset(
    {"verified_completion", "blocked_needs_owner", "material_failure"}
)
_SIGNIFICANCE = frozenset({"low", "normal", "high", "critical"})
_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_METRIC_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ROUTE_PREFIX_RE = re.compile(
    r"(?:feishu|telegram|discord|slack|signal|sms|whatsapp):", re.I
)
_FEISHU_CHAT_RE = re.compile(r"\boc_[A-Za-z0-9_-]{6,}\b")
_URL_RE = re.compile(r"\b(?:https?|wss?|file)://\S+", re.I)
_POSIX_PATH_RE = re.compile(r"(?:^|[\s(])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.I)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:authorization|cookie|password|secret|token|api[_-]?key)\s*[:=]", re.I
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_SHELL_RE = re.compile(r"`|\$\(")
_MAX_LABEL = 96
_MAX_SUMMARY = 320
_MAX_METRICS = 16
_MAX_METRIC_ABS = 1_000_000_000_000_000.0


REPORT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": REPORT_TOOL_NAME,
        "description": (
            "Declare a meaningful verified work outcome to Life Runtime shadow observation. "
            "Use only after this turn has real tool evidence for a completed result, an owner blocker, "
            "or a material failure. Do not use for ordinary conversation, intermediate progress, or "
            "message completion. label/summary must be short semantic descriptions only: no paths, "
            "commands, URLs, chat IDs, raw tool output, prompts, credentials, or secrets. This tool "
            "never contacts the user and never authorizes delivery."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "work_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "Stable opaque work identity reused when the same work later changes state.",
                },
                "event_type": {
                    "type": "string",
                    "enum": sorted(_ELIGIBLE_EVENT_TYPES),
                },
                "significance": {
                    "type": "string",
                    "enum": sorted(_SIGNIFICANCE),
                    "default": "normal",
                },
                "label": {"type": "string", "minLength": 1, "maxLength": _MAX_LABEL},
                "summary": {"type": "string", "minLength": 1, "maxLength": _MAX_SUMMARY},
                "metrics": {
                    "type": "object",
                    "maxProperties": _MAX_METRICS,
                    "additionalProperties": {"type": "number"},
                    "default": {},
                },
                "evidence_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "default": 1,
                    "description": "How many recent matching terminal tool calls from this turn to bind as evidence.",
                },
            },
            "required": ["work_id", "event_type", "label", "summary"],
        },
    },
}


class WorkProducerError(RuntimeError):
    pass


class WorkProducerValidationError(WorkProducerError):
    pass


class WorkProducerEvidenceError(WorkProducerError):
    pass


class WorkProducerTransportError(WorkProducerError):
    pass


@dataclass(frozen=True)
class WorkProducerCredentials:
    principal_id: str
    runtime_bearer: str = field(repr=False)
    work_bearer: str = field(repr=False)

    @classmethod
    def from_file(cls, path: str) -> "WorkProducerCredentials":
        candidate = Path(path).expanduser()
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as exc:
            raise WorkProducerValidationError("work_producer_credentials_missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkProducerValidationError("work_producer_credentials_must_not_be_symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkProducerValidationError("work_producer_credentials_must_be_regular_file")
        if metadata.st_mode & 0o077:
            raise WorkProducerValidationError("work_producer_credentials_must_be_owner_only")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise WorkProducerValidationError("work_producer_credentials_wrong_owner")
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkProducerValidationError("work_producer_credentials_invalid_json") from exc
        data = _exact_mapping(
            parsed,
            {"schema_version", "principal_id", "runtime_bearer", "work_bearer"},
            "work_producer_credentials",
        )
        if data["schema_version"] != CREDENTIALS_SCHEMA_VERSION:
            raise WorkProducerValidationError("work_producer_credentials_schema_unsupported")
        principal_id = _opaque_id(data["principal_id"], "principal_id")
        runtime_bearer = _secret_string(data["runtime_bearer"], "runtime_bearer")
        work_bearer = _secret_string(data["work_bearer"], "work_bearer")
        return cls(
            principal_id=principal_id,
            runtime_bearer=runtime_bearer,
            work_bearer=work_bearer,
        )


@dataclass(frozen=True)
class WorkDeclaration:
    work_id: str
    event_type: str
    significance: str
    label: str
    summary: str
    metrics: Mapping[str, int | float]
    evidence_count: int

    @classmethod
    def from_args(cls, value: Mapping[str, Any]) -> "WorkDeclaration":
        allowed = {
            "work_id",
            "event_type",
            "significance",
            "label",
            "summary",
            "metrics",
            "evidence_count",
        }
        if not isinstance(value, Mapping):
            raise WorkProducerValidationError("work_declaration_must_be_mapping")
        if any(not isinstance(key, str) or key not in allowed for key in value):
            raise WorkProducerValidationError("work_declaration_contains_unknown_field")
        work_id = _opaque_id(value.get("work_id"), "work_id")
        event_type = value.get("event_type")
        if event_type not in _ELIGIBLE_EVENT_TYPES:
            raise WorkProducerValidationError("work_declaration_event_type_not_allowed")
        significance = value.get("significance", "normal")
        if significance not in _SIGNIFICANCE:
            raise WorkProducerValidationError("work_declaration_significance_not_allowed")
        label = _safe_semantic_text(value.get("label"), "label", _MAX_LABEL)
        summary = _safe_semantic_text(value.get("summary"), "summary", _MAX_SUMMARY)
        metrics = _safe_metrics(value.get("metrics", {}))
        evidence_count = value.get("evidence_count", 1)
        if isinstance(evidence_count, bool) or not isinstance(evidence_count, int):
            raise WorkProducerValidationError("work_declaration_evidence_count_must_be_integer")
        if not 1 <= evidence_count <= 4:
            raise WorkProducerValidationError("work_declaration_evidence_count_out_of_range")
        return cls(
            work_id=work_id,
            event_type=str(event_type),
            significance=str(significance),
            label=label,
            summary=summary,
            metrics=metrics,
            evidence_count=evidence_count,
        )


@dataclass(frozen=True)
class ToolEvidence:
    evidence_ref: str
    tool_name: str
    status: str
    observed_at: str
    observed_monotonic: float


class HermesToolEvidenceRegistry:
    """Bounded in-memory registry of terminal tool metadata only.

    It deliberately never stores args, results, prompts, messages, filesystem paths,
    or provider payloads. Raw post_tool_call values are ignored by the caller before
    this boundary.
    """

    def __init__(
        self,
        *,
        max_contexts: int = 256,
        max_per_context: int = 16,
        ttl_seconds: float = 1800.0,
    ):
        self.max_contexts = max_contexts
        self.max_per_context = max_per_context
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[tuple[str, str], deque[ToolEvidence]] = OrderedDict()
        self._lock = threading.RLock()

    def observe(
        self,
        *,
        tool_name: str,
        status: str | None,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
    ) -> ToolEvidence | None:
        if tool_name == REPORT_TOOL_NAME:
            return None
        if not session_id or not turn_id or not tool_call_id:
            return None
        normalized_status = (status or "ok").strip().lower() or "ok"
        now_mono = time.monotonic()
        observed_at = _now_utc()
        digest = sha256(
            "\0".join(
                [session_id, turn_id, tool_call_id, tool_name, normalized_status]
            ).encode("utf-8")
        ).hexdigest()
        evidence = ToolEvidence(
            evidence_ref=f"evidence://hermes/tool/{digest[:48]}",
            tool_name=tool_name,
            status=normalized_status,
            observed_at=observed_at,
            observed_monotonic=now_mono,
        )
        key = (session_id, turn_id)
        with self._lock:
            self._prune(now_mono)
            bucket = self._items.setdefault(key, deque(maxlen=self.max_per_context))
            bucket.append(evidence)
            self._items.move_to_end(key)
            while len(self._items) > self.max_contexts:
                self._items.popitem(last=False)
        return evidence

    def select(
        self,
        *,
        session_id: str,
        turn_id: str,
        event_type: str,
        count: int,
    ) -> tuple[ToolEvidence, ...]:
        if not session_id or not turn_id:
            raise WorkProducerEvidenceError("work_evidence_missing_turn_identity")
        now_mono = time.monotonic()
        key = (session_id, turn_id)
        with self._lock:
            self._prune(now_mono)
            candidates = list(self._items.get(key, ()))
        if event_type == "verified_completion":
            candidates = [item for item in candidates if item.status == "ok"]
        elif event_type == "material_failure":
            candidates = [item for item in candidates if item.status != "ok"]
        elif event_type == "blocked_needs_owner":
            candidates = list(candidates)
        else:
            raise WorkProducerEvidenceError("work_evidence_event_type_not_allowed")
        if len(candidates) < count:
            raise WorkProducerEvidenceError("work_evidence_insufficient_current_turn_evidence")
        return tuple(candidates[-count:])

    def snapshot_metadata(self) -> dict[str, int]:
        with self._lock:
            return {
                "contexts": len(self._items),
                "evidence": sum(len(items) for items in self._items.values()),
            }

    def _prune(self, now_mono: float) -> None:
        expired_keys: list[tuple[str, str]] = []
        for key, bucket in self._items.items():
            fresh = [
                item
                for item in bucket
                if now_mono - item.observed_monotonic <= self.ttl_seconds
            ]
            if fresh:
                self._items[key] = deque(fresh, maxlen=self.max_per_context)
            else:
                expired_keys.append(key)
        for key in expired_keys:
            self._items.pop(key, None)


@dataclass(frozen=True)
class WorkIngressReceipt:
    http_status: int
    status: str
    state: str | None = None
    reason: str | None = None


class LifeRuntimeWorkClient:
    def __init__(
        self,
        endpoint: str,
        credentials: WorkProducerCredentials,
        *,
        timeout_seconds: float = 1.0,
    ):
        self.endpoint = _validate_endpoint(endpoint)
        self.credentials = credentials
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise WorkProducerValidationError("work_producer_timeout_must_be_number")
        if not 0.1 <= float(timeout_seconds) <= 10.0:
            raise WorkProducerValidationError("work_producer_timeout_out_of_range")
        self.timeout_seconds = float(timeout_seconds)

    def post_event(self, event: Mapping[str, Any]) -> WorkIngressReceipt:
        body = self._post("/v1/work/events", event)
        acceptance = body.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise WorkProducerTransportError("work_ingress_event_receipt_invalid")
        return WorkIngressReceipt(
            http_status=int(body["_http_status"]),
            status=str(acceptance.get("status") or "unknown"),
            state=_optional_string(acceptance.get("state")),
            reason=_optional_string(acceptance.get("reason")),
        )

    def post_context_activity(self, activity: Mapping[str, Any]) -> WorkIngressReceipt:
        body = self._post("/v1/work/context-activity", activity)
        acceptance = body.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise WorkProducerTransportError("work_ingress_activity_receipt_invalid")
        return WorkIngressReceipt(
            http_status=int(body["_http_status"]),
            status=str(acceptance.get("status") or "unknown"),
        )

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        request = urlrequest.Request(
            f"{self.endpoint}{path}",
            data=encoded,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.credentials.runtime_bearer}",
                "x-life-work-principal": self.credentials.principal_id,
                "x-life-work-token": self.credentials.work_bearer,
            },
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                raw = response.read(65_537)
        except urlerror.HTTPError as exc:
            raise WorkProducerTransportError(f"work_ingress_http_{exc.code}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise WorkProducerTransportError("work_ingress_transport_failure") from exc
        if len(raw) > 65_536:
            raise WorkProducerTransportError("work_ingress_receipt_too_large")
        if not 200 <= status < 300:
            raise WorkProducerTransportError(f"work_ingress_http_{status}")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise WorkProducerTransportError("work_ingress_receipt_invalid_json") from exc
        if not isinstance(parsed, dict):
            raise WorkProducerTransportError("work_ingress_receipt_invalid_shape")
        parsed["_http_status"] = status
        return parsed


@dataclass(frozen=True)
class ProducerEmissionResult:
    emitted: bool
    reason: str
    event_id: str | None = None
    receipt: WorkIngressReceipt | None = None


class HermesWorkProducer:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        credentials: WorkProducerCredentials | None = None,
        client: LifeRuntimeWorkClient | None = None,
        evidence_registry: HermesToolEvidenceRegistry | None = None,
    ):
        if not config.work_producer_enabled:
            raise WorkProducerValidationError("work_producer_disabled")
        self.config = config
        self.credentials = credentials or WorkProducerCredentials.from_file(
            config.work_producer_credentials_file
        )
        self.client = client or LifeRuntimeWorkClient(
            config.work_producer_endpoint,
            self.credentials,
            timeout_seconds=config.work_producer_timeout_seconds,
        )
        self.evidence = evidence_registry or HermesToolEvidenceRegistry()

    def note_context_activity(self, session_ref: str) -> WorkIngressReceipt | None:
        if not session_ref:
            return None
        activity = {
            "schema_version": CONTEXT_ACTIVITY_SCHEMA_VERSION,
            "context_ref": _context_ref(session_ref),
            "observer_id": self.credentials.principal_id,
            "observed_at": _now_utc(),
        }
        return self.client.post_context_activity(activity)

    def observe_post_tool_call(
        self,
        *,
        tool_name: str,
        args: Mapping[str, Any] | None,
        status: str | None,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
    ) -> ProducerEmissionResult:
        if tool_name != REPORT_TOOL_NAME:
            self.evidence.observe(
                tool_name=tool_name,
                status=status,
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            return ProducerEmissionResult(False, "evidence_observed")

        if (status or "").strip().lower() != "ok":
            return ProducerEmissionResult(False, "declaration_tool_failed")
        if not session_id or not turn_id or not tool_call_id:
            return ProducerEmissionResult(False, "declaration_missing_turn_identity")
        declaration = WorkDeclaration.from_args(args or {})
        evidence = self.evidence.select(
            session_id=session_id,
            turn_id=turn_id,
            event_type=declaration.event_type,
            count=declaration.evidence_count,
        )
        event_key = sha256(
            "\0".join([session_id, turn_id, tool_call_id]).encode("utf-8")
        ).hexdigest()[:40]
        event_id = f"event:hermes:{event_key}"
        event = {
            "schema_version": WORK_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "idempotency_key": f"idem:hermes:{event_key}",
            "work_id": declaration.work_id,
            "context_ref": _context_ref(session_id),
            "producer_id": self.credentials.principal_id,
            "event_type": declaration.event_type,
            "significance": declaration.significance,
            "semantic": {
                "label": declaration.label,
                "summary": declaration.summary,
                "metrics": dict(declaration.metrics),
            },
            "evidence": {
                "verified": True,
                "refs": [item.evidence_ref for item in evidence],
            },
            "supersedes_event_id": None,
            "observed_at": _now_utc(),
        }
        receipt = self.client.post_event(event)
        return ProducerEmissionResult(
            True,
            "event_emitted",
            event_id=event_id,
            receipt=receipt,
        )


def create_work_producer(config: BridgeConfig | None = None) -> HermesWorkProducer | None:
    cfg = config or BridgeConfig.from_env()
    if not cfg.work_producer_enabled:
        return None
    return HermesWorkProducer(cfg)


def report_work_event_handler(args: dict, **kwargs: Any) -> str:
    """Validate an explicit declaration; post_tool_call performs evidence binding/emission."""
    try:
        declaration = WorkDeclaration.from_args(args or {})
    except WorkProducerValidationError:
        return json.dumps(
            {"error": "invalid_work_event_declaration"},
            sort_keys=True,
            separators=(",", ":"),
        )
    return json.dumps(
        {
            "ok": True,
            "status": "declaration_validated",
            "work_id": declaration.work_id,
            "event_type": declaration.event_type,
            "evidence_count": declaration.evidence_count,
            "note": "emission_requires_matching_current_turn_tool_evidence",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _context_ref(session_ref: str) -> str:
    digest = sha256(session_ref.encode("utf-8")).hexdigest()[:40]
    return f"context:hermes:{digest}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_endpoint(value: str) -> str:
    try:
        parsed = urlparse.urlparse(value)
    except ValueError as exc:
        raise WorkProducerValidationError("work_producer_endpoint_invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise WorkProducerValidationError("work_producer_endpoint_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkProducerValidationError("work_producer_endpoint_must_not_embed_credentials")
    if parsed.path not in {"", "/"}:
        raise WorkProducerValidationError("work_producer_endpoint_must_be_origin_only")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise WorkProducerValidationError("work_producer_http_endpoint_must_be_loopback")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{parsed.port}"


def _exact_mapping(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value.keys()) != keys:
        raise WorkProducerValidationError(f"{name}_fields_must_be_exact")
    return value


def _opaque_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise WorkProducerValidationError(f"{field_name}_must_be_bounded_opaque_id")
    if _ROUTE_PREFIX_RE.match(value) or _FEISHU_CHAT_RE.search(value):
        raise WorkProducerValidationError(f"{field_name}_must_not_be_delivery_route")
    return value


def _secret_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise WorkProducerValidationError(f"{field_name}_must_be_bounded_secret")
    return value


def _safe_semantic_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkProducerValidationError(f"{field_name}_must_be_trimmed_nonempty")
    if len(value) > maximum:
        raise WorkProducerValidationError(f"{field_name}_too_long")
    forbidden: Sequence[re.Pattern[str]] = (
        _CONTROL_RE,
        _URL_RE,
        _POSIX_PATH_RE,
        _WINDOWS_PATH_RE,
        _ROUTE_PREFIX_RE,
        _FEISHU_CHAT_RE,
        _BEARER_RE,
        _SECRET_ASSIGNMENT_RE,
        _API_KEY_RE,
        _SHELL_RE,
    )
    if any(pattern.search(value) for pattern in forbidden):
        raise WorkProducerValidationError(f"{field_name}_contains_forbidden_material")
    return value


def _safe_metrics(value: Any) -> Mapping[str, int | float]:
    if not isinstance(value, Mapping) or len(value) > _MAX_METRICS:
        raise WorkProducerValidationError("metrics_must_be_bounded_mapping")
    result: dict[str, int | float] = {}
    for key, metric in value.items():
        if not isinstance(key, str) or not _METRIC_KEY_RE.fullmatch(key):
            raise WorkProducerValidationError("metric_key_not_allowed")
        if isinstance(metric, bool) or not isinstance(metric, (int, float)):
            raise WorkProducerValidationError("metric_must_be_number")
        numeric = float(metric)
        if not math.isfinite(numeric) or abs(numeric) > _MAX_METRIC_ABS:
            raise WorkProducerValidationError("metric_out_of_range")
        result[key] = metric
    return dict(sorted(result.items()))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
