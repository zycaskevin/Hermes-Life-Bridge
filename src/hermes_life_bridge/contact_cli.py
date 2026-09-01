from __future__ import annotations
import argparse, asyncio
from .config import BridgeConfig
from .contact_service import ContactService

def main():
    p=argparse.ArgumentParser(prog="hermes-life-contact")
    p.add_argument("command",choices=["serve"])
    args=p.parse_args()
    if args.command=="serve":
        asyncio.run(ContactService(BridgeConfig.from_env()).serve())

if __name__=="__main__": main()
