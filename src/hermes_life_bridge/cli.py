from __future__ import annotations

import argparse
import json

from .bridge import HermesLifeBridge
from .compatibility import CompatibilityDiscovery
from .config import BridgeConfig
from .doctor import run_doctor
from .selftest import run_selftest
from .trace import BridgeTracer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-life")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("selftest")
    sub.add_parser("compatibility")

    trace = sub.add_parser("trace")
    trace.add_argument("--tail", type=int, default=20)

    emit = sub.add_parser("emit-test")
    emit.add_argument("--surface", choices=["gateway","cli"], default="cli")
    emit.add_argument("--session-id", default="manual")
    emit.add_argument("--turn-id", default="manual")
    emit.add_argument("--message", default="manual test")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = BridgeConfig.from_env()

    if args.command == "trace":
        print(
            json.dumps(
                BridgeTracer(config.trace_path).tail(args.tail),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "selftest":
        result = run_selftest(config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 2

    if args.command == "compatibility":
        report = CompatibilityDiscovery(config).discover()
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.supported else 2

    if args.command == "emit-test":
        bridge = HermesLifeBridge(config)
        if args.surface == "gateway":
            class Event:
                source = "gateway"
                message_id = args.turn_id
                text = args.message
            result = bridge.gateway_message(Event(), session_ref=args.session_id)
        else:
            result = bridge.cli_turn(
                session_id=args.session_id,
                turn_id=args.turn_id,
                user_message=args.message,
            )
        if result is None:
            print(json.dumps({"ok": False, "error": "runtime_unavailable"}, indent=2))
            return 2
        data = result.__dict__ if hasattr(result, "__dict__") else {"ok": bool(result)}
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if data.get("ok") else 2

    print(json.dumps(run_doctor(config), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
