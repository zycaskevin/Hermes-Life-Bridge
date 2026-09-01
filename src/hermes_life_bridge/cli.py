from __future__ import annotations
import argparse
import json
import sys
from .config import BridgeConfig
from .doctor import run_doctor
from .selftest import run_selftest
from .trace import BridgeTracer

def main():
    parser = argparse.ArgumentParser(prog="hermes-life")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor")
    sub.add_parser("selftest")
    trace = sub.add_parser("trace")
    trace.add_argument("--tail", type=int, default=20)

    args = parser.parse_args()
    config = BridgeConfig.from_env()

    if args.command == "trace":
        print(json.dumps(
            BridgeTracer(config.trace_path).tail(args.tail),
            indent=2,
            ensure_ascii=False,
        ))
        return
    if args.command == "selftest":
        result = run_selftest(config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result.get("ok"):
            raise SystemExit(2)
        return
    print(json.dumps(run_doctor(config), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
