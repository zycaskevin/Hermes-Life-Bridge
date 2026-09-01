# HLB-002 Evaluation Gate

Required for PASS:

1. `hermes-life-cognition.service` active.
2. Cognition Unix socket reachable.
3. Hermes API server health reachable on loopback.
4. `CognitiveTaskEnvelope` reaches HLB.
5. HLB calls `/v1/chat/completions` with task-isolated `X-Hermes-Session-Id`.
6. No `X-Hermes-Session-Key` is sent by default.
7. CognitiveReceipt echoes task/life/basis-state/projection/request hashes.
8. Duplicate submission of the same task returns cached receipt and does not call Hermes twice.
9. L2/L3 tasks are rejected from autonomous HLB-002.
10. Life Runtime stores the receipt as a candidate; it does not directly mutate LiveState, memory, or personality.
