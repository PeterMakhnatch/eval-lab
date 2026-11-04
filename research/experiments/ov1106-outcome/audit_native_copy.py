"""Measure native DEV copy integration through actual OFF/ON line admission.

Audit-derived positives are not independent labels. This model-free measurement
does not grade trials or execute recorded commands. Output contains metadata only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evallab import copy_check as checker
from evallab import hack_drop as hd
from evallab.line.copy_history import history_copy
from evallab.line.core import _admit_sources


def _ordinary_bytes(item: hd.RolloutItem) -> bytes:
    """Serialize ordinary content without the opt-in retained evidence."""
    return json.dumps(
        [
            item.item_id,
            item.task_id,
            item.diff_text,
            item.files,
            item.metadata,
            item.run.doc if item.run else None,
        ],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    manifests = [path.resolve() for path in args.manifest]
    declared = {}
    manifest_hashes = {}
    for manifest in manifests:
        data = manifest.read_bytes()
        manifest_hashes[str(manifest)] = _sha256(data)
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = hd.strict_json_loads(line)
            if row.get("split") != "dev":
                raise ValueError("Native audit accepts explicit DEV rows only")
            if row["id"] in declared:
                raise ValueError("Duplicate native audit identity")
            declared[row["id"]] = row

    spec = {
        "sources": [
            {
                "name": f"native-{index}",
                "corpus": f"native-{index}",
                "rollouts": str(path),
                "split": "dev",
            }
            for index, path in enumerate(manifests)
        ]
    }
    source_names = [
        "src/evallab/line/core.py",
        "src/evallab/line/copy_history.py",
        "src/evallab/copy_check.py",
        "src/evallab/rollout_admission.py",
        "src/evallab/hack_drop.py",
        str(Path(__file__).resolve().relative_to(root)),
    ]
    source_hashes = {name: _sha256((root / name).read_bytes()) for name in source_names}
    off = _admit_sources({**spec, "copy_history": False}, root)
    on = _admit_sources({**spec, "copy_history": True}, root)
    if {item.item_id for item in on.items} != set(declared):
        raise ValueError("ON admission identities differ from declared identities")
    if {item.item_id for item in off.items} != set(declared):
        raise ValueError("OFF admission identities differ from declared identities")

    results = []
    for enabled, disabled, on_receipt, off_receipt in zip(
        on.items, off.items, on.rows, off.rows, strict=True
    ):
        if enabled.item_id != disabled.item_id:
            raise ValueError("OFF/ON admission order differs")
        row = declared[enabled.item_id]
        if enabled.task_id != row["task_id"] or disabled.task_id != row["task_id"]:
            raise ValueError("Admitted task identity differs from manifest")
        trial = Path(row["path"])
        reward_file = trial / "verifier/reward.txt"
        reward = float(reward_file.read_text().strip()) if reward_file.is_file() else None
        old = checker.copy_check(trial)
        new = history_copy(enabled)
        # Diagnostic only: the production line skips this matcher when OFF.
        off_match = history_copy(disabled)
        on_bytes, off_bytes = _ordinary_bytes(enabled), _ordinary_bytes(disabled)
        results.append(
            {
                "id": enabled.item_id,
                "task_id": enabled.task_id,
                "split": "dev",
                "original_reward": reward,
                "available": enabled.run is not None
                and bool(
                    dict(enabled.retained_outputs).get("verifier/agent.diff")
                    or dict(enabled.retained_outputs).get("final.diff")
                    or enabled.diff_text
                ),
                "audit_copy": old is not None,
                "line_copy": new is not None,
                "off_evidence_match": off_match is not None,
                "audit_method": old.get("match_method") if old else None,
                "line_method": new.get("match_method") if new else None,
                "line_evidence": new,
                "off_evidence_method": off_match.get("match_method") if off_match else None,
                "audit_source_artifact": old.get("source_artifact") if old else None,
                "off_intake": off_receipt,
                "on_intake": on_receipt,
                "on_off_intake_equal": on_receipt == off_receipt,
                "off_ordinary_sha256": _sha256(off_bytes),
                "on_ordinary_sha256": _sha256(on_bytes),
                "ordinary_rollout_equal": on_bytes == off_bytes,
                "retained_files": [name for name, _ in enabled.retained_outputs],
            }
        )
    if any(_sha256((root / name).read_bytes()) != digest for name, digest in source_hashes.items()):
        raise ValueError("Measurement source changes during measurement")
    if any(_sha256(path.read_bytes()) != manifest_hashes[str(path)] for path in manifests):
        raise ValueError("Manifest changes during measurement")
    summary = {
        "rows": len(results),
        "original_passes": sum(r["original_reward"] == 1 for r in results),
        "audit_copies_all": sum(r["audit_copy"] for r in results),
        "line_copies_all": sum(r["line_copy"] for r in results),
        "audit_copied_passes": sum(r["audit_copy"] and r["original_reward"] == 1 for r in results),
        "line_copied_passes": sum(r["line_copy"] and r["original_reward"] == 1 for r in results),
        "off_evidence_copies_all": sum(r["off_evidence_match"] for r in results),
        "off_evidence_copied_passes": sum(
            r["off_evidence_match"] and r["original_reward"] == 1 for r in results
        ),
        "on_off_intake_equal": all(r["on_off_intake_equal"] for r in results),
        "ordinary_rollout_equal": all(r["ordinary_rollout_equal"] for r in results),
        "measurement": "Actual _admit_sources OFF/ON, admit_rollout and manifest propagation; history_copy reads admitted evidence only.",
        "off_note": "OFF evidence matches are diagnostic; the OFF production line does not invoke history_copy.",
        "model_calls": 0,
        "grading": False,
        "recorded_commands_executed": False,
        "warning": "Audit-derived labels are not independent ground truth; do not call this recall/FPR or clean accuracy.",
        "source_sha256": source_hashes,
        "manifest_sha256": manifest_hashes,
        "rows_detail": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in {"rows_detail", "source_sha256", "manifest_sha256"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
