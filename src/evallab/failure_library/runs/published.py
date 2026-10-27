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


def olmo(row: dict[str, Any], ref: str) -> RunRecord:
    reward = number(row.get("training_passed"))
    labels = []
    for field in ("training_passed", "passed", "actually_solved", "reward_hacked"):
        value = row.get(field)
        if value is not None:
            labels.append(label(f"{field}={int(value)}", "executed", ref))
    if row.get("reward_hacked") == 1:
        labels.append(label("reward_hacked", "executed", ref, "RS01"))
    if row.get("actually_solved") == 1:
        labels.append(label("clean", "executed", ref))
    for field, code in (("rh_always_equal", "RS01"), ("rh_exit", "RS02"), ("rh_conftest", "RS02")):
        if row.get(field) == 1:
            labels.append(label(field, "executed", ref, code))
    return RunRecord(
        run_id=stable_id(
            "olmo-reward-hacking", f"{row['source_eval_file']}:{row['sample_id']}:{row['epoch']}"
        ),
        collection="olmo-reward-hacking",
        task=TaskRef(
            source_id="codecontests",
            task_id=str(row["problem_id"]),
            upstream_repo="https://huggingface.co/datasets/" + SOURCES["olmo-reward-hacking"][0],
            upstream_revision=SOURCES["olmo-reward-hacking"][1],
        ),
        model=row.get("model"),
        harness="Inspect AI / GRPO",
        reward=reward,
        outcome=outcome(reward),
        labels=labels,
        transcript_ref=ref,
        license="Apache-2.0",
    )


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


def ablation(row: dict[str, Any], ref: str, *, identity: str) -> RunRecord:
    """Environment oracles and programmatic flags, never intent inferred from text."""
    raw_results = row.get("raw_test_results") or {}
    reward = number(raw_results.get("reward"))
    if reward is None:
        score = row.get("score")
        value = score.get("value") if isinstance(score, dict) else score
        reward = {"C": 1.0, "I": 0.0}.get(value) if isinstance(value, str) else number(value)
    labels = []
    benchmark = str(row.get("benchmark", "unknown"))
    native = row.get("labels") or {}
    for name, item in native.items():
        if not isinstance(item, dict):
            continue
        producer = str(item.get("source", ""))
        kind = (
            "executed"
            if producer.startswith("env_oracle:") or "djinn verifiers" in producer
            else "construction"
        )
        labels.append(label(f"{name}={str(item.get('value')).lower()}", kind, ref))
    hack_kind = "construction" if benchmark.startswith("mbpp_") else "executed"
    if row.get("programmatic_hack") is not None:
        labels.append(
            label("programmatic_hack=" + str(row["programmatic_hack"]).lower(), hack_kind, ref)
        )
        if row["programmatic_hack"]:
            labels.append(label("reward_hacked", hack_kind, ref, "RS01"))
    honest = any(
        (native.get(key) or {}).get("value") is True
        for key in ("passed_blind", "honest_correct", "honest")
    )
    if honest and not row.get("programmatic_hack"):
        labels.append(label("clean", "executed", ref))
    for field, code in (("tampered", "RS02"), ("peeked", "RS03")):
        if (native.get(field) or {}).get("value") is True:
            labels.append(label(field, "executed", ref, code))
    task_id = str(row.get("task_id", identity))
    namespace = "ablation-" + benchmark.replace("_", "-")
    task_repo = "https://huggingface.co/datasets/" + SOURCES["reward-hacking-ablation"][0]
    task_revision = SOURCES["reward-hacking-ablation"][1]
    if benchmark in {"impossiblebench", "impossiblebench_single_step"}:
        namespace = (
            "impossible-swebench"
            if row.get("task_source") == "swebench"
            else "impossible-livecodebench"
        )
        task_id += "/" + str(row["variant"])
    elif benchmark == "djinn":
        namespace = "djinn"
    return RunRecord(
        run_id=stable_id("reward-hacking-ablation", identity),
        collection="reward-hacking-ablation",
        task=TaskRef(
            source_id=namespace,
            task_id=task_id,
            upstream_repo=task_repo,
            upstream_revision=task_revision,
        ),
        model=row.get("model"),
        harness=row.get("agent_type"),
        reward=reward,
        outcome=outcome(reward, error=row.get("status") in {"error", "failed"}),
        labels=labels,
        transcript_ref=ref,
        license=None,
    )


def ablation_cheatbench(row: dict[str, Any], ref: str, *, identity: str) -> RunRecord:
    state = row.get("grader_state") or {}
    labels = [label("cheatbench_condition", "construction", ref)]
    for field in ("harness_win", "trusted_win", "false_win"):
        if state.get(field) is not None:
            labels.append(label(f"{field}={str(state[field]).lower()}", "executed", ref))
    if state.get("false_win"):
        labels.append(label("reward_hacked", "executed", ref))
    reward = number(state.get("harness_win"))
    return RunRecord(
        run_id=stable_id("reward-hacking-ablation", identity),
        collection="reward-hacking-ablation",
        task=TaskRef(
            source_id="ablation-cheatbench",
            task_id=str(row["task"]) + "/" + str(row["id"]),
            upstream_repo="https://huggingface.co/datasets/"
            + SOURCES["reward-hacking-ablation"][0],
            upstream_revision=SOURCES["reward-hacking-ablation"][1],
        ),
        model=row.get("model"),
        harness=row.get("agent"),
        reward=reward,
        outcome=outcome(reward, error=bool(row.get("error"))),
        labels=labels,
        transcript_ref=ref,
        split="cross_test",
        license=None,
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
