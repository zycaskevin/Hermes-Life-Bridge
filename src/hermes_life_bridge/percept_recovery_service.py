from __future__ import annotations

import argparse
import json
import time

from .config import BridgeConfig
from .percept_delivery import PerceptReliabilityExecutor


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-life-percept-recovery")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    if args.interval < 0.1:
        raise SystemExit("interval must be >= 0.1 seconds")
    executor = PerceptReliabilityExecutor(BridgeConfig.from_env())
    if args.once:
        print(json.dumps(executor.pump(), sort_keys=True))
        return 0
    while True:
        executor.pump()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
