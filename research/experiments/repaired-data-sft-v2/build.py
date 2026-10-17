"""Offline repaired-data SFT v2; no execution, inference, downloads or catalog writes.

Run with the pinned tools/modal-mimo-sft environment and HF_HUB_OFFLINE=1:
  python research/experiments/repaired-data-sft-v2/build.py \
    --inputs inputs.json --tokenizer-dir <local pinned snapshot> --epochs 1 --out <new dir>

inputs.json (repaired-data-sft-v2.inputs/1) binds every input as
{"path": <absolute or inputs-relative path>, "sha256": <full sha256, optional prefix>}:
  inventory: repaired-data-sft-v2.inventory/1, rows with result/trajectory refs and
             optional ancestry refs (native VariantRecords, not asserted parent labels).
  heldout: the frozen mimo.heldout/v1 document containing ALL 33 parents.
  v6_manifest: the existing mimo-clean-v6 CSV.
  repairs: [existing evallab.task_variant/v1 refs]; only verifier-repair@1 children
           of the exact v6 digest qualify, never a similarly named raw repair.
  controls: [existing qa-trace-counterfactual/2 controls.json refs].
  envcheck_manifest: optional existing repaired-v6-variants-manifest.json ref;
                     reads its actual retained Harbor repeated-control jobs.
  regrades: [existing QA counterfactual summary JSON or JSONL refs].
  audits: [existing QA full-prompt audit refs], optional.
  exclusions: [canonical training-excluded task IDs], optional additional exclusions.
  corrections: optional list of custody links, NOT a second EnvCheck export:
    {source: [native job UUID, trial UUID], corrected: [job UUID, trial UUID],
     feedback: <hash-bound actual failed native verifier/test_output.log>,
     failed_regrade: {document: <ref>, row: <zero-based index>},
     passed_regrade: {document: <ref>, row: <zero-based index>}}
D reads the same native result/ATIF and counterfactual records as B. It requires
an exact source-conversation prefix, one new user feedback turn containing ONLY
that actual test output, followed by different assistant targets and native replay
0 -> 1 on the SAME repaired digest. It refuses unavailable custody, source/test
leakage, unsupported evidence and empty correction input; it never manufactures D.
Both source and corrected native executions must use separate verifier environments;
the corrected job's hash-bound lab-metadata.json task_staging must bind the exact
repaired package and unchanged staged Harbor digest. No grader/reference file is
read as feedback.

B requires >=51 distinct full-conversation, nonzero-loss rows surviving BOTH exact
v6 and its verifier-repair@1 child with genuine native controls. C is a seeded-
permutation feasible subset of the same original-pass eligibility pool, NOT a
uniform subset draw, and must differ from B. B/C use common positive INTEGER
full epochs, loss=all, no packing, no max_steps and no truncation; B/C supervised
assistant-token presentations are exactly equal. The manifest is the schedule
contract; a later partial-epoch/early-stopped run cannot claim this match.
B/C's common original-pass frame is the actual built globally hardened v6 manifest
task domain, minus all frozen heldout parents/descendants and explicit exclusions.

--plan writes token accounting/qualification refusals before applying the B minimum
and does not export trainable arms. Normal build fails closed without >=51 B rows
or exact C; a missing D is explicit in the receipt (or fatal with --require-d).
D uses the existing loss=last contract for each genuinely new corrected assistant
completion with its entire recorded prompt prefix. Original false-pass assistant
turns remain context, never D targets; no future turns are included in a prompt.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
MINIMUM_B = 51
SCHEMA = "repaired-data-sft-v2.inputs/1"
FAULTS = {"native_faulty", "previously_reviewed_faulty", "static_fault_suspected", "unclear"}
V6_CHAIN = {"strip-future-history@1", "purge-build-caches@4", "mtime-normalize@2", "separate-verifier@10", "agent-network-none@1"}
# Observed bytes of the immutable model revision used by sft.py and v1. A local
# directory with a familiar name is not proof of tokenizer/template identity.
TOKENIZER_SHA256 = {
    "tokenizer.json": "06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523",
    "tokenizer_config.json": "792fa3f0cb88b111e54ef3134c873531008c4df471d108da17903426e308aa7b",
    "chat_template.jinja": "59a64ebb4df6d1489d09a91267cf3ceb106162d4a893c4f84833cfb8c897ff63",
    "vocab.json": "ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003",
    "merges.txt": "a9d356d7bdf1ef4949e3e748e95b8e10ad9d4e2e838eddc38a0a7b6b94d1db8d",
}
FROZEN_HELDOUT = frozenset(
    "mimo-v2.6-rl/format-code-task-" + number
    for number in (
        "001248", "000161", "000142", "000783", "001399", "001914", "002871",
        "002335", "001590", "001480", "001371", "001246", "001948", "001425",
        "002395", "002092", "000144", "002915", "000143", "000149", "002989",
        "001199", "000878", "001220", "000905", "002371", "001891", "002451",
        "002304", "000582", "000657", "000371", "001686",
    )
)


class BuildError(ValueError):
    """Missing or contradictory source evidence; never a clean observation."""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_task(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildError("missing task ancestry identity")
    task = value if "/" in value else "mimo-v2.6-rl/" + value
    if not re.fullmatch(r"mimo-v2\.6-rl/[A-Za-z0-9][A-Za-z0-9_.-]*", task):
        raise BuildError(f"unsupported task identity: {value!r}")
    return task


class Evidence:
    """Resolve hash-bound local files and retain every byte dependency in the receipt."""

    def __init__(self, base: Path):
        self.base = base
        self.files: dict[str, dict[str, Any]] = {}

    def path(self, path: str | Path, *, relative_to: Path | None = None) -> Path:
        p = Path(path).expanduser()
        return (p if p.is_absolute() else (relative_to or self.base) / p).resolve()

    def capture(self, path: str | Path, *, relative_to: Path | None = None) -> dict[str, Any]:
        p = self.path(path, relative_to=relative_to)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise BuildError(f"missing native evidence {p}: {exc}") from exc
        ref = {"path": str(p), "sha256": digest(data), "bytes": len(data)}
        old = self.files.get(str(p))
        if old is not None and old != ref:
            raise BuildError(f"evidence changed during build: {p}")
        self.files[str(p)] = ref
        return ref

    def bound(self, ref: dict[str, Any]) -> Path:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise BuildError("evidence must be a path/full-sha256 reference, not a label or boolean")
        expected = str(ref.get("sha256", "")).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BuildError(f"missing full evidence hash: {ref.get('path')}")
        got = self.capture(ref["path"])
        if got["sha256"] != "sha256:" + expected:
            raise BuildError(f"evidence hash mismatch: {ref['path']}")
        return Path(got["path"])

    def json(self, ref: dict[str, Any]) -> Any:
        try:
            data = self.bound(ref).read_bytes()
            if digest(data) != "sha256:" + str(ref["sha256"]).removeprefix("sha256:"):
                raise BuildError(f"JSON evidence changed after hash binding: {ref['path']}")
            return json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"invalid JSON evidence: {ref.get('path')}") from exc

    def observed_json(self, path: str | Path) -> Any:
        ref = self.capture(path)
        return self.json(ref)


def rows_from_document(evidence: Evidence, ref: dict[str, Any]) -> list[dict[str, Any]]:
    path = evidence.bound(ref)
    try:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        else:
            doc = json.loads(path.read_text())
            rows = doc if isinstance(doc, list) else doc.get("rows", [])
    except (ValueError, AttributeError) as exc:
        raise BuildError(f"invalid regrade document: {path}") from exc
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise BuildError(f"invalid regrade rows: {path}")
    return rows


def load_heldout(evidence: Evidence, ref: dict[str, Any]) -> set[str]:
    doc = evidence.json(ref)
    parents = [canonical_task(t) for t in doc.get("heldout", [])]
    if doc.get("schema") != "mimo.heldout/v1" or len(parents) != 33 or set(parents) != FROZEN_HELDOUT:
        raise BuildError("training exclusion requires the frozen ALL33 distinct heldout parents")
    return set(parents)


def ancestry(evidence: Evidence, refs: list[dict[str, Any]]) -> set[str]:
    """Resolve actual VariantRecord parent links, including renamed descendants."""
    parents: set[str] = set()
    seen: set[str] = set()
    completed: set[str] = set()

    def visit(ref: dict[str, Any], expected: str | None = None) -> None:
        path = evidence.bound(ref)
        if str(path) in seen:
            raise BuildError(f"cyclic ancestry record: {path}")
        seen.add(str(path))
        rec = evidence.json(ref)
        if rec.get("schema") != "evallab.task_variant/v1":
            raise BuildError(f"not a native VariantRecord: {path}")
        if expected is not None and rec.get("variant_digest") != expected:
            raise BuildError(f"ancestry parent digest mismatch: {path}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(rec.get("variant_digest", ""))):
            raise BuildError("native ancestry lacks its full variant digest")
        if str(path) in completed:
            seen.remove(str(path))
            return
        parents.add(canonical_task(rec["task_name"]))
        inputs = rec.get("inputs") or {}
        if inputs.get("parent_task"):
            parents.add(canonical_task(inputs["parent_task"]))
        parent = rec.get("parent") or {}
        source = parent.get("source") or {}
        if source.get("kind") == "variant":
            target = source.get("record")
            if not target:
                raise BuildError(f"missing parent VariantRecord: {path}")
            candidate = evidence.path(target, relative_to=REPO)
            visit(evidence.capture(candidate), parent.get("digest"))
        elif source.get("path"):
            task_path = evidence.path(source["path"]) / "task.toml"
            evidence.capture(task_path)
            config = tomllib.loads(task_path.read_text())
            parent_id = (config.get("metadata") or {}).get("source_id")
            if parent_id:
                parents.add(canonical_task(parent_id))
        seen.remove(str(path))
        completed.add(str(path))
    for ref in refs:
        visit(ref)
    return parents


@dataclass
class Source:
    key: tuple[str, str]
    task: str
    result: dict[str, Any]
    trajectory: dict[str, Any]
    result_path: Path
    trajectory_path: Path
    parents: set[str]
    provenance: dict[str, Any]


def native_source(evidence: Evidence, row: dict[str, Any]) -> Source:
    result_path = evidence.bound(row["result"])
    trajectory_path = evidence.bound(row["trajectory"])
    result, trajectory = evidence.json(row["result"]), evidence.json(row["trajectory"])
    job = (result.get("config") or {}).get("job_id")
    trial = result.get("id")
    if not all(isinstance(v, str) and v and "/" not in v and "\\" not in v for v in (job, trial)):
        raise BuildError(f"missing safe native (job_id, trial_id): {result_path}")
    if any(row.get(k) is not None and row[k] != v for k, v in (("job_id", job), ("trial_id", trial), ("execution_id", trial))):
        raise BuildError(f"inventory/native identity mismatch: {result_path}")
    task = canonical_task(result.get("task_name"))
    parents = {task} | ancestry(evidence, row.get("ancestry", []))
    task_path = ((result.get("config") or {}).get("task") or {}).get("path")
    if task_path and (Path(task_path) / "task.toml").is_file():
        toml_path = Path(task_path) / "task.toml"
        evidence.capture(toml_path)
        config = tomllib.loads(toml_path.read_text())
        original_id = (config.get("metadata") or {}).get("source_id")
        if original_id:
            parents.add(canonical_task(original_id))
    # Variant-like renamed code tasks cannot evade exclusion through their display name.
    if re.search(r"format-code-task-\d{6}.+", task) and len(parents) == 1:
        raise BuildError(f"renamed task lacks native parent ancestry: {task}")
    if row.get("metadata"):
        evidence.bound(row["metadata"])
    provenance = {k: row[k] for k in ("result", "trajectory", "metadata", "ancestry", "aliases") if k in row}
    code_parents = {p for p in parents if re.fullmatch(r"mimo-v2\.6-rl/format-code-task-\d{6}", p)}
    if len(code_parents) > 1:
        raise BuildError("native variant has ambiguous original task ancestry")
    if code_parents:
        provenance["native_task_name"] = task
        task = next(iter(code_parents))
    return Source((job, trial), task, result, trajectory, result_path, trajectory_path, parents, provenance)


def deduplicate(sources: list[Source]) -> list[Source]:
    by_key: dict[tuple[str, str], Source] = {}
    for source in sources:
        old = by_key.get(source.key)
        if old is not None:
            if old.task != source.task or old.trajectory != source.trajectory or old.result != source.result or old.parents != source.parents:
                raise BuildError(f"conflicting native copies: {source.key}")
            continue
        by_key[source.key] = source
    return [by_key[k] for k in sorted(by_key)]


def original_pass_eligible(source: Source, heldout: set[str], excluded: set[str], correction_keys: set[tuple[str, str]], *, v6_tasks: set[str] | None = None) -> None:
    if source.parents & (heldout | excluded):
        raise BuildError("heldout/excluded task parent or descendant")
    if v6_tasks is not None and source.task not in v6_tasks:
        raise BuildError("source is outside the actual built globally hardened v6 task domain")
    if source.key in correction_keys:
        raise BuildError("self-corrected targets are D, not original-pass B/C pool")
    if reward(source.result) != 1:
        raise BuildError("not an observed original native pass")


def reward(result: dict[str, Any]) -> Any:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return rewards.get("reward", rewards.get("primary_reward"))


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot import existing conversion code: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def conversion_modules() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(REPO / "src"))
    v1 = _module("rds2_lossless", REPO / "research/experiments/repaired-data-sft-v1/to_export.py")
    qa = _module("rds2_raw_messages", REPO / "research/experiments/qa-trace/training-arms/build_arms.py")
    return v1, qa, v1.sft


class Qualification:
    """Read existing QA native replay/control contracts; do not execute them."""

    def __init__(self, evidence: Evidence, inputs: dict[str, Any]):
        self.evidence = evidence
        self.v6_ref = inputs["v6_manifest"]
        with evidence.bound(self.v6_ref).open(newline="") as stream:
            self.v6 = {canonical_task(r["task_id"]): r for r in csv.DictReader(stream)}
        self.repairs: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for ref in inputs.get("repairs", []):
            rec = evidence.json(ref)
            if rec.get("schema") != "evallab.task_variant/v1" or rec.get("transform") != "verifier-repair@1":
                raise BuildError("repairs must be actual verifier-repair@1 VariantRecords")
            self.repairs.setdefault(canonical_task(rec["task_name"]), []).append((rec, ref))
        self.controls: dict[str, dict[str, Any]] = {}
        for ref in inputs.get("controls", []):
            ctrl = evidence.json(ref)
            label = ctrl.get("grader")
            if label in self.controls and self.controls[label] != ctrl:
                raise BuildError(f"conflicting native controls for {label}")
            self.controls[label] = ctrl
        self.envcheck_controls: dict[str, tuple[dict[str, Any], Path]] = {}
        if inputs.get("envcheck_manifest"):
            path = evidence.bound(inputs["envcheck_manifest"])
            manifest = evidence.json(inputs["envcheck_manifest"])
            if not isinstance(manifest.get("variants"), list):
                raise BuildError("EnvCheck input is not the existing repaired-v6 variant manifest")
            for variant in manifest["variants"]:
                expected = variant.get("variant_digest", "")
                label = f"verifier-repair@1:{expected.removeprefix('sha256:')[:12]}"
                if label in self.envcheck_controls:
                    raise BuildError("ambiguous retained EnvCheck repair/control identity")
                self.envcheck_controls[label] = (variant, path.parent)
        self.regrades: dict[str, list[dict[str, Any]]] = {}
        for ref in inputs.get("regrades", []):
            for row in rows_from_document(evidence, ref):
                self.regrades.setdefault(str(row.get("execution_id")), []).append(row)
        self.faults: dict[str, set[str]] = {}
        for ref in inputs.get("audits", []):
            for row in evidence.json(ref).get("observations", []):
                self.faults.setdefault(str(row.get("execution_id")), set()).add(row.get("evidence_stratum", "unclear"))
        self._packages: dict[str, dict[str, Any]] = {}
        self._checked_controls: set[str] = set()

    def package(self, path: str, expected: str, task: str) -> dict[str, Any]:
        from evallab.task_variants import task_directory_digest

        if expected not in self._packages:
            package = self.evidence.path(path, relative_to=REPO)
            if task_directory_digest(package) != expected:
                raise BuildError(f"grader package digest mismatch: {package}")
            # Bind all consumed package bytes, including hidden files, in the receipt.
            for file in sorted(package.rglob("*")):
                if file.is_file():
                    self.evidence.capture(file)
            config = tomllib.loads((package / "task.toml").read_text())
            parent = (config.get("metadata") or {}).get("source_id")
            if canonical_task(parent or (config.get("task") or {}).get("name")) != task:
                raise BuildError(f"grader package parent mismatch: {package}")
            self._packages[expected] = {"path": package, "config": config, "task": task}
        package_info = self._packages[expected]
        if package_info["task"] != task:
            raise BuildError("same package digest bound to different task parents")
        return package_info

    def target(self, row: dict[str, Any], source: Source) -> tuple[str, str, dict[str, Any]]:
        gs = row.get("grader_source") or {}
        task = canonical_task(gs.get("task_name", ""))
        transform, expected = gs.get("transform"), gs.get("variant_digest")
        if task != source.task or row.get("grader_digest") != expected or row.get("transform") != transform:
            raise BuildError("regrade task/transform/digest mismatch")
        if not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise BuildError("regrade lacks full exact grader digest")
        label = f"{transform}:{expected[7:19]}"
        if row.get("grader") != label:
            raise BuildError("regrade label does not bind its exact digest")
        v6 = self.v6.get(task)
        if not v6 or v6.get("status") != "built" or not V6_CHAIN <= set(v6.get("chain", "").split(">")):
            raise BuildError("missing globally hardened v6 ancestry")
        if transform == "mimo-clean-v6":
            if v6.get("final_digest") != expected:
                raise BuildError("v6 regrade is not the pinned v6 manifest package")
        elif transform == "verifier-repair@1":
            matching = [(rec, ref) for rec, ref in self.repairs.get(task, []) if rec.get("variant_digest") == expected]
            if len(matching) != 1:
                raise BuildError("missing or ambiguous repaired grader native ancestry")
            record, ref = matching[0]
            if (record.get("parent") or {}).get("digest") != v6["final_digest"]:
                raise BuildError("repair is not a child of the exact v6 package")
            if set(record.get("components_changed", [])) - {"verifier"}:
                raise BuildError("repair changes agent-visible task/environment/solution")
            if not record.get("files") or any(f.get("path") not in {"tests/test.patch", "tests/test_command.sh"} for f in record["files"]):
                raise BuildError("repair is not a test-only native variant")
            # The v6 manifest is the terminal parent authority; no registration/derivation.
            self.evidence.bound(ref)
        else:
            raise BuildError(f"unsupported clean grader transform: {transform}")
        package = self.package(gs.get("package", ""), expected, task)
        return transform, label, package

    def native_replay(self, row: dict[str, Any], expected: str, score: int, *, script: str | None = None) -> None:
        if row.get("refusals") or row.get("execution_error") or row.get("regraded") != score:
            raise BuildError("replay was refused, failed, unscored or has a different reward")
        if row.get("input_kind") != "native_patch_replay":
            raise BuildError("missing native patch replay custody (snapshot-only summaries need native matrix evidence)")
        receipt_path = self.evidence.path(row.get("matrix_receipt", ""))
        receipt = self.evidence.observed_json(receipt_path)
        matrix, outcomes = receipt.get("matrix") or {}, receipt.get("results") or []
        if matrix.get("task_package_digest") != expected or len(outcomes) != 1:
            raise BuildError("native matrix grader digest/result multiplicity mismatch")
        result = outcomes[0]
        if result.get("status") not in {"ok", "mismatch"} or result.get("error") or result.get("rewards") != [score] or result.get("staged_task_digest") != expected:
            raise BuildError("native matrix is not an observed scored replay on the exact grader")
        runs = matrix.get("runs") or []
        if len(runs) != 1 or runs[0].get("agent") != "oracle" or not runs[0].get("solution"):
            raise BuildError("native replay matrix lacks its source script")
        script_path = self.evidence.path(runs[0]["solution"], relative_to=receipt_path.parents[2])
        script_ref = self.evidence.capture(script_path)
        script_hash = script_ref["sha256"][7:]
        if row.get("script_sha256") != script_hash or result.get("solution_sha256") != script_hash:
            raise BuildError("native replay script hash mismatch")
        if script is not None and script_path.read_text() != script:
            raise BuildError("native replay script is not the actual source/control patch")

    def retained_envcheck_controls(self, label: str, expected: str, package: dict[str, Any]) -> None:
        """Consume actual EnvCheck Harbor results/stability, never manifest booleans alone."""
        variant, archive = self.envcheck_controls[label]
        if (
            variant.get("variant_digest") != expected
            or canonical_task(variant.get("task", "")) != package["task"]
            or variant.get("status") != "repaired"
            or variant.get("declared_survivors") != 0
            or variant.get("v6_oracle_x3") != [1, 1, 1]
            or variant.get("v6_nop_x3") != [0, 0, 0]
        ):
            raise BuildError("retained EnvCheck repair is partial, has survivors or lacks valid controls")
        short = package["task"].split("format-code-task-")[-1]
        for agent, score in (("oracle", 1), ("nop", 0)):
            job = archive / "v6-controls/jobs" / f"v6rep-{short}-{agent}"
            metadata = self.evidence.observed_json(job / "lab-metadata.json")
            summary = self.evidence.observed_json(job / "result.json")
            staging = metadata.get("task_staging") or {}
            if (
                metadata.get("timed_out") or metadata.get("exit_code") != 0
                or staging.get("source_package_digest") != expected
                or staging.get("declared_task_name") != package["task"]
                or not staging.get("source_harbor_digest")
                or staging.get("source_harbor_digest") != staging.get("staged_harbor_digest")
            ):
                raise BuildError("EnvCheck native control is not bound to the exact staged repaired grader")
            evaluations = (summary.get("stats") or {}).get("evals") or {}
            names = [
                name for evaluation in evaluations.values()
                for group in ((evaluation.get("reward_stats") or {}).get("reward") or {}).values()
                for name in group
            ]
            if len(names) != 1 or Path(names[0]).name != names[0]:
                raise BuildError("EnvCheck native control is missing or ambiguous")
            trial = job / names[0]
            result = self.evidence.observed_json(trial / "result.json")
            config = result.get("config") or {}
            if (
                config.get("job_id") != summary.get("id")
                or (config.get("agent") or {}).get("name") != agent
                or result.get("exception_info") is not None
                or reward(result) != score
                or canonical_task(result.get("task_name", "")) != package["task"]
            ):
                raise BuildError("EnvCheck native nop/reference control result disagrees with custody or reward")
            stability = self.evidence.observed_json(trial / "verifier/stability.json")
            runs = stability.get("runs") or []
            if (
                stability.get("schema") != "evallab.verifier_stability/v1"
                or stability.get("repeat_n_requested") != 3
                or stability.get("n_runs") != 3 or len(runs) != 3
                or any(run.get("reward") != score or run.get("error") for run in runs)
            ):
                raise BuildError("EnvCheck native control lacks three actual consistent scored verifications")
            reward_file = trial / "verifier/reward.txt"
            self.evidence.capture(reward_file)
            if float(reward_file.read_text().strip()) != score:
                raise BuildError("EnvCheck native reward file disagrees with its result")

    def controls_ok(self, label: str, expected: str, package: dict[str, Any]) -> None:
        if label in self._checked_controls:
            return
        if label in self.envcheck_controls:
            self.retained_envcheck_controls(label, expected, package)
            if label not in self.controls:
                self._checked_controls.add(label)
                return
        from evallab.qa import counterfactual as cf

        ctrl = self.controls.get(label)
        if not ctrl or ctrl.get("engine") != cf.ENGINE_VERSION or ctrl.get("nop") != 0 or ctrl.get("reference") != 1 or ctrl.get("valid") is not True:
            raise BuildError("missing valid native nop/reference controls; scalar regrade controls are not evidence")
        patch = cf.reference_patch(package["task"], REPO)
        if patch is None or hashlib.sha256(patch.encode()).hexdigest() != ctrl.get("reference_sha256"):
            raise BuildError("native reference control lacks the exact retained reference patch binding")
        workdir = package["config"]["environment"]["workdir"]
        control_rows = ctrl.get("rows") or {}
        self.native_replay(control_rows.get("nop") or {}, expected, 0, script=cf._patch_script("", workdir))
        kept, _ = cf.strip_binary_artifacts(patch)
        self.native_replay(control_rows.get("reference") or {}, expected, 1, script=cf._patch_script(kept, workdir))
        self._checked_controls.add(label)

    def regrade(self, row: dict[str, Any], source: Source, score: int) -> tuple[str, str]:
        from evallab.qa import counterfactual as cf

        if row.get("engine") != cf.ENGINE_VERSION or row.get("execution_id") != source.key[1]:
            raise BuildError("regrade is not bound to the native source execution")
        trial = self.evidence.path(row.get("trial", ""))
        original = self.evidence.observed_json(trial / "result.json")
        key = ((original.get("config") or {}).get("job_id"), original.get("id"))
        if key != source.key or reward(original) != 1 or row.get("recorded") != 1:
            raise BuildError("regrade source identity/original-pass binding mismatch")
        if self.evidence.capture(trial / "agent/trajectory.json")["sha256"] != self.evidence.capture(source.trajectory_path)["sha256"]:
            raise BuildError("regrade and training targets use different source trajectory bytes")
        if cf.source_digest(trial) != row.get("source_digest"):
            raise BuildError("regrade source digest is stale or unbound")
        # source_digest reads these paths, so retain the same present-byte dependencies.
        for relative in ("config.json", "verifier/agent.diff", "artifacts/manifest.json", "artifacts/var/tmp/mimo-separate/workspace.tgz", "artifacts/logs/agent/trajectory.json"):
            if (trial / relative).is_file():
                self.evidence.capture(trial / relative)
        transform, label, package = self.target(row, source)
        expected = row["grader_digest"]
        self.controls_ok(label, expected, package)
        diff_path = trial / "verifier/agent.diff"
        self.evidence.capture(diff_path)
        kept, _ = cf.strip_binary_artifacts(diff_path.read_text())
        script = cf._patch_script(kept, package["config"]["environment"]["workdir"])
        self.native_replay(row, expected, score, script=script)
        if row.get("outcome") != ("stays" if score == 1 else "flips"):
            raise BuildError("regrade outcome disagrees with native observed rewards")
        return transform, expected

    def clean(self, source: Source) -> list[dict[str, Any]]:
        if self.faults.get(source.key[1], set()) & FAULTS:
            raise BuildError("source has an unresolved/observed functional fault")
        rows = self.regrades.get(source.key[1], [])
        if not rows:
            raise BuildError("no hash-bound clean regrade evidence")
        accepted: list[dict[str, Any]] = []
        kinds: set[str] = set()
        errors = []
        for row in rows:
            try:
                kind, _ = self.regrade(row, source, int(row.get("regraded") == 1))
                if row.get("regraded") != 1:
                    raise BuildError("source fails a qualified clean grader")
                kinds.add(kind)
                accepted.append(row)
            except BuildError as exc:
                errors.append(str(exc))
        # Refused/malformed supplied evidence is not silently ignored in favor of a pass.
        if errors:
            raise BuildError("; ".join(sorted(set(errors))))
        if kinds != {"mimo-clean-v6", "verifier-repair@1"}:
            raise BuildError("requires observed pass on BOTH mimo-clean-v6 and verifier-repair@1")
        return accepted


@dataclass
class Prepared:
    source: Source
    messages: list[dict[str, Any]]
    sequence_tokens: int
    assistant_loss_tokens: int
    provenance: dict[str, Any] = field(default_factory=dict)
    loss: str = "all"


def require_minimum(rows: list[Prepared], minimum: int = MINIMUM_B) -> None:
    if minimum < MINIMUM_B:
        raise BuildError(f"B-v2 minimum cannot be smaller than {MINIMUM_B}")
    distinct = {row.source.key for row in rows if row.assistant_loss_tokens > 0}
    if len(distinct) != len(rows) or len(distinct) < minimum:
        raise BuildError(f"B-v2 insufficient distinct nonzero-loss verified-clean rows: supplied={len(rows)}, distinct_nonzero={len(distinct)}, required={minimum}")


def matched_control(pool: list[Prepared], b: list[Prepared], seed: int) -> list[Prepared]:
    """Exact positive-weight subset sum, randomized order; not uniform subset sampling.

    Two reachability bitsets track whether a non-B row has been included. Thus even
    when B is the only feasible subset this refuses instead of returning B itself.
    Prefix bitsets permit deterministic reconstruction without millions of Python
    predecessor objects. No row repetition, partial conversation or token padding.
    """
    b_keys = {r.source.key for r in b}
    target = sum(r.assistant_loss_tokens for r in b)
    if target <= 0 or any(r.assistant_loss_tokens <= 0 for r in pool):
        raise BuildError("exact exposure matching requires positive assistant-loss token weights")
    if len({r.source.key for r in pool}) != len(pool) or not b_keys <= {r.source.key for r in pool}:
        raise BuildError("C pool must contain distinct original-pass rows including every B source")
    ordered = sorted(pool, key=lambda row: row.source.key)
    random.Random(seed).shuffle(ordered)
    cap = (1 << (target + 1)) - 1
    history = [(1, 0)]
    for row in ordered:
        without, with_other = history[-1]
        shift = row.assistant_loss_tokens
        if row.source.key in b_keys:
            history.append(((without | (without << shift)) & cap, (with_other | (with_other << shift)) & cap))
        else:
            history.append((without, (with_other | ((without | with_other) << shift)) & cap))
    if not ((history[-1][1] >> target) & 1):
        raise BuildError(f"C-v2 infeasible exact subset sum: target_loss_tokens={target}, pool_rows={len(pool)}; B itself is not a control")
    remaining, other, selected = target, True, []
    for index in range(len(ordered) - 1, -1, -1):
        row = ordered[index]
        without, with_other = history[index]
        previous = with_other if other else without
        if (previous >> remaining) & 1:
            continue
        weight = row.assistant_loss_tokens
        selected.append(row)
        remaining -= weight
        if row.source.key not in b_keys and other:
            other = not bool((without >> remaining) & 1)
    if remaining or other or sum(r.assistant_loss_tokens for r in selected) != target or {r.source.key for r in selected} == b_keys:
        raise BuildError("exact subset-sum reconstruction failed")
    return sorted(selected, key=lambda row: row.source.key)


def correction_environment(evidence: Evidence, corrected: Source, expected: str) -> None:
    """A repaired grader is not permission to expose that grader to the agent."""
    if corrected.result.get("verifier_environment_mode") != "separate":
        raise BuildError("D correction lacks a native separate-verifier source boundary")
    ref = corrected.provenance.get("metadata")
    if not ref:
        raise BuildError("D missing hash-bound native correction task-staging metadata")
    staging = evidence.json(ref).get("task_staging") or {}
    if (
        staging.get("source_package_digest") != expected
        or staging.get("declared_task_name") != corrected.task
        or not staging.get("source_harbor_digest")
        or staging.get("source_harbor_digest") != staging.get("staged_harbor_digest")
    ):
        raise BuildError("D correction was not executed on the exact separate repaired grader")


def correction_row(evidence: Evidence, link: dict[str, Any], sources: dict[tuple[str, str], Source], qualification: Qualification, heldout: set[str], qa: Any) -> Source:
    try:
        original, corrected = sources[tuple(link["source"])], sources[tuple(link["corrected"])]
    except (KeyError, TypeError) as exc:
        raise BuildError("D missing genuine source/corrected native execution") from exc
    if original.key == corrected.key or original.task != corrected.task or (original.parents | corrected.parents) & heldout:
        raise BuildError("D correction identity/task/heldout ancestry refusal")
    if reward(original.result) != 1 or reward(corrected.result) != 1:
        raise BuildError("D requires an original native false pass and an observed corrected native pass")
    if original.result.get("verifier_environment_mode") != "separate":
        raise BuildError("D original context has no native hidden-test/source separation evidence")
    before_ref, after_ref = link["failed_regrade"], link["passed_regrade"]
    if any(type(ref.get("row")) is not int or ref["row"] < 0 for ref in (before_ref, after_ref)):
        raise BuildError("D regrade custody needs nonnegative integer row indices")
    try:
        before = rows_from_document(evidence, before_ref["document"])[before_ref["row"]]
        after = rows_from_document(evidence, after_ref["document"])[after_ref["row"]]
    except (IndexError, TypeError, KeyError) as exc:
        raise BuildError("D missing actual failed/passed repaired regrade rows") from exc
    kind_before, digest_before = qualification.regrade(before, original, 0)
    kind_after, digest_after = qualification.regrade(after, corrected, 1)
    if kind_before != "verifier-repair@1" or kind_after != kind_before or digest_before != digest_after:
        raise BuildError("D requires false-pass -> true-pass on the same exact repaired grader")
    correction_environment(evidence, corrected, digest_before)
    feedback_path = evidence.bound(link["feedback"])
    feedback = feedback_path.read_text()
    if not feedback.strip():
        raise BuildError("D actual failing repaired-test output is empty")
    # The feedback must be an actual retained native verifier output, not a hand-written
    # summary, a reference diff, hidden source, or another task's feedback.
    failed_job = evidence.path(before["job_dir"])
    if failed_job not in feedback_path.parents or feedback_path.parent.name != "verifier" or feedback_path.name != "test_output.log":
        raise BuildError("D feedback is not retained output of the actual failing native repaired-test run")
    messages_before, _, fidelity_before = qa.to_messages(original.trajectory)
    messages_after, _, fidelity_after = qa.to_messages(corrected.trajectory)
    if fidelity_before != "raw_completion" or fidelity_after != "raw_completion":
        raise BuildError("D needs actual raw assistant self-correction targets")
    prefix = len(messages_before)
    if messages_after[:prefix] != messages_before or len(messages_after) < prefix + 2:
        raise BuildError("D correction does not preserve the complete original conversation")
    if messages_after[prefix] != {"role": "user", "content": feedback}:
        raise BuildError("D must expose only exact failing-test output, not hidden tests/reference/instructions")
    suffix = messages_after[prefix + 1:]
    if suffix[0].get("role") != "assistant" or not any(m.get("role") == "assistant" and (m.get("content") or m.get("reasoning_content") or m.get("tool_calls")) for m in suffix):
        raise BuildError("D has no corrected assistant targets")
    if qa.network_fetch(corrected.trajectory):
        raise BuildError("D correction contains observed network answer retrieval")
    previous_targets = [m for m in messages_before if m.get("role") == "assistant"]
    if not any(m.get("role") == "assistant" and m not in previous_targets for m in suffix):
        raise BuildError("D repeats old assistant targets instead of a self-correction")
    corrected.provenance = {
        **corrected.provenance, "self_correction": link,
        "repaired_digest": digest_before, "correction_message_start": prefix + 1,
    }
    return corrected


def tokenizer_identity(evidence: Evidence, directory: Path, sft: Any) -> tuple[Any, dict[str, Any]]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise BuildError("--tokenizer-dir must be an existing LOCAL pinned tokenizer; downloads are forbidden")
    if sft.MODEL_REVISION != "2367e865d009c13ac81713a2878291d33ab28177":
        raise BuildError("the v2 tokenizer byte pins no longer match the trainer's model revision")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    files = []
    for name, sha in TOKENIZER_SHA256.items():
        path = directory / name
        evidence.bound({"path": str(path), "sha256": sha})
        files.append(evidence.capture(path))
    unexpected = {"special_tokens_map.json", "added_tokens.json"} & {p.name for p in directory.iterdir()}
    if unexpected:
        raise BuildError(f"unpinned tokenizer overrides: {sorted(unexpected)}")
    tokenizer = sft.get_tokenizer(str(directory))
    return tokenizer, {"model": sft.MODEL_ID, "model_revision": sft.MODEL_REVISION, "tokenizer_files": files, "chat_template_sha256": digest(tokenizer.chat_template.encode()), "mask_code": evidence.capture(REPO / "tools/modal-mimo-sft/sft.py"), "lossless_code": evidence.capture(REPO / "research/experiments/repaired-data-sft-v1/to_export.py"), "raw_conversion_code": evidence.capture(REPO / "research/experiments/qa-trace/training-arms/build_arms.py"), "builder_code": evidence.capture(Path(__file__))}


def prepare(source: Source, tok: Any, v1: Any, qa: Any, sft: Any, max_length: int, *, target_turn: int | None = None) -> Prepared:
    messages, tools, fidelity = qa.to_messages(source.trajectory)
    if fidelity != "raw_completion":
        raise BuildError(f"source fidelity={fidelity}; parsed actions are not assistant targets")
    loss = "all"
    if target_turn is not None:
        first_corrected = source.provenance.get("correction_message_start")
        if first_corrected is None or target_turn < first_corrected or messages[target_turn].get("role") != "assistant":
            raise BuildError("D target is not a newly generated corrected assistant completion")
        messages = messages[:target_turn + 1]
        loss = "last"
    row = {
        "messages": messages, "tools": tools,
        "agent": (source.trajectory.get("agent") or {}).get("name"),
        "execution_id": source.key[1],
    }
    try:
        flat = v1.flatten(tok, row)
        rendered = sft.render_and_mask(
            tok, flat, max_length=max_length, label=source.key[1], loss=loss,
        )
    except (sft.SftError, ValueError) as exc:
        raise BuildError(str(exc)) from exc
    model = ((source.result.get("agent_info") or {}).get("model_info") or {}).get("name")
    provenance = {
        **source.provenance, "native_pair": list(source.key),
        "execution_id": source.key[1], "task": source.task,
        "parent_ancestry": sorted(source.parents), "source_model": model,
        "source_model_config_sha256": digest(json.dumps(
            (source.result.get("config") or {}).get("agent"), sort_keys=True,
        ).encode()),
        "fidelity": fidelity, "loss": loss,
        "recorded_messages_sha256": digest(json.dumps(messages, sort_keys=True).encode()),
        "recorded_tools_sha256": digest(json.dumps(tools, sort_keys=True).encode()),
        "sequence_tokens": len(rendered.input_ids),
        "assistant_loss_tokens": rendered.trained,
        "input_ids_sha256": digest(json.dumps(rendered.input_ids).encode()),
        "assistant_loss_mask_sha256": digest(bytes(rendered.mask)),
    }
    if target_turn is not None:
        provenance["target_turn_index"] = target_turn
    return Prepared(source, flat, len(rendered.input_ids), rendered.trained, provenance, loss)


def write_arm(out: Path, rows: list[Prepared], fields: dict[str, Any], v1: Any, sft: Any) -> None:
    if all(row.loss == "all" for row in rows):
        v1.write_export(out, [row.messages for row in rows], fields)
    else:
        out.mkdir()
        data = "".join(
            json.dumps({"messages": row.messages, "loss": row.loss}) + "\n"
            for row in rows
        )
        (out / sft.CONVERSATIONS_FILE).write_text(data)
        manifest = {
            "contract": sft.EXPORT_CONTRACT,
            "conversations_sha256": digest(data.encode()), **fields,
        }
        (out / sft.MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "provenance.jsonl").write_text("".join(
        json.dumps(row.provenance, sort_keys=True) + "\n" for row in rows
    ))


def totals(rows: list[Prepared], epochs: int) -> dict[str, Any]:
    return {"rows": len(rows), "tasks": len({r.source.task for r in rows}), "sequence_tokens": sum(r.sequence_tokens for r in rows), "assistant_loss_tokens": sum(r.assistant_loss_tokens for r in rows), "sequence_token_presentations": epochs * sum(r.sequence_tokens for r in rows), "assistant_loss_token_presentations": epochs * sum(r.assistant_loss_tokens for r in rows), "native_pairs": [list(r.source.key) for r in rows]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if type(args.epochs) is not int or args.epochs < 1:
        raise BuildError("--epochs must be a realized positive integer common full-epoch count")
    if type(args.max_length) is not int or not 1 <= args.max_length <= 65536:
        raise BuildError("--max-length must be positive and no larger than the native 65536 context")
    evidence = Evidence(args.inputs.resolve().parent)
    input_ref = evidence.capture(args.inputs.resolve())
    inputs = evidence.json(input_ref)
    if inputs.get("schema") != SCHEMA:
        raise BuildError(f"expected {SCHEMA}")
    heldout = load_heldout(evidence, inputs["heldout"])
    inventory = evidence.json(inputs["inventory"])
    if inventory.get("schema") != "repaired-data-sft-v2.inventory/1":
        raise BuildError("expected hash-bound native inventory/1")
    v1, qa, sft = conversion_modules()
    tokenizer, identity = tokenizer_identity(evidence, args.tokenizer_dir, sft)
    qualification = Qualification(evidence, inputs)
    discovered, source_refusals = [], []
    for observation in inventory["rows"]:
        try:
            discovered.append(native_source(evidence, observation))
        except BuildError as exc:
            source_refusals.append({
                "native_pair": [observation.get("job_id"), observation.get("trial_id")],
                "task": observation.get("task"), "refusal": str(exc),
            })
    sources = deduplicate(discovered)
    source_map = {source.key: source for source in sources}
    excluded = {canonical_task(t) for t in inputs.get("exclusions", [])}
    correction_keys = {tuple(link["corrected"]) for link in inputs.get("corrections", [])}
    pool, b, refusals = [], [], source_refusals
    v6_frame = {
        task for task, row in qualification.v6.items()
        if row.get("status") == "built" and V6_CHAIN <= set(row.get("chain", "").split(">"))
    }
    for source in sources:
        entry: dict[str, Any] = {"native_pair": list(source.key), "task": source.task}
        try:
            original_pass_eligible(source, heldout, excluded, correction_keys, v6_tasks=v6_frame)
            row = prepare(source, tokenizer, v1, qa, sft, args.max_length)
            pool.append(row)
            entry.update(sequence_tokens=row.sequence_tokens, assistant_loss_tokens=row.assistant_loss_tokens)
            if qa.network_fetch(source.trajectory):
                raise BuildError("observed network answer retrieval cannot qualify B")
            row.provenance["clean_regrades"] = qualification.clean(source)
            b.append(row)
        except BuildError as exc:
            entry["refusal"] = str(exc)
            refusals.append(entry)
    receipt: dict[str, Any] = {"schema": "repaired-data-sft-v2.build/1", "inputs": input_ref, "identity": identity, "heldout_parents": sorted(heldout), "additional_training_exclusions": sorted(excluded), "selection_seed": args.seed, "schedule": {"epochs": args.epochs, "loss": "all", "packing": False, "max_steps": None, "max_length": args.max_length, "length_policy": "refuse; never truncate", "requires_complete_epochs": True}, "counts": {"inventory_observations": len(inventory["rows"]), "distinct_native_pairs": len(sources), "eligible_original_pass": len(pool), "verified_clean_nonzero_loss": len(b), "required_b": MINIMUM_B}, "prospective_pool": totals(pool, args.epochs), "prospective_b_not_admitted": totals(b, args.epochs), "refusals": refusals}
    receipt["d"] = {
        "status": "not_yet_admitted" if inputs.get("corrections") else "missing_genuine_verified_corrections",
        "input_links": len(inputs.get("corrections", [])),
    }
    if args.out.exists():
        raise BuildError(f"refusing to overwrite existing output/data: {args.out}")
    args.out.mkdir(parents=True)
    def save() -> None:
        receipt["evidence"] = sorted(evidence.files.values(), key=lambda ref: ref["path"])
        (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if args.plan:
        receipt["status"] = "prospective_only_not_training_admission"
        receipt["candidate_rows"] = [r.provenance for r in pool]
        save()
        return receipt
    try:
        require_minimum(b)
        c = matched_control(pool, b, args.seed)
        d, d_refusals = [], []
        for link in inputs.get("corrections", []):
            try:
                corrected = correction_row(evidence, link, source_map, qualification, heldout | excluded, qa)
                messages, _, _ = qa.to_messages(corrected.trajectory)
                start = corrected.provenance["correction_message_start"]
                targets = [
                    index for index in range(start, len(messages))
                    if messages[index].get("role") == "assistant"
                ]
                # One example per actual corrected completion, each with its full
                # served context and last-only loss; false-pass turns stay context.
                d.extend(
                    prepare(corrected, tokenizer, v1, qa, sft, args.max_length, target_turn=index)
                    for index in targets
                )
            except BuildError as exc:
                d_refusals.append({"source": link.get("source"), "corrected": link.get("corrected"), "refusal": str(exc)})
        target_ids = [(r.source.key, r.provenance["target_turn_index"]) for r in d]
        if len(set(target_ids)) != len(target_ids):
            raise BuildError("duplicate corrected native assistant targets in D")
        receipt["d"] = {"status": "ready" if d and not d_refusals else "missing_genuine_verified_corrections", "refusals": d_refusals, "input_links": len(inputs.get("corrections", []))}
        if d_refusals or (args.require_d and not d):
            raise BuildError("D-v2 missing/refused genuine EnvCheck false-pass -> true-pass self-corrections")
        arms = {"B": b, "C": c}
        if d:
            arms["D"] = d
        receipt["arms"] = {arm: totals(rows, args.epochs) for arm, rows in arms.items()}
        exposure = receipt["arms"]["B"]["assistant_loss_token_presentations"]
        if exposure != receipt["arms"]["C"]["assistant_loss_token_presentations"]:
            raise BuildError("B/C realized schedule exposure mismatch")
        receipt["exposure_match"] = {"exact": True, "assistant_loss_token_presentations": exposure, "sampler": "seeded_permutation_feasible_subset_not_uniform", "same_original_pass_pool": True, "c_is_not_b": True}
        for arm, rows in arms.items():
            manifest = {"builder": str(Path(__file__).relative_to(REPO)), "arm": arm, "rows": len(rows), **totals(rows, args.epochs), "execution_ids": [r.source.key[1] for r in rows], "tasks": sorted({r.source.task for r in rows}), "identity": identity, "schedule": receipt["schedule"], "heldout_applied": True, "heldout_parents": sorted(heldout), "selection_seed": args.seed, "exposure_match": receipt["exposure_match"] if arm in {"B", "C"} else None}
            if arm == "D":
                manifest["schedule"] = {**receipt["schedule"], "loss": "last"}
                manifest["target_policy"] = "corrected_assistant_completions_only_with_complete_prompt_prefix"
            write_arm(args.out / arm, rows, manifest, v1, sft)
        receipt["status"] = "ready"
    except BuildError as exc:
        receipt["status"], receipt["error"] = "refused", str(exc)
        save()
        raise
    save()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--epochs", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--max-length", type=int, default=65536)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--require-d", action="store_true")
    args = parser.parse_args()
    try:
        receipt = run(args)
    except (BuildError, OSError, KeyError, ValueError) as exc:
        print(f"repaired-data-sft-v2: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": receipt["status"], "counts": receipt["counts"], "receipt": str(args.out / "receipt.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
