from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from campaign.keyenv import valid_name
    from campaign.spend_keys import read_counters
    from campaign.system import RealSystem

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not valid_name(args[0]):
        print("campaign: keyprobe: expected OPENROUTER_KEY_<LANE>", file=sys.stderr)
        return 2
    counters, problem = read_counters(RealSystem(), args[0])
    if counters is None:
        print(f"campaign: keyprobe: {problem}", file=sys.stderr)
        return 1
    remaining = counters["limit_remaining"]
    if counters["limit"] is None or remaining is None:
        print("campaign: keyprobe: a finite key limit and remaining amount are required", file=sys.stderr)
        return 1
    print(format(remaining, "f"))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
