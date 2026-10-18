#!/usr/bin/env python3
"""Prepare EVALUATION-ONLY reset-context inputs; never execute or export training.

Usage (a new, owned output version is required)::

    python research/experiments/test-repair-performance-20261017/evaluation_corrections.py \
        --partition runs/envcheck-20261017/m2/partition-v1/partition.jsonl \
        --out runs/envcheck-20261017/m3/evaluation-corrections-v1

Only the fixed accepted26 plan's B a1 UUIDs are eligible. Both observed
original1/repaired0 and original0/repaired0 qualify, independently of partition
labels. Missing/infra outcomes do not. Repaired passes are recorded separately as
unexecuted pass-throughs. Empty requests are a prerequisite report, not delivery
of corrections. A caller must separately authorize the native runtime/budget,
execute at most one correction per a1, and obtain its actual repaired replay.
Attach request.provenance.correction to that execution's canonical partition row
for analyze.py; requests themselves are not executed-correction observations.

The native task is an exact snapshot of the ORIGINAL package (the analysis pins
that source digest), not the repaired grader. Only the original prompt plus the
existing extra_instruction_path/sha256 fields carry model input. The admitted
repaired package is separately snapshotted and bound for subsequent evaluation.

Some native separate-verifier trials have no agent.diff but retain agent.kept.diff
and agent.full.diff. These are accepted only with their original collect-phase
workspace.tgz, byte-bound inventory source_digest, frozen package/capture script,
matching native receipts and exact patch-to-snapshot hunk checks. No diff is
manufactured, no post-hidden-test workspace is read, and unknown capture formats
remain explicit source-state prerequisites. study_plan.py and its frozen hash do
not change. analyze.py checks the same alternative custody instead of weakening
its source-patch identity check. correction_data.request_for is NEVER called.
"""

from __future__ import annotations

import argparse
import re
import shutil
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import correction_data as c
import partition as p
from study_plan import MODEL, file_ref, native_source

FIXED_PLAN = p.REPO / "runs/envcheck-20261017/m3/plan-v1.json"
FIXED_PLAN_SHA256 = "sha256:3a1ad43491e1a562a576fc4103d8ae02d8820d757ac5ee36ab32b92dc4e6afa2"
OWNED_ROOT = p.REPO / "runs/envcheck-20261017/m3"
PATCH_CUSTODY = "envcheck.separate-verifier-pre-hidden-patch/v1"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n?$")


def checked_ref(ref: dict[str, Any]) -> None:
    if p.file_ref(Path(ref["path"])) != {key: ref[key] for key in ("path", "sha256")}:
        raise ValueError(f"retained input bytes changed: {ref['path']}")


def bound_partition(binding: dict[str, Any]) -> dict[str, Any]:
    checked_ref(binding)
    rows = p.read_jsonl(Path(binding["path"]))
    index = binding["row_index"]
    if type(index) is not int or index < 0 or index >= len(rows):
        raise ValueError("partition row selector is outside its immutable input")
    row = rows[index]
    if p.sha256(p.canonical_bytes(row)) != binding["row_sha256"]:
        raise ValueError("partition row differs from its bound input bytes")
    return row


def checked_plan() -> tuple[dict[str, Any], dict[str, Any], set[str], dict[str, Any]]:
    # This is the fixed schedule, not a newly selected subset or substituted plan.
    plan_ref = file_ref(FIXED_PLAN, FIXED_PLAN_SHA256)
    plan = p.read_json(FIXED_PLAN)
    from analyze import Evidence, validate_plan

    validate_plan(plan, Evidence())
    heldout, heldout_ref = p.frozen_heldout(p.DEFAULT_HELDOUT)
    if Path(plan["inputs"]["heldout"]["path"]).resolve() != p.DEFAULT_HELDOUT.resolve():
        raise ValueError("plan does not use the canonical frozen heldout authority")
    if plan["inputs"]["heldout"]["sha256"] != heldout_ref["sha256"]:
        raise ValueError("plan's all33 authority differs from the frozen manifest")
    population = plan["population"]
    if (set(population["frozen_heldout_parent_ids"]) != heldout
            or set(population["training_exclusion_parent_ids"]) != heldout):
        raise ValueError("the full all33 training exclusion lineage changed")
    authority = p.read_json(p.DEFAULT_HELDOUT)
    if authority.get("freeze_ready") is not True or authority.get("scoring_allowed") is not True:
        raise ValueError("frozen authority does not permit heldout evaluation")
    checkpoint = plan["model_checkpoint"]
    checked_ref(checkpoint["training_receipt"])
    training = p.read_json(Path(checkpoint["training_receipt"]["path"]))
    if (plan["model"] != MODEL or checkpoint["adapter"] != "rds1-b-v4e4/adapter"
            or checkpoint["base_revision"] != training["model_revision"]
            or checkpoint["adapter"] != training["remote"]["adapter"]
            or checkpoint["adapter_model_sha256"] != p.digest(training["remote"]["adapter_model_sha256"])
            or checkpoint["training_receipt"] != plan["inputs"]["training_receipt_B"]):
        raise ValueError("B selector/checkpoint differs from its actual retained training receipt")
    return plan, plan_ref, heldout, heldout_ref


def original_package(base: dict[str, Any]) -> Path:
    ref = base["sources"]["experiment_spec"]
    checked_ref(ref)
    spec = p.read_json(Path(ref["path"]))
    task_path = Path(spec.get("task_path") or spec["task"])
    if task_path.is_absolute() or ".." in task_path.parts:
        raise ValueError("original native spec lacks a repo-relative package path")
    package = (Path(ref["path"]).parent.parent.parent / task_path).resolve()
    from evallab.task_variants import task_directory_digest

    if task_directory_digest(package) != base["task_digest"]:
        raise ValueError("original package is missing or differs from the plan's source digest")
    return package


def checked_native(row: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    if native_source(Path(base["source_trial"])) != base:
        raise ValueError("planned native identity/settings/source hashes changed")
    source = c.checked_source(row)
    if (source["execution_id"] != base["execution_id"] or source["task"] != base["task"]
            or source["model"] != MODEL or source["original_reward"] not in (0.0, 1.0)
            or source["original_reward"] != base["native_reward"]):
        raise ValueError("partition source is not this plan task's original B a1 execution")
    for field, native_field in (("result", "result"), ("native", "native_trajectory"), ("fingerprint", "fingerprint")):
        if source["alias"]["files"].get(field, {}).get("sha256") != base["sources"][native_field]["sha256"]:
            raise ValueError(f"inventory alias does not match the planned native {field} bytes")
    flags = row.get("flags") or {}
    if flags.get("alias_conflicts") or flags.get("teacher") or flags.get("copied_fix") or flags.get("attempt_conflict"):
        raise ValueError("conflicting/teacher/copied/non-original source is not eligible")
    result = p.read_json(Path(base["sources"]["result"]["path"]))
    config = result["config"]
    if (config.get("source_trial") or config.get("extra_instruction_paths") or config.get("extra_instructions")
            or config["agent"].get("resume_trajectory") or config["agent"].get("load_trajectory")):
        raise ValueError("source a1 contains restored history or additional historical instruction")
    messages, _ = p.native_messages(Path(base["sources"]["native_trajectory"]["path"]))
    first = next((index for index, message in enumerate(messages) if message.get("role") == "user"), None)
    if first is None or any(message.get("role") != "system" for message in messages[:first]):
        raise ValueError("source native conversation is not a fresh original task input")
    return source


def checked_variant(task: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    pin = row["variant"]
    checked_ref(pin["manifest"])
    current = [item for item in p.variants(Path(pin["manifest"]["path"]))
               if item["task"] == task["task"] and item["repaired_digest"] == task["repaired_digest"]]
    if len(current) != 1 or current[0] != pin:
        raise ValueError("partition's admitted package/control custody changed")
    if not p.mechanical_grader_admitted(pin) or not pin["measurement_eligible"]:
        raise ValueError("repaired package lacks actual mechanical admission/control custody")
    repair = task["repair"]
    checked_ref(repair["qualification_source"])
    document = p.read_json(Path(repair["qualification_source"]["path"]))
    rows = document if isinstance(document, list) else document.get("variants", document.get("tasks", [document]))
    qualification = repair["qualification_row"]
    if qualification not in rows or p.sha256(p.canonical_bytes(qualification)) != repair["qualification_row_sha256"]:
        raise ValueError("plan's repair qualification row differs from its canonical manifest")
    if any(pin["manifest_row"].get(key) != value for key, value in qualification.items()):
        raise ValueError("partition manifest is not the exact planned repair qualification")
    if (pin["task"] != task["task"] or pin["repaired_digest"] != task["repaired_digest"]
            or Path(pin["package_path"]).resolve() != Path(repair["package_path"]).resolve()
            or p.digest(pin["manifest_row"].get("v6_parent_digest_of_variant")) != task["source_task_digest"]
            or p.digest(repair.get("parent_digest")) != task["source_task_digest"]):
        raise ValueError("repaired package or frozen source-parent lineage differs from the plan")
    return pin


def checked_baseline(task: dict[str, Any], row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate = task["candidates"][0]
    base = candidate["native"]
    if (candidate["attempt"] != 1 or not candidate["reusable"] or candidate["reuse_issues"]
            or not base or row["execution_id"] != base["execution_id"]
            or candidate["original_reward"] not in (0, 1)
            or row.get("attempt") not in (None, 1) or row.get("heldout") is not True
            or row.get("task") != task["task"] or row.get("model") != MODEL
            or row.get("repaired_digest") != task["repaired_digest"]
            or row.get("original_reward") != candidate["original_reward"]):
        raise ValueError("requires this accepted plan task's reusable, actually scored B a1 UUID")
    source = checked_native(row, base)
    pin = checked_variant(task, row)
    if row.get("status") != "measured" or row.get("repaired_reward") not in (0.0, 1.0):
        raise ValueError("actual measured native repaired baseline required; missing/infra is not zero")
    entries = []
    for supplied in row["provenance"].get("receipts", []):
        c.checked_evidence(supplied)
        matching = [entry for entry in p.receipt_rows([Path(supplied["path"])])
                    if entry["selector"] == supplied["selector"]]
        if len(matching) != 1 or matching[0] != {key: value for key, value in supplied.items() if key not in ("valid", "problems")}:
            raise ValueError("partition receipt's native output custody differs from canonical retained artifacts")
        entries.append(matching[0])
    joined = p.join_one(row, pin, entries)
    if joined["status"] != "measured" or joined["repaired_reward"] != row["repaired_reward"]:
        raise ValueError("baseline does not revalidate against its untouched canonical receipts")
    # Also require precisely the receipt contract used by the M3 analyzer.
    from analyze import Evidence, joined_grade

    value, issues = joined_grade(joined, base, task, Evidence())
    if value != row["repaired_reward"] or issues:
        raise ValueError(f"baseline does not meet M3 measured-grade custody: {issues}")
    return source, pin, joined


def patch_blocks(text: str) -> dict[str, str]:
    starts = list(re.finditer(r"^diff --git a/(.+) b/(.+)\n", text, re.MULTILINE))
    if not starts or starts[0].start() != 0:
        raise ValueError("retained patch is not a nonempty supported native Git diff")
    blocks = {}
    for index, match in enumerate(starts):
        path = match[2]
        pure = PurePosixPath(path)
        if match[1] != path or pure.is_absolute() or ".." in pure.parts or '"' in path or path in blocks:
            raise ValueError("ambiguous/renamed/unsafe retained patch path; pre-hidden textual capture required")
        blocks[path] = text[match.start():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
    return blocks


def check_snapshot_hunks(full: str, kept: str, snapshot: Path, hidden_patch: Path) -> None:
    """Check retained diffs against pre-hidden archive bytes WITHOUT deriving a patch."""
    full_blocks, kept_blocks = patch_blocks(full), patch_blocks(kept)
    if any(full_blocks.get(path) != block for path, block in kept_blocks.items()):
        raise ValueError("retained kept patch is not an exact block subset of the original full patch")
    hidden_paths = set(re.findall(r"^diff --git a/.+ b/(.+)$", hidden_patch.read_text(), re.MULTILINE))
    if set(kept_blocks) & hidden_paths:
        raise ValueError("retained correction patch contains hidden-test paths")
    with tarfile.open(snapshot, "r:gz") as archive:
        members = {}
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("original pre-hidden snapshot has unsafe/linked/non-file members")
            if member.isfile():
                name = path.as_posix()
                if name in members:
                    raise ValueError("ambiguous duplicate file in original pre-hidden snapshot")
                members[name] = member
        for path, block in full_blocks.items():
            deleted = "\n+++ /dev/null\n" in block
            if deleted:
                if path in members:
                    raise ValueError("deleted source patch path is present in the pre-hidden snapshot")
                target = []
            else:
                if f"\n+++ b/{path}\n" not in block or path not in members:
                    raise ValueError("source diff's new-side file is absent from the original snapshot")
                stream = archive.extractfile(members[path])
                if stream is None:
                    raise ValueError("original snapshot member is unreadable")
                target = stream.read().decode("utf-8").splitlines(keepends=True)
            lines = block.splitlines(keepends=True)
            index, hunks = 0, 0
            while index < len(lines):
                match = HUNK.fullmatch(lines[index])
                if not match:
                    index += 1
                    continue
                hunks += 1
                new_start = int(match[3])
                old_count, new_count = int(match[2] or 1), int(match[4] or 1)
                old_seen, new_lines = 0, []
                previous = ""
                index += 1
                while index < len(lines) and not lines[index].startswith("@@ "):
                    line = lines[index]
                    if line.startswith("\\ No newline at end of file"):
                        if previous in (" ", "+") and new_lines:
                            new_lines[-1] = new_lines[-1].removesuffix("\n")
                    elif line[:1] in (" ", "+", "-"):
                        previous = line[0]
                        old_seen += previous in (" ", "-")
                        if previous in (" ", "+"):
                            new_lines.append(line[1:])
                    else:
                        raise ValueError("unknown retained patch hunk format; no reconstructed fallback")
                    index += 1
                if old_seen != old_count or len(new_lines) != new_count:
                    raise ValueError("retained source patch hunk counts are inconsistent")
                start = max(new_start - 1, 0)
                if target[start:start + new_count] != new_lines:
                    raise ValueError("retained source patch is not the actual pre-hidden snapshot's new-side bytes")
            if not hunks:
                raise ValueError("binary/mode-only patch needs an independently validated pre-hidden textual capture")


def checked_retained_patch(
    source: dict[str, Any], package: Path, aliases: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove an actual collect-phase capture, independently of any study plan."""
    from evallab.task_variants import task_directory_digest

    trial = Path(source["source_trial"])
    current = p.source_record(trial)
    retained_aliases = aliases if aliases is not None else [source["alias"]]
    retained = next((alias for alias in retained_aliases if Path(alias["trial"]).resolve() == trial.resolve()), None)
    if (current != source or retained is None
            or retained["source_digest"] != current["alias"]["source_digest"]):
        raise ValueError("native capture is not bound by its exact retained source alias")
    package = package.resolve()
    package_digest = task_directory_digest(package)
    config_path, wrapper_path = package / "task.toml", package / "tests/test.sh"
    config = tomllib.loads(config_path.read_text())
    collect = (config.get("verifier") or {}).get("collect") or []
    commands = [hook.get("command", "") for hook in collect if hook.get("service") == "main"]
    if ((config.get("verifier") or {}).get("environment_mode") != "separate"
            or "/var/tmp/mimo-separate" not in config.get("artifacts", [])
            or not any('tar --null --exclude=.git -czf "$SNAP/workspace.tgz" -C "$CWD" --files-from="$LIST"' in command
                       and 'ls-files -c -o --exclude-standard -z' in command for command in commands)):
        raise ValueError("frozen source package lacks the recognized pre-verifier collect-phase snapshot contract")
    wrapper = wrapper_path.read_text()
    ordered = [
        'git read-tree "$PRISTINE" || { echo "baseline reset failed"',
        'tar -xzf "$SNAP/workspace.tgz" -C /tmp/agentcopy',
        'git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy diff --cached --binary "$PRISTINE" > "$V/agent.full.diff"',
        'git read-tree "$PRISTINE" && git add -A . && git diff --cached "$PRISTINE" > "$V/agent.kept.diff"',
        '# 4. Hidden tests',
        'git apply --verbose /tests/test.patch > "$V/apply.log"',
    ]
    positions = [wrapper.find(fragment) for fragment in ordered]
    if -1 in positions or positions != sorted(set(positions)):
        raise ValueError("frozen native wrapper does not prove pre-hidden snapshot/full/kept capture ordering")
    manifest_path = trial / "artifacts/manifest.json"
    manifests = [entry for entry in p.read_json(manifest_path)
                 if entry.get("source") == "/var/tmp/mimo-separate"]
    if len(manifests) != 1 or any(manifests[0].get(key) != value for key, value in {
        "status": "ok", "type": "directory", "destination": "artifacts/var/tmp/mimo-separate",
    }.items()):
        raise ValueError("original native artifact manifest does not retain the collect-phase snapshot")
    snapshot = trial / "artifacts/var/tmp/mimo-separate/workspace.tgz"
    full, kept = trial / "verifier/agent.full.diff", trial / "verifier/agent.kept.diff"
    if not all(path.is_file() and path.stat().st_size for path in (snapshot, full, kept)):
        raise ValueError("source-state prerequisite: retained pre-hidden workspace.tgz + agent.full.diff + agent.kept.diff; never extract from a post-hidden workspace")
    # Snapshot bytes are covered by source_digest, not merely a path in manifest.json.
    # Each retained alias must agree; this proof never writes into an alias.
    for alias in retained_aliases:
        other = Path(alias["trial"])
        actual = p.source_record(other)
        if actual["alias"]["source_digest"] != alias["source_digest"] or alias["source_digest"] != retained["source_digest"]:
            raise ValueError("source snapshot changed or conflicts across retained aliases")
        for relative in ("verifier/agent.full.diff", "verifier/agent.kept.diff", "artifacts/var/tmp/mimo-separate/workspace.tgz"):
            if p.file_ref(other / relative)["sha256"] != p.file_ref(trial / relative)["sha256"]:
                raise ValueError("pre-hidden patch/snapshot bytes conflict across original native aliases")
    check_snapshot_hunks(full.read_text(), kept.read_text(), snapshot, package / "tests/test.patch")
    patch_ref = p.file_ref(kept)
    custody = {
        "schema": PATCH_CUSTODY,
        "source_execution_id": source["execution_id"], "source_digest": retained["source_digest"],
        "source_result": p.file_ref(trial / "result.json"),
        "native_trajectory": current["alias"]["files"]["native"],
        "source_package": {"path": str(package), "sha256": package_digest},
        "capture_config": p.file_ref(config_path), "capture_wrapper": p.file_ref(wrapper_path),
        "artifact_manifest": p.file_ref(manifest_path), "pre_hidden_snapshot": p.file_ref(snapshot),
        "full_patch": p.file_ref(full), "source_patch": patch_ref,
        "capture_phase": "separate-verifier-collect-before-hidden-tests",
        "patch_role": "native-retained-agent.kept.diff",
    }
    return patch_ref, custody


def source_patch_for(
    row: dict[str, Any], base: dict[str, Any], binding: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source = checked_native(row, base)
    pinned = base["sources"].get("patch")
    if pinned:
        checked_ref(pinned)
        if source["alias"]["files"].get("patch", {}).get("sha256") != pinned["sha256"]:
            raise ValueError("inventory source patch differs from the fixed plan's actual agent.diff")
        if not Path(pinned["path"]).read_text().strip():
            raise ValueError("empty source patch cannot be an incomplete fix")
        return p.file_ref(Path(pinned["path"])), None
    if bound_partition(binding) != row:
        raise ValueError("alternative source custody lacks an exact canonical partition input")
    trial = Path(base["source_trial"])
    retained = next((alias for alias in row["provenance"]["aliases"] if Path(alias["trial"]).resolve() == trial.resolve()), None)
    current = p.source_record(trial)
    if retained is None or retained["source_digest"] != current["alias"]["source_digest"]:
        raise ValueError("plan's original snapshot is not bound by the retained inventory alias digest")
    patch_ref, proof = checked_retained_patch(current, original_package(base), row["provenance"]["aliases"])
    return patch_ref, {**proof, "plan": p.file_ref(FIXED_PLAN), "partition_input": binding}


def validate_source_patch(meta: dict[str, Any], base: dict[str, Any]) -> None:
    """Analysis adapter: an alternate source is checked, never exempted from SHA custody."""
    if meta.get("purpose") != "evaluation" or meta.get("heldout") is not True or meta.get("training_allowed") is not False:
        raise ValueError("alternate heldout source patch is evaluation-only")
    custody = meta["source_patch_custody"]
    checked_ref(custody["plan"])
    if custody["plan"] != p.file_ref(FIXED_PLAN) or custody["plan"]["sha256"] != FIXED_PLAN_SHA256:
        raise ValueError("alternate source patch belongs to a different fixed plan")
    planned = [task["candidates"][0]["native"] for task in p.read_json(FIXED_PLAN)["tasks"]
               if task["candidates"][0]["native"] and task["candidates"][0]["native"]["execution_id"] == base["execution_id"]]
    if planned != [base]:
        raise ValueError("alternate source patch is not the frozen plan's exact original a1 state")
    row = bound_partition(custody["partition_input"])
    patch_ref, actual = source_patch_for(row, base, custody["partition_input"])
    if actual != custody or meta["source_patch"] != patch_ref:
        raise ValueError("correction patch differs from its checked native pre-hidden source custody")


def owned_output(path: Path) -> Path:
    out = path.expanduser().resolve()
    if out.parent != OWNED_ROOT.resolve() or re.fullmatch(r"evaluation-corrections-v[1-9][0-9]*", out.name) is None:
        raise ValueError("output must be a new runs/envcheck-20261017/m3/evaluation-corrections-vN directory")
    return out


def package_snapshot(package: Path, expected: str, out: Path) -> dict[str, Any]:
    from evallab.task_variants import task_directory_digest

    if task_directory_digest(package) != expected or any(path.is_symlink() for path in package.rglob("*")):
        raise ValueError("package snapshot refuses changed packages or symlinks")
    snapshot = out / "packages" / expected.removeprefix("sha256:")
    if not snapshot.exists():
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copytree(package, snapshot)
    if task_directory_digest(snapshot) != expected or task_directory_digest(package) != expected:
        raise ValueError("package changed while copying its immutable owned snapshot")
    return {"path": str(snapshot), "sha256": expected}


def native_fields(base: dict[str, Any], snapshot: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    spec = p.read_json(Path(base["sources"]["experiment_spec"]["path"]))
    fields = {key: spec[key] for key in (
        "reference_profile", "deviations", "environment", "egress_lock", "timeout_seconds",
        "max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens", "cost_limit_usd",
    )}
    fields.update({
        "task": base["task"], "task_path": Path(snapshot["path"]).relative_to(p.REPO).as_posix(),
        "task_package_digest": base["task_digest"], "agent": "mimoagent", "model": MODEL,
        "attempts": 1, "concurrency": 1, "diff_sources": [],
        "extra_instruction_path": Path(extra["path"]).relative_to(p.REPO).as_posix(),
        "extra_instruction_sha256": extra["sha256"],
    })
    return fields


def request_for(
    row: dict[str, Any], *, plan: dict[str, Any], plan_ref: dict[str, Any], heldout: set[str],
    heldout_ref: dict[str, Any], partition_input: dict[str, Any], out: Path,
) -> dict[str, Any]:
    out = owned_output(out)
    task_id = p.parent_task(row["task"])
    # Evaluation is a distinct path. Never pass a substituted exclusion set to training.
    actual_plan, actual_plan_ref, actual_heldout, actual_heldout_ref = checked_plan()
    if (heldout != actual_heldout or heldout_ref != actual_heldout_ref
            or task_id not in actual_heldout or plan_ref != actual_plan_ref or plan != actual_plan):
        raise ValueError("evaluation requests require the actual all33 frozen authority and fixed plan")
    if bound_partition(partition_input) != row:
        raise ValueError("request partition input differs from its retained bytes")
    tasks = [task for task in plan["tasks"] if task["task"] == task_id]
    if len(tasks) != 1 or tasks[0]["training_allowed"] is not False or tasks[0]["heldout"] is not True:
        raise ValueError("not an evaluation-only task in the accepted26 schedule")
    task = tasks[0]
    source, pin, joined = checked_baseline(task, row)
    if joined["repaired_reward"] != 0.0:
        raise ValueError("baseline repaired pass needs no correction; pass-through is not execution")
    base = task["candidates"][0]["native"]
    package = original_package(base)
    prompt, prompt_ref = c.actual_task_prompt(source, package)
    if (package / "instruction.md").read_bytes() != (Path(pin["package_path"]) / "instruction.md").read_bytes():
        raise ValueError("repair changed the original task prompt")
    patch_ref, custody = source_patch_for(row, base, partition_input)
    patch = Path(patch_ref["path"]).read_bytes().decode("utf-8")
    trial = c.feedback_trial(joined)
    stdout = trial / "verifier/test_output.log"
    if not stdout.is_file():
        raise ValueError("source-state prerequisite: actual repaired-failing native test_output.log")
    safe, policy = c.safe_feedback(stdout.read_bytes().decode("utf-8"))
    extra = c.render_extra_instruction(patch, safe)
    ident = p.sha256(p.canonical_bytes({"purpose": "evaluation", "plan": plan_ref["sha256"],
                    "source_execution_id": base["execution_id"], "repaired_digest": pin["repaired_digest"],
                    "extra_instruction_sha256": p.sha256(extra.encode())})).removeprefix("sha256:")
    original_snapshot = package_snapshot(package, task["source_task_digest"], out)
    repaired_snapshot = package_snapshot(Path(pin["package_path"]), pin["repaired_digest"], out)
    directory = out / ident
    directory.mkdir()
    for name, data in (("task-prompt.txt", prompt), ("source.patch", patch), ("feedback.txt", safe), ("extra-instruction.txt", extra)):
        (directory / name).write_bytes(data.encode("utf-8"))
    correction = {
        "source_execution_id": base["execution_id"], "protocol": c.PROTOCOL,
        "purpose": "evaluation", "heldout": True, "training_allowed": False,
        "partition_scope": "mechanical_repaired_grader", "plan": plan_ref,
        "partition_input": partition_input, "source_patch": patch_ref,
        "feedback": p.file_ref(stdout), "feedback_result": p.file_ref(trial / "result.json"),
        "safe_feedback": p.file_ref(directory / "feedback.txt"), "task_prompt": prompt_ref,
        "extra_instruction": p.file_ref(directory / "extra-instruction.txt"),
        "task_package_snapshot": original_snapshot, "repaired_task_package_snapshot": repaired_snapshot,
        "allowed_model_inputs": ["task_prompt", "source_patch", "repaired_failure_stdout"],
        "hidden_test_source_included": False, "reference_source_included": False,
        "full_historical_context_included": False,
    }
    if custody is not None:
        correction["source_patch_custody"] = custody
    fields = native_fields(base, original_snapshot, correction["extra_instruction"])
    request = {
        "schema": "envcheck.correction-request/v1", "request_id": ident,
        "purpose": "evaluation", "heldout": True, "training_allowed": False,
        "protocol": c.PROTOCOL, "task": task_id, "model": MODEL,
        "source_execution_id": base["execution_id"], "heldout_manifest": heldout_ref,
        "plan": plan_ref, "model_checkpoint": plan["model_checkpoint"],
        "required_native_settings": base["settings"], "required_native_settings_digest": base["settings_digest"],
        "source_partition": row, "partition_input": partition_input,
        "partition_scope": "mechanical_repaired_grader", "feedback_policy": policy,
        "task_prompt": p.file_ref(directory / "task-prompt.txt"), "patch_input": p.file_ref(directory / "source.patch"),
        "native_spec_fields": fields, "provenance": {"correction": correction},
        "execution": {"status": "prepared_not_executed", "model_calls": 0, "sandbox_calls": 0,
                      "runtime_authorized": False, "budget_authorized": False},
        "runtime_prerequisites": ["separate parent authorization of native runtime and budget",
                                  "exact B checkpoint and required_native_settings; no history restore",
                                  "at most one real correction generation for this a1 UUID",
                                  "actual corrected native output plus canonical exact-repaired-package replay"],
    }
    p.write_json(directory / "request.json", request)
    return {**request, "request": p.file_ref(directory / "request.json")}


def checked_request(request: dict[str, Any]) -> None:
    """Recheck prepared input custody before a separately authorized native run."""
    if (request.get("purpose") != "evaluation" or request.get("heldout") is not True
            or request.get("training_allowed") is not False or request.get("protocol") != c.PROTOCOL):
        raise ValueError("not an evaluation-only reset-context request")
    out = owned_output(Path(request["request"]["path"]).parent.parent)
    if Path(request["request"]["path"]).name != "request.json":
        raise ValueError("evaluation request receipt is outside its immutable owned request directory")
    checked_ref(request["request"])
    if p.read_json(Path(request["request"]["path"])) != {key: value for key, value in request.items() if key != "request"}:
        raise ValueError("request JSONL differs from its immutable request receipt")
    plan, plan_ref, heldout, heldout_ref = checked_plan()
    if request["plan"] != plan_ref or request["heldout_manifest"] != heldout_ref or request["task"] not in heldout:
        raise ValueError("request changed fixed plan/all33 authority")
    task = next(task for task in plan["tasks"] if task["task"] == request["task"])
    row = bound_partition(request["partition_input"])
    if row != request["source_partition"]:
        raise ValueError("request source partition is not its retained canonical row")
    source, pin, joined = checked_baseline(task, row)
    if joined["repaired_reward"] != 0.0:
        raise ValueError("request baseline is not an observed repaired failure")
    base = task["candidates"][0]["native"]
    patch_ref, custody = source_patch_for(row, base, request["partition_input"])
    meta = request["provenance"]["correction"]
    if (meta.get("purpose") != "evaluation" or meta.get("heldout") is not True
            or meta.get("training_allowed") is not False or meta.get("protocol") != c.PROTOCOL
            or meta.get("hidden_test_source_included") is not False or meta.get("reference_source_included") is not False
            or meta.get("full_historical_context_included") is not False
            or meta.get("plan") != plan_ref or meta.get("partition_input") != request["partition_input"]
            or meta.get("allowed_model_inputs") != ["task_prompt", "source_patch", "repaired_failure_stdout"]):
        raise ValueError("request metadata lost evaluation isolation or bounded reset-context inputs")
    if (meta["source_patch"] != patch_ref or meta.get("source_patch_custody") != custody
            or meta["source_execution_id"] != base["execution_id"] or request["source_execution_id"] != base["execution_id"]
            or request["model"] != MODEL or request["model_checkpoint"] != plan["model_checkpoint"]
            or request["required_native_settings"] != base["settings"]
            or request["required_native_settings_digest"] != base["settings_digest"]):
        raise ValueError("request source patch/model/settings custody changed")
    for ref in (request["task_prompt"], request["patch_input"], meta["task_prompt"], meta["source_patch"],
                meta["feedback"], meta["feedback_result"], meta["safe_feedback"], meta["extra_instruction"]):
        checked_ref(ref)
    package = original_package(base)
    prompt, prompt_ref = c.actual_task_prompt(source, package)
    if meta["task_prompt"] != prompt_ref or (package / "instruction.md").read_bytes() != (Path(pin["package_path"]) / "instruction.md").read_bytes():
        raise ValueError("request prompt is not the exact unchanged original task input")
    feedback_trial = c.feedback_trial(joined)
    if (Path(meta["feedback_result"]["path"]) != feedback_trial / "result.json"
            or Path(meta["feedback"]["path"]) != feedback_trial / "verifier/test_output.log"):
        raise ValueError("request feedback is not from its actual native repaired-failure replay")
    safe, policy = c.safe_feedback(Path(meta["feedback"]["path"]).read_bytes().decode("utf-8"))
    patch = Path(patch_ref["path"]).read_bytes().decode("utf-8")
    if (request["feedback_policy"] != policy or Path(request["task_prompt"]["path"]).read_bytes().decode("utf-8") != prompt
            or request["patch_input"]["sha256"] != patch_ref["sha256"]
            or Path(meta["safe_feedback"]["path"]).read_bytes().decode("utf-8") != safe
            or Path(meta["extra_instruction"]["path"]).read_bytes().decode("utf-8") != c.render_extra_instruction(patch, safe)):
        raise ValueError("request contains input beyond the exact prompt/patch/source-free diagnostic")
    from evallab.task_variants import task_directory_digest

    for field, expected in (("task_package_snapshot", task["source_task_digest"]), ("repaired_task_package_snapshot", pin["repaired_digest"])):
        snapshot = meta[field]
        if (snapshot != {"path": str(out / "packages" / expected.removeprefix("sha256:")), "sha256": expected}
                or task_directory_digest(Path(snapshot["path"])) != expected):
            raise ValueError("request's exact original/repaired package snapshot changed")
    fields = request["native_spec_fields"]
    if fields != native_fields(base, meta["task_package_snapshot"], meta["extra_instruction"]):
        raise ValueError("native fields do not implement the bound fresh single-correction protocol")


def prepare(args: argparse.Namespace) -> int:
    plan, plan_ref, heldout, heldout_ref = checked_plan()
    out = owned_output(args.out)
    by_execution: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    inputs = []
    for path in args.partition:
        ref = p.file_ref(path)
        inputs.append(ref)
        for index, row in enumerate(p.read_jsonl(path)):
            binding = {**ref, "row_index": index, "row_sha256": p.sha256(p.canonical_bytes(row))}
            by_execution.setdefault(row["execution_id"], []).append((row, binding))
    out.mkdir(parents=True, exist_ok=False)
    requests, prerequisites, pass_through = [], [], []
    for task in plan["tasks"]:
        base = task["candidates"][0]["native"]
        source_id = base["execution_id"] if base else None
        rows = [(row, binding) for row, binding in by_execution.get(source_id, []) if row.get("repaired_digest") == task["repaired_digest"]]
        unique = {p.sha256(p.canonical_bytes(row)): (row, binding) for row, binding in rows}
        try:
            if len(unique) != 1:
                raise ValueError("missing or ambiguous canonical partition row for this exact a1/repaired-package pair")
            row, binding = next(iter(unique.values()))
            _, _, joined = checked_baseline(task, row)
            if joined["repaired_reward"] == 1.0:
                pass_through.append({"task": task["task"], "source_execution_id": source_id,
                                     "partition_input": binding, "mode": "baseline_pass_no_correction_needed",
                                     "executed_correction": False})
                continue
            requests.append(request_for(row, plan=plan, plan_ref=plan_ref, heldout=heldout, heldout_ref=heldout_ref,
                                        partition_input=binding, out=out))
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
            prerequisites.append({"task": task["task"], "source_execution_id": source_id,
                                  "repaired_digest": task["repaired_digest"], "reason": f"{type(exc).__name__}: {exc}"})
    p.write_jsonl(out / "requests.jsonl", requests)
    p.write_jsonl(out / "prerequisites.jsonl", prerequisites)
    p.write_jsonl(out / "pass-through.jsonl", pass_through)
    summary = {
        "schema": "envcheck.evaluation-corrections/v1", "purpose": "evaluation", "heldout": True, "training_allowed": False,
        "plan": plan_ref, "heldout_manifest": heldout_ref, "partition_inputs": inputs,
        "planned_tasks": 26, "requests_prepared_not_executed": len(requests),
        "baseline_passes_no_correction": len(pass_through), "tasks_with_missing_prerequisites": len(prerequisites),
        "status": "inputs_prepared_not_executed" if requests else "no_correction_inputs_available",
        "genuine_correction_executions": 0, "model_calls": 0, "sandbox_calls": 0, "training_exports": 0,
        "runtime_authorized": False, "budget_authorized": False,
        "source_state_prerequisites": ["actually measured failing native repaired baseline for the exact plan a1 UUID",
                                       "actual retained pre-hidden source patch and original prompt with checked custody",
                                       "actual native repaired failing stdout with a recognized source-free diagnostic"],
        "execution_prerequisites": ["separate parent native-runtime/budget authorization", "real same-model correction and repaired replay"],
    }
    p.write_json(out / "summary.json", summary)
    print(p.canonical_bytes(summary).decode())
    return 0 if requests else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--partition", type=Path, action="append", required=True, help="canonical partition.py measured join JSONL; repeat for disjoint sources")
    parser.add_argument("--out", type=Path, required=True)
    return prepare(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
