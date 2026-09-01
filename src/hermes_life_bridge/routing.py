from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os

NON_DELIVERY_PLATFORMS = {"", "gateway", "cli", "hlb-selftest"}

def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", None)
    return str(raw if raw is not None else value)

def _source_dict(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, dict):
        return dict(source)
    to_dict = getattr(source, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
    data = {}
    for name in ("platform", "chat_id", "thread_id", "message_id", "chat_type", "user_id"):
        if hasattr(source, name):
            try:
                data[name] = getattr(source, name)
            except Exception:
                pass
    return data

@dataclass(frozen=True)
class HermesRoute:
    platform: str
    chat_id: str = ""
    thread_id: str = ""
    message_id: str = ""

    @property
    def target(self) -> str:
        if not self.platform:
            return ""
        if self.chat_id and self.thread_id:
            return f"{self.platform}:{self.chat_id}:{self.thread_id}"
        if self.chat_id:
            return f"{self.platform}:{self.chat_id}"
        return self.platform

def normalize_session_source(source: Any) -> HermesRoute:
    data = _source_dict(source)
    platform = _enum_value(data.get("platform")).strip().lower()
    chat_id = str(data.get("chat_id") or "").strip()
    thread_id = str(data.get("thread_id") or "").strip()
    message_id = str(data.get("message_id") or "").strip()

    if not data and isinstance(source, str):
        raw = source.strip()
        if raw and "(" not in raw and ")" not in raw and " " not in raw:
            platform = raw.lower()

    if platform.startswith("platform."):
        platform = platform.split(".", 1)[1]

    if "sessionsource" in platform or "(" in platform or ")" in platform:
        platform = ""

    return HermesRoute(platform, chat_id, thread_id, message_id)

def is_delivery_route(route: HermesRoute) -> bool:
    return bool(
        route.platform
        and route.platform not in NON_DELIVERY_PLATFORMS
        and route.chat_id
        and route.target
    )


class RouteStore:
    def __init__(self, path: str):
        self.raw_path = path or ""
        self.path = Path(path) if path else None

    def save(self, route: HermesRoute) -> None:
        # Backward-compatible no-op for tests/embedders constructing BridgeConfig
        # positionally without the newer route_path field.
        if self.path is None or not is_delivery_route(route):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "platform": route.platform,
            "chat_id": route.chat_id,
            "thread_id": route.thread_id,
            "message_id": route.message_id,
            "target": route.target,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def load(self):
        if self.path is None or not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None
