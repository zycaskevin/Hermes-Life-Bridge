from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest

from hermes_life_bridge import research_freshness as policy


def _report(days: int = 7):
    now = datetime.now(timezone.utc)
    utc = lambda value: value.isoformat().replace('+00:00', 'Z')
    return {
        'schema': policy.SCHEMA,
        'enforced': True,
        'date_basis': 'publisher_declared_publication_time',
        'claim_event_dates_verified': False,
        'older_fallback_used': False,
        'window': {
            'from': utc(now - timedelta(days=days)),
            'to': utc(now),
            'reference_time': utc(now),
            'clock_source': 'server_utc',
            'bounds': 'inclusive',
            'constraint': {'mode': 'relative', 'max_age_days': days},
        },
        'received_count': 1,
        'eligible_count': 1,
        'selected_count': 1,
        'excluded_counts': {
            'older_than_window': 0,
            'newer_than_window': 0,
            'missing_or_invalid_date': 0,
        },
        'status': 'IN_WINDOW',
        'coverage': 'SPARSE',
        'sources': [{
            'source_ref': 'https://example.com/source',
            'published_at': utc(now - timedelta(days=1)),
            'read_refs': ['https://example.com/source'],
        }],
    }


def test_requested_days_parses_supported_windows():
    assert policy.requested_days('Hermes past week') == 7
    assert policy.requested_days('Hermes 最近一週') == 7
    assert policy.requested_days('Hermes releases', explicit=30) == 30


def test_requested_days_rejects_conflicts_and_invalid_bounds():
    with pytest.raises(policy.ResearchFreshnessError):
        policy.requested_days('past week', explicit=30)
    with pytest.raises(policy.ResearchFreshnessError):
        policy.requested_days('public topic', explicit=0)


def test_validate_receipt_accepts_bound_publication_window():
    report = _report(7)
    now = time.time()
    checked = policy.validate_receipt(
        report,
        days=7,
        started_at=now - 5,
        finished_at=now + 5,
        source_refs=['https://example.com/source'],
    )
    assert checked['selected_count'] == 1
    assert checked['sources'][0]['source_ref'] == 'https://example.com/source'


def test_validate_receipt_rejects_older_fallback_and_wrong_window():
    report = _report(7)
    report['older_fallback_used'] = True
    with pytest.raises(policy.ResearchFreshnessError):
        policy.validate_receipt(
            report,
            days=7,
            started_at=None,
            finished_at=time.time(),
            source_refs=['https://example.com/source'],
        )

    report = _report(7)
    report['window']['constraint']['max_age_days'] = 30
    with pytest.raises(policy.ResearchFreshnessError):
        policy.validate_receipt(
            report,
            days=7,
            started_at=None,
            finished_at=time.time(),
            source_refs=['https://example.com/source'],
        )
