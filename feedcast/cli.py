"""`feedcast` command: each module registers its own subcommands (CONTRACT §7)."""
from __future__ import annotations

import argparse
import importlib
import sys

MODULES = ["profile", "capture", "schedule", "doctor", "ingest", "report", "feedback", "setup"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="feedcast", description="Your LinkedIn feed, filtered to your work.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in MODULES:
        try:
            mod = importlib.import_module(f"feedcast.{name}")
        except ModuleNotFoundError as e:
            if e.name == f"feedcast.{name}":
                continue  # not built yet
            raise
        mod.register(sub)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
