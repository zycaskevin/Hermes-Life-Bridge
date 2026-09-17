#!/usr/bin/env python3
"""Disposable HLB native-observation smoke for Digital Life Core."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

from hermes_life_bridge.core_observation import main as observe_main


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listening_socket(path: Path) -> tuple[socket.socket, threading.Thread]:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def accept_once() -> None:
        connection, _ = server.accept()
        connection.close()

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    return server, thread


def main() -> int:
    lifetime_src = os.environ.get("LIFETIME_HUB_CORE_OBSERVATION_PYTHONPATH")
    if not lifetime_src:
        raise RuntimeError("LIFETIME_HUB_CORE_OBSERVATION_PYTHONPATH is required")
    sys.path.insert(0, str(Path(lifetime_src).resolve()))
    from lifetime_hub import ContinuityStore, Provenance
    from lifetime_hub.canonical import utc_now

    root_hash = "8" * 64
    life_did = f"digital-life-identity:root:{root_hash}"
    with tempfile.TemporaryDirectory(prefix="hlb-core-observation-") as raw_root:
        root = Path(raw_root)
        db = root / "lifetime.sqlite3"
        runtime_socket = root / "runtime.sock"
        contact_socket = root / "contact.sock"
        config = root / "hlb.env"
        manifest = root / "component.json"
        output = root / "observation.json"
        provenance = Provenance(
            source_type="digital-life-identity",
            source_id=root_hash,
            actor_id="core-003b",
            captured_at=utc_now(),
            evidence_refs=(f"identity-root:{root_hash}",),
        )
        with ContinuityStore(db) as store:
            companion, _ = store.create_identity_companion(
                name="Core 003B HLB", identity_source=life_did, provenance=provenance
            )
            store.create_agent_definition(
                companion_id=companion.companion_id,
                runtime_contract_ref="life-runtime://core-003b",
                memory_scope_ref="dlmf://scope/did:core-003b:memory/life",
                personality_scope_ref="dld://core-003b",
                self_gateway_policy_ref="self-gateway://core-003b",
                capability_classes=("conversation",),
                provenance=Provenance(
                    source_type="agent-definition",
                    source_id="core-003b",
                    actor_id="core-003b",
                    captured_at=utc_now(),
                ),
            )
        config.write_text(
            "\n".join((
                f"LIVE_RUNTIME_LIFE_DID={life_did}",
                f"LIVE_RUNTIME_SOCKET={runtime_socket}",
                f"HLB_TRACE_PATH={root / 'trace.jsonl'}",
                f"HLB_COGNITION_SOCKET={root / 'cognition.sock'}",
                f"HLB_COGNITION_DB={root / 'cognition.sqlite3'}",
                "HLB_HERMES_API_BASE_URL=http://127.0.0.1:8642",
                f"HLB_HERMES_ENV={root / 'hermes.env'}",
                f"HLB_CONTACT_SOCKET={contact_socket}",
                f"HLB_CONTACT_DB={root / 'contact.sqlite3'}",
                "HLB_CONTACT_DELIVERY_ENABLED=true",
                "",
            )),
            encoding="utf-8",
        )
        manifest.write_text(json.dumps({
            "schema": "digital-life.component.v1",
            "componentId": "hlb-core-003b",
            "componentType": "channel-adapter",
            "contract": "dlcore/channel-reference/v1",
            "contractVersion": "1",
            "binding": {"scope": "digital-life", "lifeDid": life_did},
            "operations": ["receive", "deliver", "health"],
            "capabilities": ["telegram"],
            "authorityClaims": [],
            "endpoint": "unix:///tmp/hlb-core-003b.sock",
            "health": {
                "protocol": "digital-life.health.v1",
                "endpoint": "unix:///tmp/hlb-core-003b-health.sock",
                "readinessEndpoint": "unix:///tmp/hlb-core-003b-ready.sock",
            },
            "metadata": {"implementation": "hermes-life-bridge", "evidenceKind": "synthetic"},
        }, indent=2) + "\n", encoding="utf-8")

        runtime_server, runtime_thread = listening_socket(runtime_socket)
        contact_server, contact_thread = listening_socket(contact_socket)
        before = digest(db)
        try:
            status = observe_main([
                "--manifest", str(manifest),
                "--life-did", life_did,
                "--lifetime-db", str(db),
                "--companion-id", companion.companion_id,
                "--lifetime-pythonpath", str(Path(lifetime_src).resolve()),
                "--config-file", str(config),
                "--output", str(output),
            ])
        finally:
            runtime_server.close()
            contact_server.close()
            runtime_thread.join(timeout=1)
            contact_thread.join(timeout=1)
        if status != 0:
            raise RuntimeError("HLB observer rejected synthetic native fixture")
        after = digest(db)
        observed = json.loads(output.read_text(encoding="utf-8"))
        assert before == after
        assert observed["health"]["ready"] is True
        assert observed["readiness"]["ready"] is True
        assert observed["health"]["binding"]["lifeDid"] == life_did
        rendered = json.dumps(observed)
        assert str(runtime_socket) not in rendered
        assert str(contact_socket) not in rendered
        print(json.dumps({
            "result": "PASS",
            "evidenceKind": "synthetic-native-process",
            "scenarios": [
                "native-config-life-binding",
                "independent-lifetime-subject",
                "runtime-socket-reachable",
                "contact-socket-reachable",
                "delivery-explicitly-enabled",
                "observation-no-lifetime-write",
                "no-message-or-cognition-operation",
            ],
            "lifetimeWrites": 0,
            "messageOperations": 0,
            "attachmentAuthorized": False,
            "productionUat": "NOT_RUN",
        }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
