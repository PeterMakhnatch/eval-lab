#!/usr/bin/env python3
"""Recompute DEV certification metadata. Never execute graders or print task text."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import sys
from pathlib import Path

RUNS = ("000682-r3", "002391-r2", "002391-r2-honest", "002139-r2")


def proportion(k: int, n: int) -> dict:
    """Descriptive Wilson interval; these selected cells are not a random sample."""
    if not n:
        return {"k": k, "n": n, "wilson95": None}
    z = 1.959963984540054
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {"k": k, "n": n, "fraction": p, "wilson95": [max(0, center - half), min(1, center + half)]}


class Sources:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    def data(self, path: Path) -> bytes:
        path = path.expanduser().absolute()
        if any(part.startswith("heldout-") or part == "answer-keys" for part in path.parts):
            raise ValueError(f"Forbidden evidence path: {path}")
        data = path.read_bytes()
        self.files[str(path)] = {
            "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "lines": len(data.splitlines()),
        }
        return data

    def load(self, path: Path):
        return json.loads(self.data(path))

    def record(self, path: Path) -> dict:
        self.data(path)
        return self.files[str(path.expanduser().absolute())]

    def occurrences(self, path: Path, pattern: str) -> dict:
        data = self.data(path)
        matches = [i for i, line in enumerate(data.decode(errors="replace").splitlines(), 1)
                   if re.search(pattern, line)]
        return {**self.record(path), "matching_lines": matches,
                "matching_lines_count": len(matches), "pattern": pattern}


def changed_lines_digest(data: bytes) -> str:
    # Compare changed lines only; diff headers and context can differ after normalization.
    lines = [line.rstrip() for line in data.decode(errors="replace").splitlines()
             if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def one_file(root: Path, name: str) -> Path:
    matches = [p for p in root.iterdir() if p.name == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one {name} in {root}; got {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    prior, root = args.prior.expanduser().resolve(), args.evidence_root.expanduser().resolve()
    src = Sources()
    checks: dict[str, bool] = {}
    validation_path = prior / "cert/validation.json"
    validation = src.load(validation_path)
    by_hash = {row["patch_sha256"]: row for row in validation}
    prereg_path = prior / "PREREG.md"
    prereg = src.data(prereg_path).decode()
    a0_match = re.search(r"`([^`]*A0/submissions-inventory\.json)`", prereg)
    if not a0_match:
        raise ValueError("Prior PREREG does not identify the A0 inventory")
    inventory_path = Path(a0_match[1]).expanduser()
    inventory = src.load(inventory_path)
    # The original experiment's scorecard names this DEV-admission receipt.
    scorecard_path = prior / "scorecard/build_scorecard.py"
    scorecard_source = src.data(scorecard_path).decode()
    match = re.search(r'UNIVERSE_ROWS\s*=\s*RESULTS\s*/\s*"([^"]+)"', scorecard_source)
    if not match:
        raise ValueError("Scorecard does not identify the DEV-admission receipt")
    results_root = inventory_path.parents[2]
    admission_path = results_root / match[1]
    admissions = src.load(admission_path)
    needed = {"format-code-task-" + run[:6] for run in RUNS}
    dev = {r["task_id"]: r.get("strictly_dev") for r in admissions["tasks"] if r["task_id"] in needed}
    if set(dev) != needed or not all(dev.values()):
        raise ValueError("DEV admission is absent or false; refusing task/test scans")

    run_records, plans, contracts, input_records = {}, {}, {}, {}
    for run in RUNS:
        runtime = root / "cert" / run
        committed_path = prior / "cert/runs" / run / "certification.json"
        runtime_path = runtime / "certification.json"
        cert = src.load(committed_path)
        checks[f"{run}:committed_runtime_bytes_equal"] = src.data(committed_path) == src.data(runtime_path)
        inputs = src.load(runtime / "inputs.json")
        input_records[run] = inputs
        plan = src.load(runtime / "plan.json")
        plans[run] = plan
        hardening = src.load(runtime / "hardening.json")
        # Resolve the contract from the preserved output-package manifest and its directory inventory.
        package = Path(hardening["output"])
        contract = one_file(package / "tests", "test_gold_contract.py")
        contracts[run] = contract
        src.record(contract)
        arms: dict[str, dict] = {}
        cell_records = []
        for cell in plan:
            receipt_path = runtime / "cells" / cell["name"] / "result.json"
            result = src.load(receipt_path)
            arm, key = cell["arm"], cell.get("key") or cell["name"]
            arms.setdefault(arm, {})[key] = result.get("reward")
            cell_records.append({"name": cell["name"], "arm": arm, "key": cell.get("key"),
                                 "reward": result.get("reward"), "source": src.record(receipt_path)})
        mappings = {"train": "train_rewards", "eval": "eval_rewards", "evalbase": "eval_base_rewards",
                    "alternative": "alternative_solution_rewards", "witness": "witness_rewards"}
        for arm, field in mappings.items():
            checks[f"{run}:{arm}_receipt_map_matches"] = arms.get(arm, {}) == cert.get(field, {})
        for arm, field in (("reference", "reference_reward"), ("nop", "nop_reward"), ("exit0_pth", "exit0_pth_reward")):
            checks[f"{run}:{arm}_receipt_matches"] = list(arms[arm].values()) == [cert[field]]
        checks[f"{run}:input_alternatives_match"] = set(inputs["alternative_patch_sha256"]) == set(cert["alternative_solution_rewards"])
        gate_values = {
            "reference": cert["reference_reward"] == 1,
            "nop": cert["nop_reward"] == 0,
            "witness": bool(arms.get("witness")) and all(v == 0 for v in arms["witness"].values()),
            "exit0_pth": cert["exit0_pth_reward"] == 0,
            "train": bool(arms.get("train")) and all(v == 0 for v in arms["train"].values()),
            "eval": bool(arms.get("eval")) and all(v == 0 for v in arms["eval"].values()),
            "alternatives": len(arms.get("alternative", {})) >= 2 and all(v == 1 for v in arms["alternative"].values()),
        }
        run_records[run] = {
            "recorded_status": cert["status"], "gates_passed": {"k": sum(gate_values.values()), "n": len(gate_values)},
            "gates": gate_values, "alternatives": cert["alternative_solution_rewards"],
            "acceptance": proportion(sum(v == 1 for v in cert["alternative_solution_rewards"].values()), len(cert["alternative_solution_rewards"])),
            "train_killed": proportion(sum(v == 0 for v in arms.get("train", {}).values()), len(arms.get("train", {}))),
            "eval_killed": proportion(sum(v == 0 for v in arms.get("eval", {}).values()), len(arms.get("eval", {}))),
            "certification_source": src.record(committed_path), "cells": cell_records,
            "contract": src.record(contract),
        }
        checks[f"{run}:status_matches_cells"] = (cert["status"] == "certified") == all(gate_values.values())

    joins = []
    for sha, reward in run_records["000682-r3"]["alternatives"].items():
        row = by_hash[sha]
        receipt = src.load(Path(row["cell_receipt"]))
        checks[f"000682:{sha}:original_validation_reward"] = receipt["reward"] == row["original_reward"] == 1
        joins.append({"patch_sha256": sha, "reward": reward, "worker": row["worker"],
                      "test_aware": row["test_aware"], "original_reward": row["original_reward"],
                      "validation_source": str(validation_path), "cell_receipt": row["cell_receipt"]})
    checks["000682:all_held_out_test_aware"] = bool(joins) and all(row["test_aware"] for row in joins)

    a, b = (src.load(root / "cert" / run / "certification.json") for run in ("002391-r2", "002391-r2-honest"))
    alt_a, alt_b = a["alternative_solution_rewards"], b["alternative_solution_rewards"]
    removed, added, retained = sorted(alt_a.keys() - alt_b.keys()), sorted(alt_b.keys() - alt_a.keys()), sorted(alt_a.keys() & alt_b.keys())
    checks["002391:expected_replacement"] = (removed == ["65e5213c707a08c82209c71446979690fcd7a85c9e3ad9ad194bc244a5415b49"] and added == ["b259e89bbc1da753c3e81a891677c00eed21e39f952db13cc3a3929eb8c9fe8c"] and retained == ["ac2df6277a4bb84f6ffddcec5e343120534bdf76ca8582cab0d46a743da986d5"])
    checks["002391:contracts_identical"] = src.data(contracts["002391-r2"]) == src.data(contracts["002391-r2-honest"])
    mutable = {"alternative_solution_rewards", "alternative_solutions_accepted", "alternative_solutions_total", "gate7_alternatives_passed", "status", "upgraded_package"}
    unchanged = {k: a[k] for k in a if k not in mutable and a[k] == b.get(k)}
    checks["002391:non_alternative_fields_unchanged"] = all(a[k] == b.get(k) for k in a if k not in mutable)
    split_path = root / "cert/002391-r2-synth/report.json"
    split = src.load(split_path)
    checks["002391:original_split_matches"] = set(split["held_out_sha256"]) == set(alt_a)
    checks["002391:contract_matches_synth_manifest"] = src.data(Path(split["test_path"])) == src.data(contracts["002391-r2"])
    patch_paths = dict(zip(split["shown_sha256"] + split["held_out_sha256"], split["shown"] + split["held_out"], strict=True))
    for sha, path in patch_paths.items():
        checks[f"002391:{sha}:patch_hash_matches_manifest"] = src.record(Path(path))["sha256"] == sha
    receipt_byte_identity = {}
    for cell in plans["002391-r2"]:
        if cell["arm"] != "alternative":
            left = root / "cert/002391-r2/cells" / cell["name"] / "result.json"
            right = root / "cert/002391-r2-honest/cells" / cell["name"] / "result.json"
            receipt_byte_identity[cell["name"]] = {
                "bytes_equal": src.data(left) == src.data(right),
                "left": str(left), "right": str(right),
            }
    for label, left, right in (
        ("retained", root / "cert/002391-r2/cells/alternative-1/result.json",
         root / "cert/002391-r2-honest/cells/alternative-0/result.json"),
        ("replacement", root / "cells/grade-002391-r2-heldout-a-flash/result.json",
         root / "cert/002391-r2-honest/cells/alternative-1/result.json"),
    ):
        receipt_byte_identity[label] = {
            "bytes_equal": src.data(left) == src.data(right),
            "left": str(left), "right": str(right),
        }
    replacement_validation = by_hash[added[0]]
    original_replacement_receipt = src.load(Path(replacement_validation["cell_receipt"]))
    checks["002391:replacement_original_reward"] = original_replacement_receipt["reward"] == replacement_validation["original_reward"] == 1

    provenance = {}
    task_rows = [r for r in inventory["submissions"] if r["task_id"] == "format-code-task-002391" and r.get("is_dev")]
    for name in ("real-1", "real-4"):
        normalized = next(Path(p) for p in split["held_out"] if Path(p).stem == name)
        signature = changed_lines_digest(src.data(normalized))
        matches = []
        for row in task_rows:
            source_patch = Path(row["patch_path"])
            if not source_patch.is_file():
                continue
            data = src.data(source_patch)
            if changed_lines_digest(data) != signature:
                continue
            reward_file = one_file(source_patch.parent, "reward.txt")
            recorded_reward = float(src.data(reward_file).decode().strip())
            checks[f"002391:{name}:source_patch_hash"] = hashlib.sha256(data).hexdigest() == row["patch_sha256"]
            checks[f"002391:{name}:source_reward_matches"] = recorded_reward == row["original_reward"]
            matches.append({**{k: row[k] for k in ("run_id", "patch_path", "patch_sha256", "original_reward", "is_dev")},
                            "arm_label": "cheat" if "-cheat-" in str(source_patch) else "honest" if "-honest-" in str(source_patch) else "unspecified",
                            "reward_file": src.record(reward_file), "recorded_reward": recorded_reward})
        checks[f"002391:{name}:unique_inventory_join"] = len(matches) == 1
        provenance[name] = {"normalized_patch": src.record(normalized), "changed_lines_sha256": signature,
                            "join_method": "SHA256 of ordered added/removed lines, right whitespace removed; excludes diff headers/context",
                            "source_matches": matches}
    honest_rows = [{k: r[k] for k in ("run_id", "patch_path", "patch_sha256", "original_reward", "is_dev")}
                   for r in task_rows if "-honest-" in r["patch_path"]]

    original_packages = {cell["package"] for cell in plans["002391-r2"] if cell["arm"] == "evalbase"}
    if len(original_packages) != 1:
        raise ValueError("Original DEV package identity is ambiguous")
    original_package = Path(next(iter(original_packages)))
    instruction = one_file(original_package, "instruction.md")
    test_patch = one_file(original_package / "tests", "test.patch")
    published = {
        "contract": src.occurrences(contracts["002391-r2"], r"assert.*published"),
        "instruction": src.occurrences(instruction, r"\bpublished\b"),
        "original_test_patch": src.occurrences(test_patch, r"\bpublished\b"),
        "scan_policy": "DEV admitted before local byte scan; no instruction or hidden-test text is emitted",
        "original_package_from": str(root / "cert/002391-r2/plan.json"),
        "assertion": "self.assertEqual(vulns[0].published, datetime(2024, 1, 1, tzinfo=timezone.utc))",
        "prior_verifier_misquote": {
            "quoted_line": 94, "quoted_assertion": "self.assertEqual(merged.published, pub)",
            "finding": "The quoted line and assertion do not match preserved contract bytes. Use the observed assertion and occurrence lines.",
        },
    }
    checks["002391:published_assertion_present"] = bool(published["contract"]["matching_lines"])
    checks["002391:published_absent_original_contract"] = not (published["instruction"]["matching_lines"] or published["original_test_patch"]["matching_lines"])
    mock = {
        "contract": src.occurrences(contracts["002139-r2"], r"project\s*=\s*['\"]UNSET['\"]"),
        "default_project_assertions": src.occurrences(contracts["002139-r2"], r"assert.*Client.*None"),
        "literal": "project=\"UNSET\"",
        "causality": "[INFERENCE] The sentinel default can reject a call that omits project. This audit does not execute the alternative.",
    }
    checks["002139:explicit_none_call_assertions_present"] = bool(mock["default_project_assertions"]["matching_lines"])
    checks["002139:unset_mock_default_present"] = bool(mock["contract"]["matching_lines"])
    # Preserve the failed receipt hash without emitting its traceback or task text.
    failed = [r for r in run_records["002139-r2"]["cells"] if r["arm"] == "alternative" and r["reward"] == 0]
    mock["failed_alternative_receipts"] = [r["source"] for r in failed]
    shown_metrics = {}
    for run, short, label in (("000682-r3", "000682", "000682-r3"), ("002391-r2", "002391", "002391-r2"), ("002139-r2", "002139", "002139-r2")):
        split_report = src.load(root / "cert" / f"{run}-synth" / "report.json")
        shown = []
        for path, sha in zip(split_report["shown"], split_report["shown_sha256"], strict=True):
            worker = Path(path).stem
            receipt_path = root / "cells" / f"grade-{short}-{label[len(short)+1:]}-shown-{worker}" / "result.json"
            receipt = src.load(receipt_path)
            shown.append({"patch_sha256": sha, "worker": worker, "reward": receipt["reward"], "source": src.record(receipt_path)})
        all_rewards = [r["reward"] for r in shown] + list(run_records[run]["alternatives"].values())
        shown_metrics[run] = {"shown": shown, "total_acceptance": proportion(sum(v == 1 for v in all_rewards), len(all_rewards))}
    shown_rewards = [r["reward"] for r in shown_metrics["002391-r2"]["shown"]]
    honest_rewards = shown_rewards + list(alt_b.values())
    shown_metrics["002391-r2-honest"] = {
        "shown": shown_metrics["002391-r2"]["shown"],
        "total_acceptance": proportion(sum(v == 1 for v in honest_rewards), len(honest_rewards)),
    }
    command = shlex.join(["python3", str(Path(__file__).resolve()), "--prior", str(prior), "--evidence-root", str(root), "--out", str(args.out.resolve())])
    src.record(Path(__file__).resolve())
    output = {
        "schema_version": "ov1104-cert-provenance-audit-v1", "command": command,
        "scope": "Retrospective metadata only. No inference, network, graders, exploit controls, or task-text output.",
        "dev_admission": {"source": str(admission_path), "strictly_dev": dev},
        "cert_input_manifests": input_records,
        "interval_policy": "Wilson95 describes selected binary grade outcomes only; no sampling/generalization claim. Identity, membership and gate counts have no inferential CI.",
        "runs": run_records, "000682_held_out_validation_join": joins,
        "002391_comparison": {"removed": removed, "added": added, "retained": retained,
                               "held_out_denominator_before": len(alt_a), "held_out_denominator_after": len(alt_b),
                               "denominator_count_changed": len(alt_a) != len(alt_b), "denominator_membership_changed": set(alt_a) != set(alt_b),
                               "unchanged_fields": unchanged, "contract_equal": checks["002391:contracts_identical"],
                               "receipt_byte_identity_observations": receipt_byte_identity,
                               "original_fixed_split_source": str(split_path), "original_split_acceptance": run_records["002391-r2"]["acceptance"],
                               "replacement_acceptance": run_records["002391-r2-honest"]["acceptance"]},
        "002391_A0_provenance": provenance, "002391_honest_arm_inventory": honest_rows,
        "arm_label_caveat": "Arm labels record experiment provenance, not semantic correctness. Passing a grader also does not prove semantic correctness. No copy/cheat behavior is inferred from an arm label.",
        "published_evidence": published, "UNSET_mock_evidence": mock, "shown_and_total": shown_metrics,
        "audit_checks": checks, "all_audit_checks_pass": all(checks.values()),
        "corrected_interpretation": {"000682-r3": "provisional: every held-out alternative is test-aware",
                                     "002391-r2-honest": "unsupported under the original fixed alternative split; contract unchanged and one failed alternative replaced",
                                     "002139-r2": "failed Gate 7; UNSET mock is an observed incidental default, not an executed repair"},
        "sources": sorted(src.files.values(), key=lambda r: r["path"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    report = ["# Certification provenance audit", "", "Metadata only. No grader or model runs. No task or reference text is included.", "",
              "## Recompute", "", "```bash", command, "```", "", "## Findings", "",
              "- `000682-r3`: 2/2 held-out alternatives are test-aware. This is deterministic provenance, not independent validation.",
              "- `002391-r2-honest`: the contract is unchanged. The held-out denominator remains 2; one alternative identity changes.",
              f"- Removed `{removed[0]}` at reward 0. Added `{added[0]}` at reward 1. Retained `{retained[0]}` at reward 1.",
              "- Gates 1–6 and their rewards remain unchanged. The original fixed split still fails Gate 7.",
              "- `real-1` and `real-4` join DEV A0 source runs with a `cheat` arm label and original reward 1.",
              "- An arm label does not prove semantic correctness or a cheating mechanism. A grader pass does not prove semantic correctness.",
              "- The genuine `honest` arm inventory row has original reward 0. It is not the `real-4` source.", "",
              "| Run | Gates (deterministic) | Held-out acceptance | Descriptive 95% Wilson CI |", "|---|---|---|---|"]
    for run, row in run_records.items():
        metric = row["acceptance"]
        lo, hi = metric["wilson95"]
        report.append(f"| {run} | {row['gates_passed']['k']}/{row['gates_passed']['n']} | {metric['k']}/{metric['n']} | {100*lo:.1f}–{100*hi:.1f}% |")
    report += ["", "Wilson intervals describe these selected cells. They do not establish population validity or independence.", "",
               "## Incidental requirements", "", f"- Published assertion: `{published['assertion']}`."]
    report += ["- The prior verifier quotes a different assertion at line 94. That quotation does not match the preserved bytes.",
               "- The actual assertion uses `vulns[0].published` at the line recorded below."]
    for label, record in (("Contract", published["contract"]), ("Original instruction", published["instruction"]), ("Original DEV test patch", published["original_test_patch"]), ("UNSET mock default", mock["contract"]), ("Project assertions", mock["default_project_assertions"])):
        report.append(f"- {label}: `{record['path']}`, lines {record['matching_lines']}; SHA256 `{record['sha256']}`.")
    report += ["- `published` occurs in neither the original instruction nor original DEV test patch. The script emits no content from either file.",
               "- [INFERENCE] The `UNSET` mock default can reject an implementation that omits the project argument. No repair runs here.", "", "## Source identities", ""]
    for name, record in provenance.items():
        for match in record["source_matches"]:
            report.append(f"- `{name}`: run `{match['run_id']}`, arm `{match['arm_label']}`, reward `{match['original_reward']}`; source `{match['patch_path']}`; SHA256 `{match['patch_sha256']}`.")
    report += ["", "## Checks and source ledger", "", f"All deterministic audit checks pass: `{output['all_audit_checks_pass']}`.",
               "The JSON contains each receipt hash, source path, per-cell reward, comparison, and source join.",
               "Receipt byte equality is an observation, not a required invariant. Different receipt bytes can record identical grades.",
               "Contract paths come from preserved hardening output manifests. Original package paths come from `plan.json` evalbase entries.",
               "The original split and normalized alternative paths come from the existing synthesis `report.json`.",
               "The A0 inventory path comes from the prior PREREG. The DEV-admission path comes from the prior scorecard source.", ""]
    for record in output["sources"]:
        report.append(f"- `{record['path']}` — SHA256 `{record['sha256']}`.")
    args.out.with_name("REPORT.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"out": str(args.out.resolve()), "report": str(args.out.with_name('REPORT.md').resolve()),
                      "all_audit_checks_pass": output["all_audit_checks_pass"], "runs": {k: {"gates_passed": v["gates_passed"], "acceptance": v["acceptance"]} for k, v in run_records.items()},
                      "comparison": {k: output["002391_comparison"][k] for k in ("removed", "added", "retained", "denominator_count_changed", "denominator_membership_changed", "contract_equal")}}, indent=2))
    return 0 if output["all_audit_checks_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
