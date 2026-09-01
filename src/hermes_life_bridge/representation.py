from __future__ import annotations
from enum import Enum
from typing import Any
import re

KNOWN_PLATFORMS = {"feishu","telegram","discord","slack","signal","sms","cli","gateway","hlb-selftest"}

SESSION_SOURCE_RE = re.compile(r"SessionSource\([^)]*\)", re.IGNORECASE)
PLATFORM_ENUM_RE = re.compile(
    r"(?:<)?Platform\.(?P<name>[A-Za-z0-9_-]+)(?::\s*['\"](?P<value>[A-Za-z0-9_-]+)['\"]>)?",
    re.IGNORECASE,
)
PLATFORM_FIELD_RE = re.compile(
    r"platform\s*=\s*(?:<Platform\.)?(?P<name>[A-Za-z0-9_-]+)(?::\s*['\"](?P<value>[A-Za-z0-9_-]+)['\"]>)?",
    re.IGNORECASE,
)
CANONICAL_TARGET_RE = re.compile(
    r"^(?P<platform>feishu|telegram|discord|slack|signal|sms):",
    re.IGNORECASE,
)
OBJECT_AT_RE = re.compile(r"<[^>]*\bobject at 0x[0-9A-Fa-f]+>")


def canonical_platform(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value).strip().lower()

    raw_value = getattr(value, "value", None)
    if raw_value is not None and not isinstance(value, str):
        candidate = str(raw_value).strip().lower()
        if candidate:
            return candidate

    s = str(value).strip()
    if not s:
        return ""

    low = s.lower()
    if low in KNOWN_PLATFORMS:
        return low

    target = CANONICAL_TARGET_RE.search(s)
    if target:
        return target.group("platform").lower()

    # Prefer explicit enum value if present, e.g. <Platform.FEISHU: 'feishu'>.
    m = PLATFORM_ENUM_RE.search(s)
    if m:
        return (m.group("value") or m.group("name")).lower()

    m = PLATFORM_FIELD_RE.search(s)
    if m:
        return (m.group("value") or m.group("name")).lower()

    if low.startswith("platform."):
        candidate = low.split(".", 1)[1]
        if candidate in KNOWN_PLATFORMS:
            return candidate

    # Simple non-repr tokens may be custom plugin platform names.
    if re.fullmatch(r"[a-z0-9_-]+", low):
        return low
    return ""


def sanitize_operational_string(value: str) -> str:
    s = value
    if not s:
        return s

    # Replace embedded SessionSource repr with canonical platform when recoverable.
    def _session_sub(match: re.Match) -> str:
        platform = canonical_platform(match.group(0))
        return platform or "[REDACTED_RUNTIME_OBJECT]"

    s = SESSION_SOURCE_RE.sub(_session_sub, s)

    def _platform_sub(match: re.Match) -> str:
        return (match.group("value") or match.group("name")).lower()

    s = PLATFORM_ENUM_RE.sub(_platform_sub, s)
    s = OBJECT_AT_RE.sub("[REDACTED_RUNTIME_OBJECT]", s)

    # Fail closed on any malformed leftovers.
    if "sessionsource(" in s.lower() or "platform." in s.lower() or "<platform." in s.lower():
        platform = canonical_platform(s)
        return platform or "[REDACTED_RUNTIME_OBJECT]"
    return s


def canonicalize_operational_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return canonical_platform(value)
    if isinstance(value, str):
        return sanitize_operational_string(value)
    if isinstance(value, dict):
        return {
            str(k): canonicalize_operational_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [canonicalize_operational_value(v) for v in value]

    # Known structured objects should become primitives, never repr().
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return canonicalize_operational_value(data)
        except Exception:
            pass

    platform = canonical_platform(value)
    if platform:
        return platform
    return "[REDACTED_RUNTIME_OBJECT]"


def contains_forbidden_representation_bytes(raw: bytes) -> bool:
    low = raw.lower()
    return any(
        marker in low
        for marker in (
            b"sessionsource(",
            b"platform.feishu",
            b"<platform.",
            b"object at 0x",
        )
    )
