# DLCORE-003B — Hermes Life Bridge native observation

This is an owner-side, read-only projection for Digital Life Core conformance. It does
not add a health daemon to HLB and does not move channel or Hermes authority into Core.

`hermes_life_bridge.core_observation` consumes:

- the operator-selected `channel-adapter` Component Manifest;
- an independently supplied canonical DLI root;
- HLB's native `BridgeConfig` life binding;
- a read-only Lifetime Hub subject verification;
- local Runtime and Contact Unix-socket reachability.

The Core-facing observation contains no socket paths, API keys, chat identifiers,
routes, messages, model output or credentials.

The Core channel profile declares `receive`, `deliver`, and `health`. Therefore HLB is
reported `ready=true` only when the Runtime ingress socket and Contact socket are
reachable **and** external contact delivery is explicitly enabled. Keeping delivery
disabled remains a valid safe HLB posture, but the common channel profile reports it as
`degraded / ready=false` rather than pretending delivery is available.

The Unix probe establishes a connection and immediately closes it without sending a
request body. Existing HLB services treat EOF as no operation. Tests use disposable
Unix sockets and prove no message/cognition/contact operation is created by the Core
projection.

This health projection is not proof of a real Telegram/LINE send, Hermes model
execution, Runtime Authority, action authorization, component attachment, or human UAT.
