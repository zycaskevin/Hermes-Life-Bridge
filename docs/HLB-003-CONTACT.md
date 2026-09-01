# HLB-003 Contact Bridge

HLB-003 executes a Life Runtime `ContactDecision(outcome=contact)` through Hermes delivery.

Safety properties:
- default delivery is disabled (`HLB_CONTACT_DELIVERY_ENABLED=false`);
- only configured `HLB_CONTACT_TARGET` can receive delivery;
- intent expiry, message hash, life_did, evidence and decision correlation are validated;
- idempotency guarantees one backend send per intent;
- operational DB stores metadata/hash, not raw message text;
- actual platform delivery uses `hermes send --to ... --json` via argv, not a shell.
