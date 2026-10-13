"""Bound retention of this run's checkpoints and export its private LoRA locally.

Run only in the isolated tools/tinker-sft project. This performs metadata/storage
operations, never model sampling or training. Signed download URLs stay in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import tinker

STORAGE_PER_GB_MONTH = Decimal("0.10")
SHORTEST_MONTH_SECONDS = Decimal(28 * 24 * 3600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=86400)
    parser.add_argument("--storage-cap-usd", type=Decimal, required=True)
    args = parser.parse_args()
    if not 3600 <= args.ttl_seconds <= 86400:
        raise SystemExit("checkpoint retention must be between one and 24 hours")
    if not args.storage_cap_usd.is_finite() or args.storage_cap_usd <= 0:
        raise SystemExit("storage cap must be finite and positive")
    if args.out.exists() or args.export_dir.exists():
        raise SystemExit("receipt and export paths must be new; do not renew a lease implicitly")
    rows = [json.loads(line) for line in args.checkpoints.read_text().splitlines() if line.strip()]
    paths = sorted({str(row[key]) for row in rows for key in ("state_path", "sampler_path") if row.get(key)})
    if not paths:
        raise SystemExit("no owned checkpoint paths in the trainer receipt")
    parsed = {path: tinker.types.ParsedCheckpointTinkerPath.from_tinker_path(path) for path in paths}
    client = tinker.ServiceClient().create_rest_client()
    started = datetime.now(UTC)
    # Apply a server-side expiry before downloads or any further paid inference.
    for path in paths:
        client.set_checkpoint_ttl_from_tinker_path(path, args.ttl_seconds).result(timeout=90)
    checkpoints = {}
    for run_id in sorted({value.training_run_id for value in parsed.values()}):
        response = client.list_checkpoints(run_id).result(timeout=90)
        checkpoints.update({entry.tinker_path: entry for entry in response.checkpoints if entry.tinker_path in parsed})
    entries = []
    total = Decimal(0)
    for path in paths:
        checkpoint = checkpoints.get(path)
        if checkpoint is None or checkpoint.expires_at is None or checkpoint.size_bytes is None:
            raise SystemExit("checkpoint size/expiry was not confirmed; do not start further paid work")
        expires = checkpoint.expires_at
        if not started < expires <= datetime.now(UTC) + timedelta(seconds=args.ttl_seconds + 30):
            raise SystemExit("provider checkpoint expiry is outside the requested retention window")
        seconds = Decimal(str((expires - checkpoint.time).total_seconds()))
        cost = Decimal(checkpoint.size_bytes) / Decimal(1_000_000_000) * STORAGE_PER_GB_MONTH * seconds / SHORTEST_MONTH_SECONDS
        total += cost
        entries.append({
            "tinker_path": path, "checkpoint_type": checkpoint.checkpoint_type,
            "size_bytes": checkpoint.size_bytes, "created_at": checkpoint.time.isoformat(),
            "expires_at": expires.isoformat(),
        })
    total = total.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    receipt = {
        "checked_at": datetime.now(UTC).isoformat(), "ttl_seconds": args.ttl_seconds,
        "checkpoints": entries, "storage_cost_upper_bound_usd": float(total),
        "storage_basis": "metadata bytes * $0.10/decimal GB/month * creation-to-expiry seconds / 28-day month; commitment estimate, not invoice",
        "storage_cap_usd": float(args.storage_cap_usd), "exports": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Persist confirmed expiry even if a later export fails.
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if total > args.storage_cap_usd:
        raise SystemExit("verified storage commitment exceeds its allocation; stop further paid work")
    args.export_dir.mkdir(parents=True, exist_ok=False)
    samplers = [entry for entry in entries if entry["checkpoint_type"] == "sampler"]
    latest_sampler = max(samplers, key=lambda entry: entry["created_at"], default=None)
    for entry in entries:
        if entry is not latest_sampler:
            continue
        size = entry["size_bytes"]
        if shutil.disk_usage(args.export_dir).free < 2 * size + 10_000_000_000:
            raise SystemExit("not enough local disk for the private LoRA archive and safety reserve")
        response = client.get_checkpoint_archive_url_from_tinker_path(entry["tinker_path"]).result(timeout=90)
        destination = args.export_dir / "sampler-final.tar"
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with urllib.request.urlopen(response.url, timeout=180) as source, destination.open("xb") as target:
                while chunk := source.read(1 << 20):
                    target.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
        except Exception:
            # Do not include a presigned URL in logs or exception text.
            raise SystemExit("private checkpoint export failed; confirmed server expiry is in the receipt") from None
        receipt["exports"].append({"tinker_path": entry["tinker_path"], "path": str(destination), "bytes": downloaded, "sha256": "sha256:" + digest.hexdigest()})
        args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
