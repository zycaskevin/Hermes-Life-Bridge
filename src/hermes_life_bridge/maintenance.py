from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .config import BridgeConfig
from .operation_store import OperationStore


def _checkpoint_sqlite(path: str) -> bool:
    candidate = Path(path)
    if not path or not candidate.exists():
        return False
    try:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        try:
            candidate.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def run_maintenance(config: BridgeConfig | None = None) -> dict[str, object]:
    config = config or BridgeConfig.from_env()
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=config.operation_retention_seconds
    )
    store = OperationStore(config.operation_db)
    try:
        purged = store.purge_terminal(
            before=cutoff.isoformat().replace("+00:00", "Z"),
            limit=10000,
        )
        if purged:
            store.compact()
        else:
            store.checkpoint()
    finally:
        store.close()
    checkpointed = {
        "operation_db": True,
        "cognition_db": _checkpoint_sqlite(config.cognition_db),
        "contact_db": _checkpoint_sqlite(config.contact_db),
    }
    return {
        "ok": True,
        "purged_terminal_operations": purged,
        "contact_dedupe_retained": True,
        "checkpointed": checkpointed,
        "retention_seconds": config.operation_retention_seconds,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-life-maintenance")
    parser.parse_args(argv)
    print(json.dumps(run_maintenance(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
