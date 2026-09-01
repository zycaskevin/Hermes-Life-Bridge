# Security

HLB-001 is an observer bridge. It must not:
- block/rewrite user messages;
- persist raw message text;
- print API keys/tokens;
- directly authorize high-risk actions.

Bridge errors are intentionally fail-open for the Hermes conversation but fail-visible
through trace/doctor telemetry.
