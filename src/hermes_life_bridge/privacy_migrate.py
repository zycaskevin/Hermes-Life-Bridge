from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import os
import sys

FORBIDDEN_KEYS = {
    "target", "canonical_target", "reply_target", "chat_id", "thread_id"
}


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret:
                out = out.replace(secret, "[REDACTED]")
        return out
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    if isinstance(value, dict):
        return _redact_record(value, secrets)
    return value


def _redact_record(record: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key.lower() in FORBIDDEN_KEYS:
            continue
        out[key] = _redact_value(value, secrets)
    return out


def sanitize_trace(route_path: str, trace_path: str) -> dict[str, Any]:
    route_file = Path(route_path)
    trace_file = Path(trace_path)
    if not trace_file.exists():
        return {"changed": False, "lines": 0, "original_sha256": ""}

    route = {}
    if route_file.exists():
        try:
            route = json.loads(route_file.read_text(encoding="utf-8"))
        except Exception:
            route = {}

    secrets = []
    for key in ("target", "chat_id", "thread_id"):
        value = str(route.get(key) or "")
        if value and value not in secrets:
            secrets.append(value)

    original = trace_file.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    changed = False
    rows = []

    for raw in original.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(raw)
        except Exception:
            # Do not preserve unknown raw lines that could contain route identifiers.
            redacted = raw
            for secret in secrets:
                redacted = redacted.replace(secret, "[REDACTED]")
            rows.append(redacted)
            changed = changed or redacted != raw
            continue

        safe = _redact_record(record, secrets)
        encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False)
        rows.append(encoded)
        changed = changed or safe != record

    if changed:
        tmp = trace_file.with_suffix(trace_file.suffix + ".privacy-migrate")
        tmp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(trace_file)
        os.chmod(trace_file, 0o600)

    return {
        "changed": changed,
        "lines": len(rows),
        "original_sha256": original_sha,
    }


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: privacy_migrate.py ROUTE_FILE TRACE_FILE")
    result = sanitize_trace(sys.argv[1], sys.argv[2])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
