from __future__ import annotations
import argparse, asyncio, json
from .cognition_service import CognitionService
from .config import BridgeConfig
from .hermes_api import HermesApiClient


def main():
    p = argparse.ArgumentParser(prog="hermes-life-cognition")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("health")
    args = p.parse_args()
    config = BridgeConfig.from_env()
    if args.command == "health":
        result = HermesApiClient(config).health()
        print(json.dumps({"ok": True, "hermes": result, "cognition_socket": config.cognition_socket}, indent=2))
        return
    asyncio.run(CognitionService(config).serve())

if __name__ == "__main__":
    main()
