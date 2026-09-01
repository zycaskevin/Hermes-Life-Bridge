# HLB-003 Evaluation

Release gate requires:
- Life Runtime contact-governance tests PASS;
- HLB contact-delivery tests PASS;
- delivery disabled by default;
- dry-run does not invoke `hermes send`;
- one-shot armed E2E sends exactly one unique message;
- replaying the same intent does not call backend again;
- delivery configuration is restored to disabled after real E2E;
- contact operational DB contains message hash/metadata but not raw message;
- DeliveryReceipt does not directly mutate LiveState;
- doctor sees runtime, cognition and contact sockets healthy.
