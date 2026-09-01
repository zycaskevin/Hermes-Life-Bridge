# Architecture

```text
Hermes Gateway                    Hermes CLI
     │                                │
pre_gateway_dispatch              pre_llm_call
     │ authoritative                  │ fallback
     └──────────────┬─────────────────┘
                    ▼
           Hermes Life Bridge
             normalize
             correlate
             dedupe
             trace
                    │
                    ▼
          Life Runtime socket
                    │
                    ▼
             PerceptReceipt
```

## Ownership

Hermes Life Bridge owns:
- Hermes hook compatibility
- transport/correlation normalization
- dedupe keys
- bridge observability
- bridge health/doctor
- adapter-side retry policy

It does not own:
- LiveState
- memory
- identity
- personality
- motivation
- contact policy
- cognitive routing


## HLB-002 cognition path

```text
Life Runtime CognitiveTask
        ↓ Unix socket
Hermes Life Bridge cognition service
        ↓ /v1/chat/completions
Hermes Agent (task-isolated session)
        ↓
CognitiveReceipt
        ↓
Life Runtime candidate validation/store
```

`X-Hermes-Session-Key` is deliberately omitted in HLB-002 so the bridge does not opt into Hermes long-term memory.
