from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib, json, os, re, sys

from .routing import HermesRoute, RouteStore

VERSION = "HLB-003.3"
SUPPORTED = r"(?:feishu|telegram|discord|slack|signal|sms)"
CANONICAL_TARGET_RE = re.compile(
    rf"(?P<platform>{SUPPORTED}):(?P<chat>[A-Za-z0-9_.@-]+)(?::(?P<thread>[A-Za-z0-9_.@-]+))?",
    re.IGNORECASE,
)
SESSION_SOURCE_RE = re.compile(r"SessionSource\([^)]*\)", re.IGNORECASE)
SESSION_PLATFORM_RE = re.compile(
    r"platform\s*=\s*(?:<Platform\.)?(?P<platform>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
SESSION_PLATFORM_VALUE_RE = re.compile(
    r"['\"](?P<platform>feishu|telegram|discord|slack|signal|sms)['\"]",
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r"chat_id\s*=\s*['\"](?P<chat>[A-Za-z0-9_.:@-]+)['\"]", re.IGNORECASE)
THREAD_ID_RE = re.compile(r"thread_id\s*=\s*['\"](?P<thread>[A-Za-z0-9_.:@-]+)['\"]", re.IGNORECASE)
FEISHU_CHAT_RE = re.compile(r"\boc_[A-Za-z0-9_-]+\b")
FORBIDDEN_KEYS = {"target", "canonical_target", "reply_target", "chat_id", "thread_id"}

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def trace_artifacts(state: Path) -> list[Path]:
    if not state.exists():
        return []
    return sorted(
        [
            p for p in state.iterdir()
            if p.is_file()
            and "trace" in p.name.lower()
            and "attestation" not in p.name.lower()
            and not p.name.lower().endswith(".sha256")
        ],
        key=lambda p: (p.stat().st_mtime_ns, p.name),
    )

def routes_from_text(text: str) -> list[HermesRoute]:
    out: list[HermesRoute] = []
    for m in CANONICAL_TARGET_RE.finditer(text):
        out.append(HermesRoute(
            m.group("platform").lower(),
            m.group("chat") or "",
            m.group("thread") or "",
            "",
        ))
    for m in SESSION_SOURCE_RE.finditer(text):
        body = m.group(0)
        platform = ""
        pm = SESSION_PLATFORM_RE.search(body)
        if pm:
            platform = pm.group("platform").lower()
            if platform == "platform":
                platform = ""
        if not platform:
            pv = SESSION_PLATFORM_VALUE_RE.search(body)
            platform = pv.group("platform").lower() if pv else ""
        cm = CHAT_ID_RE.search(body)
        tm = THREAD_ID_RE.search(body)
        chat = cm.group("chat") if cm else ""
        thread = tm.group("thread") if tm else ""
        if platform and chat:
            out.append(HermesRoute(platform, chat, thread, ""))
    return out

def recover_route(route_path: Path, artifacts: list[Path]) -> HermesRoute:
    candidates: list[HermesRoute] = []
    if route_path.exists():
        try:
            data = json.loads(route_path.read_text(encoding="utf-8"))
            r = HermesRoute(
                str(data.get("platform") or "").lower(),
                str(data.get("chat_id") or ""),
                str(data.get("thread_id") or ""),
                str(data.get("message_id") or ""),
            )
            if r.platform not in {"", "gateway", "cli", "hlb-selftest"} and r.chat_id:
                candidates.append(r)
        except Exception:
            pass
    for p in artifacts:
        candidates.extend(routes_from_text(p.read_text(encoding="utf-8", errors="replace")))
    candidates = [r for r in candidates if r.platform not in {"", "gateway", "cli", "hlb-selftest"} and r.chat_id]
    if not candidates:
        raise RuntimeError("cannot_recover_real_gateway_route")
    return candidates[-1]

def redact_string(value: str, route: HermesRoute) -> str:
    out = value
    for secret in (route.target, route.chat_id, route.thread_id):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    out = SESSION_SOURCE_RE.sub("[REDACTED_SESSION_SOURCE]", out)
    out = CANONICAL_TARGET_RE.sub(lambda m: f"{m.group('platform').lower()}:[REDACTED]", out)
    out = FEISHU_CHAT_RE.sub("[REDACTED_CHAT]", out)
    return out

def redact_value(value: Any, route: HermesRoute) -> Any:
    if isinstance(value, str):
        return redact_string(value, route)
    if isinstance(value, list):
        return [redact_value(x, route) for x in value]
    if isinstance(value, dict):
        return redact_record(value, route)
    return value

def redact_record(record: dict[str, Any], route: HermesRoute) -> dict[str, Any]:
    out = {}
    for key, value in record.items():
        if key.lower() in FORBIDDEN_KEYS:
            continue
        out[key] = redact_value(value, route)
    return out

def sanitize(path: Path, route: HermesRoute) -> dict[str, Any]:
    original = path.read_bytes()
    rows = []
    for raw in original.decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(raw)
        except Exception:
            rows.append(redact_string(raw, route))
        else:
            rows.append(json.dumps(redact_record(rec, route), sort_keys=True, ensure_ascii=False))
    clean = ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".0033")
    tmp.write_bytes(clean)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return {
        "name": path.name,
        "pre_redaction_sha256": sha(original),
        "post_redaction_sha256": sha(clean),
        "bytes_before": len(original),
        "bytes_after": len(clean),
    }

def assert_trace_clean(files: list[Path], route: HermesRoute) -> None:
    needles = [x.encode() for x in (route.target, route.chat_id, route.thread_id) if x]
    for p in files:
        if not p.exists():
            continue
        raw = p.read_bytes()
        for n in needles:
            if n in raw:
                raise RuntimeError(f"raw_route_identity_remains:{p.name}")
        text = raw.decode("utf-8", errors="replace")
        if SESSION_SOURCE_RE.search(text):
            raise RuntimeError(f"sessionsource_repr_remains:{p.name}")
        if CANONICAL_TARGET_RE.search(text):
            raise RuntimeError(f"canonical_target_remains:{p.name}")
        if FEISHU_CHAT_RE.search(text):
            raise RuntimeError(f"feishu_chat_id_remains:{p.name}")

def assert_exclusive_state_dir(state: Path, route_path: Path, attestation_path: Path, route: HermesRoute) -> None:
    needles = [x.encode() for x in (route.target, route.chat_id, route.thread_id) if x]
    for p in state.iterdir():
        if not p.is_file() or p == route_path or p == attestation_path:
            continue
        raw = p.read_bytes()
        for n in needles:
            if n in raw:
                raise RuntimeError(f"route_identity_outside_private_store:{p.name}")

def migrate(state_dir: str, route_path: str, primary_trace_path: str, attestation_path: str) -> dict[str, Any]:
    state = Path(state_dir)
    route_file = Path(route_path)
    primary = Path(primary_trace_path)
    attestation = Path(attestation_path)
    artifacts = trace_artifacts(state)
    if not artifacts:
        raise RuntimeError("no_trace_artifacts")
    route = recover_route(route_file, artifacts)
    route_pre_sha = sha(route_file.read_bytes()) if route_file.exists() else ""

    reports = [sanitize(p, route) for p in artifacts]
    assert_trace_clean(artifacts, route)

    # Restore the real route before any later self-test is allowed to run.
    RouteStore(str(route_file)).save(route)
    if not route_file.exists() or (route_file.stat().st_mode & 0o777) != 0o600:
        raise RuntimeError("private_route_store_invalid")

    backups = [p for p in artifacts if p.resolve() != primary.resolve()]
    att = {
        "version": VERSION,
        "created_at": now_iso(),
        "route_store_pre_sha256": route_pre_sha,
        "recovered_platform": route.platform,
        "recovered_target_sha256": sha(route.target.encode()),
        "recovered_chat_id_sha256": sha(route.chat_id.encode()),
        "trace_files": reports,
        "raw_route_identity_retained": False,
        "raw_backup_files_planned_for_removal": [p.name for p in backups],
    }
    attestation.write_text(json.dumps(att, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(attestation, 0o600)

    # Hash attestation exists; raw historical backups can now be removed.
    removed = []
    for p in backups:
        if p.exists():
            p.unlink()
            removed.append(p.name)

    att["raw_backup_files_removed"] = removed
    att["raw_backup_retained"] = False
    attestation.write_text(json.dumps(att, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(attestation, 0o600)

    remaining = [p for p in trace_artifacts(state) if p.resolve() != primary.resolve()]
    if remaining:
        raise RuntimeError("historical_trace_backup_remains")

    # Final fail-closed byte scan over the whole HLB state directory.
    assert_trace_clean([primary], route)
    assert_exclusive_state_dir(state, route_file, attestation, route)

    return {
        "ok": True,
        "platform": route.platform,
        "route_ready": True,
        "chat_id_present": bool(route.chat_id),
        "thread_id_present": bool(route.thread_id),
        "trace_files_migrated": len(artifacts),
        "raw_backup_files_removed": len(removed),
        "raw_backup_retained": False,
        "attestation_written": True,
        "route_store_mode": "0600",
    }

def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: privacy_migrate_v0033 STATE_DIR ROUTE_FILE PRIMARY_TRACE ATTESTATION")
    print(json.dumps(migrate(*sys.argv[1:]), sort_keys=True))

if __name__ == "__main__":
    main()
