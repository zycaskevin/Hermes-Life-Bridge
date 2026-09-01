# Contributing

Run:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

Any hook or transport change must include:
- privacy test;
- idempotency test;
- fail-open test;
- trace-stage evidence.
