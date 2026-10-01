"""Independent HLB validation of AF's publication-window receipt.

No memory reads, model calls, provider calls, or authoritative clock overrides.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc
SCHEMA = "agent-factory.research-freshness.v1"


class ResearchFreshnessError(ValueError):
    pass


def requested_days(query: str, explicit: int | None = None) -> int | None:
    values: set[int] = set()
    for number, unit in re.findall(r"\b(?:past|last)\s+(\d+|one|a)\s*(days?|weeks?)\b", query, re.I):
        n = 1 if number.lower() in {"one", "a"} else int(number)
        values.add(n * (7 if unit.lower().startswith("week") else 1))
    if re.search(r"\b(?:past|last)\s+week\b", query, re.I):
        values.add(7)
    for number, unit in re.findall(r"(?:最近|過去|过去|近)\s*(\d+|一|七)\s*(?:個|个)?\s*(天|日|週|周)", query):
        n = {"一": 1, "七": 7}.get(number)
        values.add((int(number) if n is None else n) * (7 if unit in {"週", "周"} else 1))
    if explicit is not None:
        if type(explicit) is not int or not 1 <= explicit <= 365:
            raise ResearchFreshnessError("research_time_constraint_invalid")
        values.add(explicit)
    if len(values) > 1:
        raise ResearchFreshnessError("research_time_constraint_conflict")
    days = next(iter(values), None)
    if days is not None and not 1 <= days <= 365:
        raise ResearchFreshnessError("research_time_constraint_invalid")
    return days


def _date(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ResearchFreshnessError("research_freshness_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchFreshnessError("research_freshness_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchFreshnessError("research_freshness_invalid")
    return parsed.astimezone(UTC)


def _count(value: Any, maximum: int = 48) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ResearchFreshnessError("research_freshness_invalid")
    return value


def replay_is_fresh(cached: dict, days: int | None, now: float) -> bool:
    if days is None:
        return True
    try:
        report = validate_receipt(cached.get("freshness"), days=days, started_at=now - 300,
                                  finished_at=now, source_refs=cached.get("sources", []))
        return report is not None and all(
            _date(source["published_at"]).timestamp() >= now - days * 86400
            for source in report["sources"]
        )
    except (ResearchFreshnessError, ValueError, TypeError, KeyError):
        return False


def validate_receipt(value: Any, *, days: int | None, started_at: float | None,
                     finished_at: float, source_refs: list[str], success: bool = True) -> dict | None:
    if days is None and value is None:
        return None  # Unconstrained v1 requests keep their historical wire compatibility.
    if days is None or not isinstance(value, dict):
        raise ResearchFreshnessError("research_freshness_required")
    if (value.get("schema") != SCHEMA or value.get("enforced") is not True
            or value.get("older_fallback_used") is not False
            or value.get("date_basis") != "publisher_declared_publication_time"
            or value.get("claim_event_dates_verified") is not False):
        raise ResearchFreshnessError("research_freshness_invalid")
    window = value.get("window")
    if not isinstance(window, dict) or window.get("clock_source") != "server_utc" or window.get("bounds") != "inclusive":
        raise ResearchFreshnessError("research_freshness_invalid")
    if window.get("constraint") != {"mode": "relative", "max_age_days": days}:
        raise ResearchFreshnessError("research_freshness_constraint_mismatch")
    start, end, reference = (_date(window.get(key)) for key in ("from", "to", "reference_time"))
    if end != reference or end - start != timedelta(days=days):
        raise ResearchFreshnessError("research_freshness_invalid")
    if started_at is not None and not started_at - 5 <= reference.timestamp() <= finished_at + 5:
        raise ResearchFreshnessError("research_freshness_clock_mismatch")
    received, eligible, selected = (_count(value.get(key)) for key in ("received_count", "eligible_count", "selected_count"))
    excluded = value.get("excluded_counts")
    reasons = {"older_than_window", "newer_than_window", "missing_or_invalid_date"}
    if not isinstance(excluded, dict) or set(excluded) != reasons:
        raise ResearchFreshnessError("research_freshness_invalid")
    exclusions = {key: _count(excluded[key]) for key in reasons}
    if not 0 <= selected <= min(eligible, 6) or eligible + sum(exclusions.values()) != received:
        raise ResearchFreshnessError("research_freshness_invalid")
    status = "IN_WINDOW" if success else "NO_RESULTS_IN_TIME_WINDOW"
    coverage = "NONE" if selected == 0 else "SPARSE" if selected < 3 else "BOUNDED"
    if (value.get("status") != status or value.get("coverage") != coverage
            or (success and selected == 0) or (not success and eligible != 0)):
        raise ResearchFreshnessError("research_freshness_invalid")
    sources = value.get("sources")
    if not isinstance(sources, list) or len(sources) != selected:
        raise ResearchFreshnessError("research_freshness_invalid")
    projected_sources = []
    urls: set[str] = set()
    original_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ResearchFreshnessError("research_freshness_invalid")
        ref = source.get("source_ref")
        read_refs = source.get("read_refs", [])
        if (not isinstance(ref, str) or not ref.startswith("https://") or len(ref) > 2048
                or ref in original_urls or not isinstance(read_refs, list) or len(read_refs) > 4
                or any(not isinstance(link, str) or not link.startswith("https://") or len(link) > 2048 for link in read_refs)):
            raise ResearchFreshnessError("research_freshness_invalid")
        date = _date(source.get("published_at"))
        if not start <= date <= end:
            raise ResearchFreshnessError("research_source_outside_time_window")
        original_urls.add(ref)
        urls.update([ref, *read_refs])
        projected_sources.append({"source_ref": ref, "published_at": date.isoformat().replace("+00:00", "Z"),
                                  "read_refs": list(read_refs)})
    if urls != set(source_refs):
        raise ResearchFreshnessError("research_freshness_sources_mismatch")
    return {"schema": SCHEMA, "enforced": True, "date_basis": value["date_basis"],
            "claim_event_dates_verified": False, "older_fallback_used": False,
            "window": {key: window[key] for key in ("from", "to", "reference_time", "clock_source", "bounds", "constraint")},
            "received_count": received, "eligible_count": eligible, "selected_count": selected,
            "excluded_counts": exclusions, "status": status, "coverage": coverage, "sources": projected_sources}
