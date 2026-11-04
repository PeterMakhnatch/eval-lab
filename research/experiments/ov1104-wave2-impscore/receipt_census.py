"""Check only containment receipt bindings, without reading trajectory or task data."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path)
    parser.add_argument("canary", type=Path)
    args = parser.parse_args()
    canary_bytes = args.canary.read_bytes()
    canary = json.loads(canary_bytes)
    digest = hashlib.sha256(canary_bytes).hexdigest()
    counts = Counter()
    for path in sorted(args.runs.glob("*/containment.json")):
        row = json.loads(path.read_bytes())
        peer = path.with_name("receipt.json")
        counts[(
            row["runtime"]["transport_sha256"],
            row["canary"]["sha256"] == digest,
            row["runtime"]["transport_sha256"] == canary["runtime"]["transport_sha256"],
            row["registry"]["all"] == ["bash"],
            row["registry"]["active"] == ["bash"],
            peer.is_file() and peer.read_bytes() == path.read_bytes(),
        )] += 1
    print("canary_sha256:", digest)
    print("canary_transport_sha256:", canary["runtime"]["transport_sha256"])
    print("receipts:", sum(counts.values()))
    for flags, count in sorted(counts.items()):
        transport, canary_match, transport_match, registry_exact, active_bash, receipt_match = flags
        print(
            f"transport={transport} canary_match={canary_match} transport_match={transport_match} "
            f"registry_exact={registry_exact} active_bash={active_bash} "
            f"receipt_bytes_match={receipt_match} count={count}"
        )
    if not counts:
        raise SystemExit("no containment receipts found")


if __name__ == "__main__":
    main()
