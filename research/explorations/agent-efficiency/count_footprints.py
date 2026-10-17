"""Count frozen readout files with OMP's embedded tokenizers, never joined path text.

Offline encoder counts omit request wrappers and are not provider-billed usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-results", type=Path)
    parser.add_argument("--trajectory-results", type=Path)
    parser.add_argument("--budget", type=int, default=12000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.context_results is None and args.trajectory_results is None:
        parser.error("supply context or trajectory measurements")
    inputs = []
    manifests = []
    if args.context_results is not None:
        data = args.context_results.read_bytes()
        manifests.append({"kind": "context", "sha256": hashlib.sha256(data).hexdigest()})
        for row in json.loads(data)["rows"]:
            if row["budget"] == args.budget:
                inputs.append(
                    {
                        "kind": "context",
                        "case": row["case"],
                        "arm": row["arm"],
                        "path": args.context_results.parent / row["pack_file"],
                        "sha256": row["sha256"],
                        "bytes": row["bytes"],
                        "estimated_tokens": row["estimated_tokens"],
                    }
                )
    if args.trajectory_results is not None:
        data = args.trajectory_results.read_bytes()
        manifests.append({"kind": "trajectory", "sha256": hashlib.sha256(data).hexdigest()})
        for index, row in enumerate(json.loads(data)["rows"]):
            for arm in ("full", "summary"):
                inputs.append(
                    {
                        "kind": "trajectory",
                        "case": row["source_sha256"],
                        "arm": arm,
                        "path": args.trajectory_results.parent / f"{index:03d}-{arm}.json",
                        "sha256": row[f"{arm}_sha256"],
                        "bytes": row[f"{arm}_bytes"],
                    }
                )
    rows = []
    for item in inputs:
        path = item["path"].resolve()
        source = path.read_bytes()
        if len(source) != item["bytes"] or hashlib.sha256(source).hexdigest() != item["sha256"]:
            raise RuntimeError("measurement source changed")
        # Variadic omp toks INPUTs are joined as literal text: one file per call.
        result = json.loads(
            subprocess.check_output(["omp", "toks", str(path), "--json"], timeout=60)
        )
        reported_source = result.get("source", {})
        if (
            reported_source.get("kind") != "file"
            or Path(reported_source.get("path", "")).resolve() != path
            or result.get("bytes") != len(source)
            or result.get("chars") != len(source.decode())
        ):
            raise RuntimeError("tokenizer did not count the intended source file")
        counters = result.get("encodings", [])
        if not counters or any(
            type(c.get("tokens")) is not int or c["tokens"] < 0 for c in counters
        ):
            raise RuntimeError("missing or invalid offline token counts")
        rows.append(
            {
                **{k: v for k, v in item.items() if k != "path"},
                "encodings": counters,
            }
        )
    report = {
        "omp_version": subprocess.check_output(["omp", "--version"], timeout=30).decode().strip(),
        "counter": "embedded offline tokenizers, exactly one verified file per invocation",
        "scope_limit": "Encoder labels are tool-advertised compatibility, not served-model attestation or provider usage. Request wrappers, caching, output and reasoning are excluded.",
        "source_measurements": manifests,
        "rows": rows,
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "files": len(rows),
                "encodings_per_file": len(rows[0]["encodings"]) if rows else 0,
                "model_calls": 0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
