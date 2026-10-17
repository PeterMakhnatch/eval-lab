#!/usr/bin/env python3
"""Prepare bounded reset-context feedback requests and export REAL native corrections.

Default commands are local, read-only with respect to evidence, and $0. They NEVER
execute a model, verifier, patch, or sandbox. The parent owns those executions.

  uv run --no-sync python research/experiments/test-repair-performance-20261017/correction_data.py requests \
    --partition /tmp/envcheck-partition/partition.jsonl --out runs/envcheck-feedback-requests
  HF_HUB_OFFLINE=1 uv run --project tools/modal-mimo-sft --locked python \
    research/experiments/test-repair-performance-20261017/correction_data.py export \
    --requests runs/envcheck-feedback-requests/requests.jsonl --out /tmp/envcheck-corrections \
    --execution <actual-native-runner-output/executions.jsonl>

Existing separately postgraded completions remain accepted through --completion-trial
and --receipt. A native execution already graded on the exact repaired request
snapshot does NOT require another paid replay. Neither path fabricates a receipt.
An empty export writes no fake rows, reports missing prerequisites, and exits 3.
It does not load a model/tokenizer. With measured successful completions, export
reuses to_export.py's model-template-faithful flatten and sft.py's verified loss='last' mask; tokenizer
files must already be cached. No local generative inference or training occurs.

Request schema: envcheck.correction-request/v1. protocol=reset-context-feedback,
purpose=training (all33 excluded BEFORE writing any request). This is NOT restored
history. The actual task prompt is matched to the retained initial user input.
extra_instruction_path/sha256 are existing ExperimentSpec fields, forwarded through
the native queue to Harbor. request.native_spec_fields is a PARTIAL spec override,
not spend authorization: keep the exact model selector and choose authorized runtime,
reference profile and budgets in the normal front door. Requests must live under
this worktree's runs/: the queue refuses external extra-instruction/task paths.
Generation retains a byte-identical private package snapshot there, never edits
the shared frozen source, and emits repo-relative paths plus exact package pins. Extra
instructions contain only the incomplete pre-hidden-test agent patch and a safe
projection of observed failing repaired stdout. Task-visible source files remain
in the legitimate task environment; none are harvested from tests or solutions.
Accepted PARTIAL graders may supply measured mechanical outcomes without becoming
fully qualified: qualified=false and measurement_eligible=true remain distinct,
and variant.manifest_row retains the accepted status and declared survivor scope.
Neither the partition nor successful correction reward is independent semantic truth.

Feedback is a whitelist PROJECTION, never regex-redacted raw stdout: only numeric
pytest/unittest outcome counts, opaque hashed failing-check IDs, and a fixed set
of exception class names survive. Paths, names, assert expressions, source lines,
tracebacks, exception messages, captured stdout and expected/reference values do
not. Unknown/no-failure formats fail closed. Raw stdout is hash-bound in custody,
never included in the model prompt. This deliberately trades detail for no source
leakage; request feedback_policy documents the loss.

Native completion export matches the ACTUAL initial user message to one exact
request and refuses restored histories/model changes. Native execution evidence
binds the real final repaired-package grade under its own distinct measured_basis;
legacy trial inputs join only genuine exact repaired receipts. corrections.jsonl
records outcomes (including missing/infra); recorded.jsonl retains native messages/tools.
conversations.jsonl trains only measured repaired successes with loss='last': one
real post-feedback main-agent assistant target per row, with its exact recorded
prefix, reasoning and tool calls retained. Tool calls are flattened only by the
existing model's own template, checked byte-identically. Earlier assistant turns
are context, NEVER the target. No extracted patch is fabricated into an assistant
message. provenance.jsonl is line-aligned and binds the request, source execution,
completion UUID, patch, repaired receipt, and native message/tool hashes. Children
are retained in the native file hash, not passed off as main-agent calls.

"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import partition as p

PROTOCOL = "reset-context-feedback"
FEEDBACK_POLICY = "source-free-outcome-projection/v1"
EXCEPTIONS = frozenset({
    "AssertionError", "TypeError", "ValueError", "KeyError", "IndexError", "AttributeError",
    "RuntimeError", "NotImplementedError", "ImportError", "ModuleNotFoundError", "NameError",
    "ZeroDivisionError", "OverflowError", "StopIteration", "RecursionError", "SyntaxError",
    "IndentationError", "FileNotFoundError", "PermissionError", "TimeoutError", "OSError",
})
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COUNTS = r"(?:[0-9]+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?))"
PYTEST_SUMMARY = re.compile(
    rf"(?:=+\s*)?({COUNTS}(?:, {COUNTS})*)"
    r"(?: in [0-9]+(?:\.[0-9]+)?s(?: \([0-9:]+\))?)?(?:\s*=+)?"
)


def safe_feedback(text: str) -> tuple[str, dict[str, Any]]:
    """Return only finite-vocabulary diagnostic outcomes; never return an input line."""
    outcomes: dict[str, dict[str, Any]] = {}
    summaries = []
    exceptions = set()
    for raw in ANSI.sub("", text).replace("\r", "\n").splitlines():
        line = raw.strip()
        summary = PYTEST_SUMMARY.fullmatch(line)
        if summary:
            counts = {kind: int(count) for count, kind in re.findall(r"([0-9]+) ([a-z]+)", summary[1])}
            summaries.append(counts)
        unittest = re.fullmatch(r"FAILED \((failures=[0-9]+(?:, errors=[0-9]+)?|errors=[0-9]+)\)", line)
        if unittest:
            summaries.append({k: int(v) for k, v in re.findall(r"(failures|errors)=([0-9]+)", unittest[1])})
        # Pytest short-summary and verbose progress rows. Node IDs are never echoed.
        failed = re.match(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+.*)?$", line)
        progress = re.fullmatch(r"(\S+::\S+)\s+(FAILED|ERROR)(?:\s+\[\s*[0-9]+%\])?", line)
        unitcase = re.match(r"^(FAIL|ERROR):\s+(.+)$", line)
        token = failed[2] if failed else progress[1] if progress else unitcase[2] if unitcase else None
        if token:
            check = p.sha256(token.encode())
            outcomes[check] = {"check": check, "outcome": "error" if (failed and failed[1] == "ERROR") or (progress and progress[2] == "ERROR") or (unitcase and unitcase[1] == "ERROR") else "failed"}
        exception = re.match(r"^(?:E\s+)?([A-Za-z]+)(?::|$)", line)
        if exception and exception[1] in EXCEPTIONS:
            exceptions.add(exception[1])
        # Summary messages are not safe, but a fixed built-in class is.
        if failed and " - " in line:
            name = line.split(" - ", 1)[1].split(":", 1)[0]
            if name in EXCEPTIONS:
                exceptions.add(name)
    failing_counts = any(s.get("failed", 0) + s.get("failures", 0) + s.get("error", 0) + s.get("errors", 0) > 0 for s in summaries)
    if not failing_counts and not outcomes and not exceptions:
        raise ValueError("stdout has no safely recognized failing diagnostic; unknown formats are not forwarded")
    diagnostic = {
        "policy": FEEDBACK_POLICY,
        "outcome_counts": summaries,
        "failing_checks": sorted(outcomes.values(), key=lambda item: item["check"]),
        "exception_classes": sorted(exceptions),
    }
    # Constructed JSON contains no input text except numeric values and opaque digests.
    rendered = p.canonical_bytes(diagnostic).decode() + "\n"
    return rendered, {"policy": FEEDBACK_POLICY, "raw_sha256": p.sha256(text.encode()),
                      "safe_sha256": p.sha256(rendered.encode()),
                      "source_lines_forwarded": 0, "exception_messages_forwarded": 0}


def checked_source(row: dict[str, Any]) -> dict[str, Any]:
    actual = p.source_record(Path(row["source_trial"]))
    if any(actual[k] != row[k] for k in ("execution_id", "task", "model", "original_reward")):
        raise ValueError("source trial identity/model/reward changed")
    aliases = row["provenance"]["aliases"]
    retained = next((a for a in aliases if a["trial"] == actual["source_trial"]), None)
    if retained is None or retained["source_digest"] != actual["alias"]["source_digest"]:
        raise ValueError("source execution bytes changed since inventory")
    for field in ("result", "patch", "native", "trajectory"):
        if retained["files"].get(field) != actual["alias"]["files"].get(field):
            raise ValueError(f"source {field} bytes changed since inventory")
    return actual


def actual_task_prompt(source: dict[str, Any], package: Path) -> tuple[str, dict[str, Any]]:
    files = source["alias"]["files"]
    prompt_file = package / "instruction.md"
    # Harbor loads task text with universal newlines; keep the raw file hash below.
    prompt = prompt_file.read_text(encoding="utf-8")
    if "native" in files:
        messages, _ = p.native_messages(Path(files["native"]["path"]))
    elif "trajectory" in files:
        messages, _, fidelity = p.ARMS.to_messages(p.read_json(Path(files["trajectory"]["path"])))
        if fidelity != "raw_completion":
            raise ValueError("original task input is available only as a reconstruction")
    else:
        raise ValueError("no retained original task input")
    user = next((m for m in messages if m.get("role") == "user"), None)
    if not user or user.get("content", "").strip() != prompt.strip():
        raise ValueError("package prompt differs from actual original user input (possibly extra historical context)")
    return user["content"], p.file_ref(prompt_file)


def feedback_trial(row: dict[str, Any]) -> Path:
    selected = row["provenance"].get("selected_receipt")
    if not selected or not selected.get("valid"):
        raise ValueError("no validated repaired-failure receipt")
    valid = []
    for binding in selected.get("native_results", []):
        path = Path(binding["path"]).parent
        result = p.read_json(path / "result.json")
        if p.score(result) == 0.0 and not result.get("exception_info"):
            valid.append(path.resolve())
    if len(set(valid)) != 1:
        raise ValueError("missing/ambiguous actual repaired-failure native stdout trial")
    return valid[0]


def render_extra_instruction(patch: str, safe: str) -> str:
    return (
        "Reset-context feedback correction. The following incomplete patch was produced for the task above. "
        "Correct it in the task workspace; this is a new conversation, not restored history.\n\n"
        "Incomplete source patch (pre-hidden-test agent diff):\n" + patch +
        "\n\nObserved repaired-verifier failing stdout (source-free diagnostic projection; "
        "source, tracebacks, paths and exception messages omitted):\n" + safe
    )


def checked_evidence(evidence: dict[str, Any]) -> None:
    if p.file_ref(Path(evidence["path"])) != {k: evidence[k] for k in ("path", "sha256")}:
        raise ValueError("canonical repaired receipt file changed")
    doc = p.read_json(Path(evidence["path"]))
    rows = doc["rows"] if isinstance(doc.get("rows"), list) else [doc]
    index = int(evidence["selector"].rsplit("/", 1)[1])
    if rows[index] != evidence["row"] or p.sha256(p.canonical_bytes(rows[index])) != evidence["row_sha256"]:
        raise ValueError("partition disagrees with its untouched canonical repaired receipt")
    for ref in [*evidence.get("native_results", []), *evidence.get("attachments", {}).values()]:
        if p.file_ref(Path(ref["path"])) != {k: ref[k] for k in ("path", "sha256")}:
            raise ValueError("canonical receipt's retained native output/attachment changed")


def request_for(row: dict[str, Any], heldout: set[str], heldout_ref: dict[str, Any], out: Path) -> dict[str, Any]:
    from evallab.task_variants import task_directory_digest

    task = p.parent_task(row["task"])
    # This check precedes reading the patch, feedback, or writing ANY generation input.
    if task in heldout:
        raise ValueError("heldout_parent_excluded_before_generation")
    if row["partition"] != "false_pass" or row["status"] != "measured" or row["original_reward"] != 1.0 or row["repaired_reward"] != 0.0:
        raise ValueError("requires observed original1/repaired0 false pass")
    pin = row["variant"]
    exclusion = pin["manifest_row"]
    if exclusion.get("training_excluded") or exclusion.get("training_exclusion") or exclusion.get("excluded_from_training") or exclusion.get("training_eligible") is False:
        raise ValueError("manifest_training_exclusion")
    flags = row.get("flags") or {}
    if flags.get("alias_conflicts") or flags.get("teacher") or flags.get("copied_fix"):
        raise ValueError("source_alias_teacher_or_copied_fix_flag (retained in partition, not silently cleaned)")
    evidence = row["provenance"]["selected_receipt"]
    checked_evidence(evidence)
    if p.validate_receipt(row, pin, evidence["row"]):
        raise ValueError("source false pass no longer validates against its canonical repaired receipt")
    source = checked_source(row)
    package = Path(pin["package_path"])
    if not p.mechanical_grader_admitted(pin) or task_directory_digest(package) != pin["repaired_digest"]:
        raise ValueError("repaired package is not admitted for mechanical measurement or changed")
    prompt, prompt_ref = actual_task_prompt(source, package)
    patch_ref = source["alias"]["files"].get("patch")
    if not patch_ref:
        raise ValueError("missing actual pre-hidden-test source agent.diff")
    patch = Path(patch_ref["path"]).read_bytes().decode("utf-8")
    if not patch.strip():
        raise ValueError("empty source patch cannot be represented as an incomplete fix")
    trial = feedback_trial(row)
    stdout = trial / "verifier/test_output.log"
    if not stdout.is_file():
        raise ValueError("native repaired failing test_output.log is unavailable")
    raw = stdout.read_bytes().decode("utf-8")
    safe, policy = safe_feedback(raw)
    extra = render_extra_instruction(patch, safe)
    ident = p.sha256(p.canonical_bytes({"source_execution_id": row["execution_id"],
                     "repaired_digest": pin["repaired_digest"], "extra_instruction_sha256": p.sha256(extra.encode())})).removeprefix("sha256:")
    snapshot = out / "packages" / pin["repaired_digest"].removeprefix("sha256:")
    if not snapshot.exists():
        if any(path.is_symlink() for path in package.rglob("*")):
            raise ValueError("task package snapshots reject symlinks")
        snapshot.parent.mkdir(exist_ok=True)
        shutil.copytree(package, snapshot)
    if task_directory_digest(snapshot) != pin["repaired_digest"] or task_directory_digest(package) != pin["repaired_digest"]:
        raise ValueError("frozen package changed while copying its private request snapshot")
    directory = out / ident
    directory.mkdir()
    (directory / "task-prompt.txt").write_text(prompt)
    (directory / "source.patch").write_text(patch)
    (directory / "feedback.txt").write_text(safe)
    (directory / "extra-instruction.txt").write_text(extra)
    correction = {
        "source_execution_id": row["execution_id"], "protocol": PROTOCOL,
        "partition_scope": "mechanical_repaired_grader",
        "extra_instruction": p.file_ref(directory / "extra-instruction.txt"),
        "source_patch": patch_ref, "feedback": p.file_ref(stdout),
        "safe_feedback": p.file_ref(directory / "feedback.txt"),
        "task_prompt": prompt_ref, "feedback_result": p.file_ref(trial / "result.json"),
        "task_package_snapshot": {"path": str(snapshot), "sha256": pin["repaired_digest"]},
        "allowed_model_inputs": ["task_prompt", "source_patch", "repaired_failure_stdout"],
        "hidden_test_source_included": False, "reference_source_included": False,
        "full_historical_context_included": False,
    }
    request = {
        "schema": "envcheck.correction-request/v1", "request_id": ident,
        "purpose": "training", "protocol": PROTOCOL, "task": task,
        "model": row["model"], "source_execution_id": row["execution_id"],
        "heldout": False, "heldout_manifest": heldout_ref,
        "partition_scope": "mechanical_repaired_grader",
        "feedback_policy": policy, "source_partition": row,
        "task_prompt": p.file_ref(directory / "task-prompt.txt"),
        "patch_input": p.file_ref(directory / "source.patch"),
        "native_spec_fields": {
            "task": task, "task_path": snapshot.relative_to(p.REPO).as_posix(),
            "task_package_digest": pin["repaired_digest"], "agent": "mimoagent", "model": row["model"],
            "extra_instruction_path": (directory / "extra-instruction.txt").relative_to(p.REPO).as_posix(),
            "extra_instruction_sha256": correction["extra_instruction"]["sha256"],
        },
        "provenance": {"correction": correction},
    }
    p.write_json(directory / "request.json", request)
    return {**request, "request": p.file_ref(directory / "request.json")}


def prepare(args: argparse.Namespace) -> int:
    args.out = args.out.resolve()
    if not args.out.is_relative_to(p.REPO / "runs"):
        raise ValueError("native correction requests must live under this worktree's runs/ (queue path custody)")
    heldout, heldout_ref = p.frozen_heldout(args.heldout_manifest)
    rows = p.read_jsonl(args.partition)
    args.out.mkdir(parents=True, exist_ok=False)
    requests = []
    rejected = []
    seen = set()
    for row in rows:
        key = row["execution_id"], row["repaired_digest"]
        if key in seen:
            raise ValueError(f"duplicate execution/grader join row: {key}")
        seen.add(key)
        if row["partition"] != "false_pass":
            continue
        try:
            requests.append(request_for(row, heldout, heldout_ref, args.out))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rejected.append({"source_execution_id": key[0], "repaired_digest": key[1],
                             "task": row["task"], "reason": f"{type(exc).__name__}: {exc}"})
    p.write_jsonl(args.out / "requests.jsonl", requests)
    p.write_jsonl(args.out / "rejected.jsonl", rejected)
    summary = {"requests": len(requests), "rejected": len(rejected), "partition": p.file_ref(args.partition),
               "heldout_manifest": heldout_ref, "protocol": PROTOCOL, "model_calls": 0,
               "missing_prerequisites": [] if requests else ["no eligible nonheldout observed false pass with actual source patch, matching task input and safely extractable repaired failing stdout"]}
    p.write_json(args.out / "summary.json", summary)
    print(p.canonical_bytes(summary).decode())
    return 0 if requests else 3


def checked_request(row: dict[str, Any], heldout: set[str], heldout_ref: dict[str, Any]) -> None:
    if p.parent_task(row["task"]) in heldout:
        raise ValueError("heldout_parent_excluded_at_export")
    if (row.get("purpose") != "training" or row.get("protocol") != PROTOCOL or row.get("heldout_manifest") != heldout_ref
            or row.get("heldout") is not False or row.get("training_allowed") is False):
        raise ValueError("request has wrong protocol/purpose/frozen heldout custody")
    if p.file_ref(Path(row["request"]["path"])) != row["request"]:
        raise ValueError("request receipt bytes changed")
    if p.read_json(Path(row["request"]["path"])) != {k: v for k, v in row.items() if k != "request"}:
        raise ValueError("request JSONL disagrees with its retained request receipt")
    correction = row["provenance"]["correction"]
    for ref in (row["task_prompt"], row["patch_input"], correction["extra_instruction"],
                correction["safe_feedback"], correction["feedback_result"], correction["task_prompt"],
                correction["source_patch"]):
        if p.file_ref(Path(ref["path"])) != ref:
            raise ValueError(f"request input changed: {ref['path']}")
    source = checked_source(row["source_partition"])
    checked_evidence(row["source_partition"]["provenance"]["selected_receipt"])
    baseline = row["source_partition"]
    pin = baseline["variant"]
    if (baseline.get("partition") != "false_pass" or baseline.get("status") != "measured"
            or baseline.get("original_reward") != 1.0 or baseline.get("repaired_reward") != 0.0):
        raise ValueError("request no longer binds an observed original1/repaired0 false pass")
    if not p.mechanical_grader_admitted(pin) or p.validate_receipt(baseline, pin, baseline["provenance"]["selected_receipt"]["row"]):
        raise ValueError("request's source repaired-failure grader/receipt no longer validates")
    manifest = pin["manifest_row"]
    if (any(manifest.get(key) for key in ("training_excluded", "training_exclusion", "excluded_from_training"))
            or manifest.get("training_eligible") is False):
        raise ValueError("manifest_training_exclusion")
    if any((baseline.get("flags") or {}).get(key) or source["flags"].get(key)
           for key in ("teacher", "copied_fix", "alias_conflicts", "attempt_conflict")):
        raise ValueError("source_alias_teacher_copied_or_attempt_conflict")
    if (source["execution_id"], source["task"], source["model"]) != (row["source_execution_id"], row["task"], row["model"]):
        raise ValueError("request changed original source UUID/task/model")
    from evallab.task_variants import task_directory_digest

    snapshot = correction["task_package_snapshot"]
    if task_directory_digest(Path(snapshot["path"])) != snapshot["sha256"]:
        raise ValueError("native request's pinned task snapshot changed")
    prompt, _ = actual_task_prompt(source, Path(row["source_partition"]["variant"]["package_path"]))
    if Path(row["task_prompt"]["path"]).read_bytes().decode("utf-8") != prompt:
        raise ValueError("request prompt differs from its actual original task input")
    if row["patch_input"]["sha256"] != correction["source_patch"]["sha256"]:
        raise ValueError("request patch differs from the actual incomplete source patch")
    # Regenerate the safe derivative from the digest-bound original stdout, not a safety claim.
    if p.file_ref(Path(correction["feedback"]["path"])) != correction["feedback"]:
        raise ValueError("repaired feedback stdout bytes changed")
    safe, _ = safe_feedback(Path(correction["feedback"]["path"]).read_bytes().decode("utf-8"))
    if safe != Path(correction["safe_feedback"]["path"]).read_bytes().decode("utf-8"):
        raise ValueError("safe feedback is not the source-free projection of its observed stdout")
    patch = Path(row["patch_input"]["path"]).read_bytes().decode("utf-8")
    if render_extra_instruction(patch, safe) != Path(correction["extra_instruction"]["path"]).read_bytes().decode("utf-8"):
        raise ValueError("request contains inputs beyond the bound patch and source-free feedback")


def match_request(source: dict[str, Any], requests: list[dict[str, Any]], messages: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    result = p.read_json(Path(source["source_trial"]) / "result.json")
    config = result.get("config") or {}
    agent = config.get("agent") or {}
    if agent.get("resume_trajectory") or agent.get("load_trajectory"):
        raise ValueError("restored historical context is not a reset-context correction")
    first_user = next((i for i, message in enumerate(messages) if message.get("role") == "user"), None)
    if first_user is None or any(m.get("role") != "system" for m in messages[:first_user]):
        raise ValueError("native initial input contains prior history")
    content = messages[first_user].get("content")
    if not isinstance(content, str):
        raise ValueError("native correction prompt is not text")
    matches = []
    for request in requests:
        if request["task"] != source["task"] or request["model"] != source["model"]:
            continue
        prompt = Path(request["task_prompt"]["path"]).read_text().strip()
        extra = Path(request["provenance"]["correction"]["extra_instruction"]["path"]).read_text().strip()
        if content.strip().startswith(prompt) and content.strip()[len(prompt):].strip() == extra:
            matches.append(request)
    if len(matches) != 1:
        raise ValueError("actual native task+feedback input/model does not match exactly one bound request")
    return matches[0], first_user


def native_grade(
    completed: dict[str, Any], request: dict[str, Any], receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    from native_completion import NATIVE_MEASURED_BASIS

    source, execution = completed["source"], completed["execution"]
    pin = request["source_partition"]["variant"]
    terminal = execution["status"] in {"completed_repaired_pass", "completed_repaired_fail"}
    reward = completed["native_reward"] if terminal else None
    status = "measured" if terminal else execution["status"]
    postgrade = p.join_one(source, pin, receipts) if receipts else None
    if postgrade and (postgrade["status"] == "conflicting_receipts"
                      or (postgrade["status"] == "measured" and terminal and postgrade["repaired_reward"] != reward)):
        status = "conflicting_receipts"
        reward = None
    return {
        **source,
        # This completion was not run on the weak original grader. Preserve its
        # actual native reward separately, never manufacture a before/after pair.
        "original_reward": None, "native_reward": completed["native_reward"],
        "repaired_reward": reward, "repaired_digest": pin["repaired_digest"],
        "status": status, "native_execution_status": execution["status"],
        "partition": "repaired_pass" if reward == 1.0 else "repaired_fail" if reward == 0.0 else "unmeasured",
        "partition_scope": "mechanical_repaired_grader", "variant": pin,
        "measured_basis": NATIVE_MEASURED_BASIS, "executed_package": completed["executed_package"],
        "receipt": postgrade["receipt"] if postgrade else None,
        "baseline_runtime_matched": execution["baseline_runtime_matched"],
        "declared_runtime_changes": execution["declared_runtime_changes"], "notice": execution["notice"],
        "provenance": {
            **source["provenance"], "native_execution": completed["execution_binding"],
            "native_result": execution["native_result"], "final_repaired_result": execution["final_repaired_result"],
            "native_spec": execution["native_files"]["experiment-spec.json"],
            "native_lock": execution["native_files"]["lock"],
            "native_fingerprint": execution["native_files"]["fingerprint"],
            "receipts": postgrade["provenance"]["receipts"] if postgrade else [],
            "selected_receipt": postgrade["provenance"]["selected_receipt"] if postgrade else None,
            "postgrade_status": postgrade["status"] if postgrade else None,
        },
    }


def native_candidates(
    paths: list[Path], requests: list[dict[str, Any]], heldout: set[str], excluded: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not paths:
        return [], set()
    import native_completion as n

    by_execution: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    seen = set()
    for path in paths:
        file_ref = p.file_ref(path)
        for index, row in enumerate(p.read_jsonl(path)):
            ident = row.get("execution_id")
            binding = {**file_ref, "row_index": index, "row_sha256": p.sha256(p.canonical_bytes(row))}
            if not ident:
                excluded.append({"native_execution": row, "execution_binding": binding,
                                 "reason": "native observation has no actual terminal trial UUID"})
                continue
            seen.add(ident)
            by_execution[ident].append((row, binding))
    candidates = []
    for ident, observations in by_execution.items():
        if len({binding["row_sha256"] for _, binding in observations}) != 1:
            excluded.append({"execution_id": ident, "native_executions": [row for row, _ in observations],
                             "execution_bindings": [binding for _, binding in observations],
                             "reason": "conflicting native observations for one actual execution UUID"})
            continue
        row, binding = observations[0]
        try:
            if p.parent_task(row["task"]) in heldout:
                raise ValueError("heldout_parent_excluded_at_export")
            if row.get("purpose") != "training" or row.get("heldout") is not False or row.get("training_allowed") is not True:
                raise ValueError("evaluation/nontraining native observation cannot become training")
            matches = [request for request in requests if request["request_id"] == row.get("request_id")]
            if len(matches) != 1:
                raise ValueError("native execution must select exactly one retained training request")
            request = matches[0]
            exact_binding = n.execution_binding(Path(binding["path"]), ident)
            if exact_binding != binding:
                raise ValueError("native execution input changed while selecting its untouched row")
            completed = n.checked_completion(request, exact_binding)
            candidates.append({"completed": completed, "request": request})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Keep real zeros, missing grades, infra and budget stops as native
            # observations even when their artifacts cannot admit a target.
            excluded.append({"execution_id": ident, "execution_binding": binding, "native_execution": row,
                             "reason": f"{type(exc).__name__}: {exc}"})
    return candidates, seen


def clean_student(source: dict[str, Any]) -> bool:
    flags = source["flags"]
    metadata = flags.get("agent_metadata") or {}
    raw = p.read_json(Path(source["source_trial"]) / "result.json")
    return (
        not any(flags.get(key) for key in ("exception_info", "teacher", "copied_fix", "alias_conflicts", "attempt_conflict"))
        and flags.get("native_exit_status") == "Idle" and flags.get("fidelity") == "native_messages"
        and not metadata.get("infra_error")
        and metadata.get("stop_reason") not in {"trial_budget_exhausted", "context_exhausted"}
        and bool(raw.get("finished_at"))
    )


def export(args: argparse.Namespace) -> int:
    heldout, heldout_ref = p.frozen_heldout(args.heldout_manifest)
    requests = p.read_jsonl(args.requests)
    if len({request["request_id"] for request in requests}) != len(requests):
        raise ValueError("duplicate retained correction request IDs")
    for request in requests:
        checked_request(request, heldout, heldout_ref)
    receipts = p.receipt_rows(args.receipt)
    by_execution: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in receipts:
        key = p.receipt_execution(evidence["row"])
        if key:
            by_execution[key].append(evidence)
    corrections, recorded, excluded, conversations, provenance = [], [], [], [], []
    execution_paths = getattr(args, "execution", [])
    native, seen = native_candidates(execution_paths, requests, heldout, excluded)
    candidates = []
    for candidate in native:
        completed, request = candidate["completed"], candidate["request"]
        source = completed["source"]
        messages, tools = completed["messages"], completed["tools"]
        _, boundary = match_request(source, [request], messages)
        grade = native_grade(completed, request, by_execution.get(source["execution_id"], []))
        candidates.append((source, request, messages, tools, boundary, completed["patch"], completed["patch_custody"], grade))
    legacy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in args.completion_trial:
        try:
            source = p.source_record(trial)
            if source["execution_id"] not in seen:
                legacy[source["execution_id"]].append(source)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            excluded.append({"trial": str(trial), "reason": f"{type(exc).__name__}: {exc}"})
    for ident, sources in legacy.items():
        signatures = {
            (source["alias"]["source_digest"], *(source["alias"]["files"].get(key, {}).get("sha256")
                                                 for key in ("native", "patch", "lock", "fingerprint")))
            for source in sources
        }
        if len(signatures) != 1:
            excluded.append({"execution_id": ident, "reason": "conflicting native completion aliases",
                             "aliases": [source["alias"] for source in sources]})
            continue
        source = sources[0]
        trial = Path(source["source_trial"])
        # Repeat original-parent exclusion BEFORE reading any completion target.
        if source["task"] in heldout:
            excluded.append({"trial": str(trial), "reason": "heldout_parent_excluded_at_export"})
            continue
        files = source["alias"]["files"]
        if "native" not in files or "patch" not in files:
            excluded.append({"trial": str(trial), "reason": "missing native messages/legacy patch; retained kept captures require --execution"})
            continue
        try:
            messages, tools = p.native_messages(Path(files["native"]["path"]))
            request, boundary = match_request(source, requests, messages)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            excluded.append({"trial": str(trial), "reason": str(exc)})
            continue
        alias = source.pop("alias")
        source["provenance"] = {"trial_uuid": ident, "job_uuid": alias["job_uuid"],
                                "source_result_sha256": files["result"]["sha256"], "aliases": [alias]}
        grade = p.join_one(source, request["source_partition"]["variant"], by_execution.get(ident, []))
        grade["measured_basis"] = "separate_repaired_postgrade_receipt"
        candidates.append((source, request, messages, tools, boundary, files["patch"], None, grade))
    converter = None
    tok = None
    template_prerequisite = None
    for source, request, messages, tools, boundary, patch, custody, grade in candidates:
        files = source["provenance"]["aliases"][0]["files"]
        correction = {
            **grade, "source_execution_id": request["source_execution_id"], "request_id": request["request_id"],
            "protocol": PROTOCOL, "purpose": "training", "heldout": False,
            "corrected_patch": patch, "corrected_patch_custody": custody,
            "source_original_reward": request["source_partition"]["original_reward"],
            "provenance": {**grade["provenance"], "correction": request["provenance"]["correction"],
                           "request": request["request"], "native_trajectory": files["native"]},
        }
        corrections.append(correction)
        recorded.append({**correction, "messages": messages, "tools": tools,
                         "source_messages_sha256": p.sha256(p.canonical_bytes(messages)),
                         "source_tools_sha256": p.sha256(p.canonical_bytes(tools))})
        if grade["status"] != "measured" or grade["repaired_reward"] != 1.0:
            excluded.append({"execution_id": source["execution_id"], "reason": "no measured repaired success",
                             "status": grade["status"], "native_reward": grade.get("native_reward"),
                             "repaired_reward": grade["repaired_reward"], "flags": source["flags"]})
            continue
        if not clean_student(source):
            excluded.append({"execution_id": source["execution_id"], "reason": "native correction is not a clean student completion",
                             "flags": source["flags"]})
            continue
        if converter is None and template_prerequisite is None:
            # Conversion is CPU tokenizer work only. Never auto-fetch or run a model.
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                converter = p.load_script("envcheck_sft_export", "research/experiments/repaired-data-sft-v1/to_export.py")
                tok = converter.sft.get_tokenizer()
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                template_prerequisite = f"cached tokenizer/template export dependencies unavailable: {type(exc).__name__}: {exc}"
        if template_prerequisite:
            excluded.append({"execution_id": source["execution_id"], "reason": template_prerequisite})
            continue
        for index, message in enumerate(messages):
            if index <= boundary or message.get("role") != "assistant":
                continue
            prefix = messages[: index + 1]
            label = f"{source['execution_id']}:{index}"
            row = {"agent": "mimoagent", "execution_id": source["execution_id"], "messages": prefix, "tools": tools}
            try:
                flat = converter.flatten(tok, row)
                rendered = converter.sft.render_and_mask(tok, flat, max_length=args.max_length, label=label, loss="last")
            except (converter.sft.SftError, ValueError, KeyError, TypeError) as exc:
                excluded.append({"target": label, "reason": str(exc)})
                continue
            conversations.append({"messages": flat, "loss": "last"})
            provenance.append({
                **correction, "target_message_index": index, "target": label,
                "fidelity": "native_messages_model_template_equal", "render_equal_to_recorded": True,
                "source_messages_sha256": p.sha256(p.canonical_bytes(prefix)),
                "source_tools_sha256": p.sha256(p.canonical_bytes(tools)),
                "target_message_sha256": p.sha256(p.canonical_bytes(message)),
                "sequence_tokens": len(rendered.input_ids), "trained_tokens": rendered.trained,
                "loss": "last", "heldout_manifest": heldout_ref,
            })
    args.out.mkdir(parents=True, exist_ok=False)
    for name, rows in (("corrections", corrections), ("recorded", recorded),
                       ("conversations", conversations), ("provenance", provenance), ("excluded", excluded)):
        p.write_jsonl(args.out / f"{name}.jsonl", rows)
    missing = []
    if not args.completion_trial and not execution_paths:
        missing.append("actual same-target native correction evidence (--execution or --completion-trial)")
    if args.completion_trial and not args.receipt and not execution_paths:
        missing.append("genuine repaired postgrade receipts for legacy completion trials (--receipt), or direct repaired-native evidence (--execution)")
    if not conversations:
        missing.append("measured successful, nonheldout native correction targets with template-faithful loss='last'")
    if template_prerequisite:
        missing.append(template_prerequisite)
    summary = {
        "eligible_completed_corrections": len(corrections), "training_targets": len(conversations),
        "excluded": len(excluded), "repaired_statuses": dict(Counter(c["status"] for c in corrections)),
        "measured_bases": dict(Counter(c["measured_basis"] for c in corrections)),
        "requests": p.file_ref(args.requests), "executions": [p.file_ref(path) for path in execution_paths],
        "heldout_manifest": heldout_ref, "heldout_ids": len(heldout), "heldout_applied": True, "protocol": PROTOCOL,
        "loss": "last", "export_ready": bool(conversations), "missing_prerequisites": missing,
        "model_calls": 0, "verifier_executions": 0,
    }
    p.write_json(args.out / "manifest.json", {
        "contract": "evallab.sft_terminus/1", "conversations_sha256": p.file_ref(args.out / "conversations.jsonl")["sha256"],
        "heldout_applied": True, "heldout_manifest": heldout_ref, "export_ready": bool(conversations),
        "tasks": sorted({r["task"] for r in provenance}), "loss": "last",
    })
    p.write_json(args.out / "summary.json", summary)
    print(p.canonical_bytes(summary).decode())
    return 0 if conversations else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    requests = commands.add_parser("requests", help="$0 source-free reset-context request generation; excludes heldout33")
    requests.add_argument("--partition", type=Path, required=True)
    requests.add_argument("--out", type=Path, required=True)
    export_parser = commands.add_parser("export", help="$0 actual native completion-to-trainer export; never model execution")
    export_parser.add_argument("--requests", type=Path, required=True)
    export_parser.add_argument("--completion-trial", type=Path, action="append", default=[])
    export_parser.add_argument("--receipt", type=Path, action="append", default=[])
    export_parser.add_argument("--execution", type=Path, action="append", default=[],
                               help="untouched native runner executions.jsonl; repeat for independent jobs")
    export_parser.add_argument("--max-length", type=int, default=65536)
    export_parser.add_argument("--out", type=Path, required=True)
    for command in (requests, export_parser):
        command.add_argument("--heldout-manifest", type=Path, default=p.DEFAULT_HELDOUT)
    args = parser.parse_args()
    return {"requests": prepare, "export": export}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
