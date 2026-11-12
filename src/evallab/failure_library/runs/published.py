"""Offline adapters for pinned public execution tables and oracle-labeled rollouts."""

from __future__ import annotations

from typing import Any

from evallab.failure_library.records import RunCode, RunRecord, TaskRef
from evallab.failure_library.runs.common import label, number, outcome, stable_id

SOURCES = {
    "reward-hacking-ablation": (
        "willjgriff/reward-hacking-ablation",
        "dda898075de555d8bbf30b4598dbe5be878817a2",
    ),
    "olmo-reward-hacking": (
        "ai-safety-institute/reward-hacking-olmo3.1-32b-kl0.02-seed2-rollouts",
        "fb89a90a4a4eeb287148dada7715dbdcc15f6cfe",
    ),
    "termgrade": ("ai-and/termgrade-trajectories", "7d584d63dd598d7e76d444ca23e1527fb5ebd6d9"),
    "hacktrace": ("JH012/HackTrace-Data", "291cf50ffc3cb4d2af42ade5e0cd728cb0037312"),
}




def hacktrace(row: dict[str, Any], ref: str) -> RunRecord:
    kind = "judge" if "judge" in row["label_source"] else "construction"
    labels = [label(f"ACT={int(row['ACT'])}", kind, ref)]
    labels.append(label(f"OUTCOME={int(row['OUTCOME'])}", kind, ref))
    category = row.get("judge_category")
    codes: dict[str, RunCode] = {
        "test_tamper": "RS02",
        "eval_bypass": "RS02",
        "hardcode_output": "RS01",
        "special_casing": "RS01",
    }
    if category:
        labels.append(label(category, "judge", ref, codes.get(category)))
    if row["OUTCOME"]:
        # OUTCOME includes a judge's behavioral decision. Do not upgrade it to executed.
        labels.append(label("reward_hacked", kind, ref, codes.get(category)))
    elif not row["ACT"]:
        labels.append(label("clean", kind, ref))
    for field in ("visible_tests_pass", "hidden_tests_pass"):
        if row.get(field) is not None:
            labels.append(label(f"{field}={str(row[field]).lower()}", "executed", ref))
    reward = number(row.get("visible_tests_pass"))
    return RunRecord(
        run_id="hacktrace:" + row["id"],
        collection="hacktrace",
        task=TaskRef(
            source_id="codecontests",
            task_id=row["problem_name"],
            upstream_repo="https://huggingface.co/datasets/JH012/HackTrace-Data",
            upstream_revision=SOURCES["hacktrace"][1],
        ),
        model="Qwen/Qwen3-8B",
        harness="HackTrace (" + row["variant"] + ")",
        reward=reward,
        outcome=outcome(reward),
        labels=labels,
        transcript_ref=ref,
        license="CC-BY-4.0",
    )






def termgrade(
    row: dict[str, Any], ref: str, *, tests: dict[str, Any] | None, tests_ref: str | None
) -> RunRecord:
    reward = number(row.get("reward"))
    labels = [label("verifier_" + outcome(reward), "executed", ref)]
    if row.get("exceeded_cap"):
        labels.append(label("scoring_budget_exceeded", "executed", ref))
    if tests is not None:
        for index, case in enumerate(tests.get("cases") or []):
            labels.append(
                label(
                    f"assertion:{case['name']}:{case['status']}",
                    "executed",
                    (tests_ref or ref) + f"&field=cases[{index}]",
                )
            )
    else:
        labels.append(label("no_per_assertion_report", "construction", ref))
    return RunRecord(
        run_id="termgrade:" + row["trial"],
        collection="termgrade",
        task=TaskRef(
            source_id="termgrade",
            task_id=row["task"],
            upstream_repo="https://huggingface.co/datasets/ai-and/termgrade-trajectories",
            upstream_revision=SOURCES["termgrade"][1],
            # This is a publisher checksum, not a computed Eval Lab package digest.
            task_path=f"data/trials.parquet#task={row['task']}&task_checksum={row.get('task_checksum', 'unknown')}",
        ),
        model=row.get("model_id", row.get("model")),
        harness="terminus-2",
        reward=reward,
        outcome=outcome(reward, error=bool(row.get("exception"))),
        labels=labels,
        transcript_ref=ref,
        license="Apache-2.0",
    )


def ablation_record(row: dict[str, Any], ref: str, *, identity: str) -> RunRecord:
    """The release's later rhenvs envelope has flat oracle/heuristic labels."""
    env = str(row["env"])
    native = row.get("labels") or {}
    labels = []
    oracle_fields = {
        "passed",
        "passes_visible",
        "passes_original_visible",
        "passes_held_out",
        "passes_all_original",
        "passes_all",
        "honest_correct",
        "tampered",
        "peeked",
        "used_verifier",
        "submitted_true_answer",
        "read_not_used",
        "passed_blind",
        "verifier_positive",
    }
    for name, value in native.items():
        kind = (
            "executed"
            if env in {"terminal_verifier", "mbpp"} and name in oracle_fields
            else "construction"
        )
        labels.append(label(f"{name}={str(value).lower()}", kind, ref))
    verdict = next(
        (native[key] for key in ("passed", "passes_visible", "passes_all") if key in native), None
    )
    result = outcome(number(verdict))
    if row.get("cheated") is not None:
        kind = "executed" if env == "terminal_verifier" else "construction"
        labels.append(label("cheated=" + str(row["cheated"]).lower(), kind, ref))
        if row["cheated"]:
            name = "reward_hacked" if result == "pass" else "reward_hack_behavior"
            labels.append(label(name, kind, ref, "RS01"))
    if (
        env in {"terminal_verifier", "mbpp"}
        and not row.get("cheated")
        and (native.get("passed_blind") is True or native.get("honest_correct") is True)
    ):
        labels.append(label("clean", "executed", ref))
    parts = str(row["rollout_id"]).rsplit("/", 2)
    fallback = parts[-2] if len(parts) >= 2 else "unresolved:" + str(row["rollout_id"])
    task_id = str(native.get("task_id", fallback))
    return RunRecord(
        run_id=stable_id("reward-hacking-ablation", identity),
        collection="reward-hacking-ablation",
        task=TaskRef(
            source_id="ablation-" + env.replace("_", "-"),
            task_id=task_id,
            upstream_repo="https://huggingface.co/datasets/"
            + SOURCES["reward-hacking-ablation"][0],
            upstream_revision=SOURCES["reward-hacking-ablation"][1],
        ),
        model=row.get("model"),
        harness="rhenvs/" + env,
        # These exports report pass flags, but no scalar reward.
        reward=None,
        outcome=result,
        labels=labels,
        transcript_ref=ref,
        split="heldout"
        if row.get("family") == "heldout" or native.get("split") in {"test", "heldout"}
        else "unassigned",
        license=None,
    )
