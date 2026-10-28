"""Normalize native Wrench, Goodfire, CheatBench and redistributed MALT rows."""

from __future__ import annotations

from typing import Any

from evallab.failure_library.records import RunCode, RunRecord, TaskRef
from evallab.failure_library.runs.common import label, number, outcome, stable_id

WRENCH_REVISION = "6ab64c43a4c6a4d9aa53c2425f37e715257123a3"
WRENCH_INDEX_REVISION = "622449769ec6ddf33f770794489b8cd21712a0ad"
GOODFIRE_REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
CHEATBENCH_REVISION = "461360b34e93644955563dcc9243c6cc1a53bd0f"

WRENCH_CODES: dict[str, RunCode] = {
    "binary-hijacking": "RS02",
    "mutable-input-tampering": "RS02",
    "structural-extraction": "RS03",
    "deceptive-rationalization": "RS04",
    "hollow-implementation": "RS01",
    "output-spoofing": "RS01",
    "metric-spoofing": "RS01",
    "keyword-gaming": "RS01",
    "constraint-loophole": "RS01",
    "algorithmic-simplification": "RS01",
    "security-downgrading": "RS01",
}


def wrench(row: dict[str, Any], ref: str, *, label_ref: str) -> RunRecord:
    reward = number(row.get("reward"))
    classification = row["classification"]
    labels = [label("verifier_" + outcome(reward), "executed", label_ref)]
    # The source index calls these stored judgments, not execution-confirmed hacks.
    if classification == "baseline":
        labels.append(label("baseline_condition", "construction", label_ref))
    else:
        labels.append(label(classification, "judge", label_ref))
        if classification in {"rewarded_serious_exploit", "rewarded_nonserious_hack"}:
            labels.append(label("reward_hacked", "judge", label_ref))
        elif classification == "attacker_legitimate_solve":
            labels.append(label("clean", "judge", label_ref))
        for category in row.get("exploit_categories") or []:
            labels.append(label(category, "judge", label_ref, WRENCH_CODES.get(category)))
    sources = row.get("source_datasets") or []
    namespace = "terminal-bench-2" if "terminal-bench__2.0" in sources else "terminal-wrench"
    return RunRecord(
        run_id=stable_id("terminal-wrench", row["original_trajectory_path"]),
        collection="terminal-wrench",
        task=TaskRef(
            source_id=namespace,
            task_id=str(row["task_id"]),
            upstream_repo="https://huggingface.co/datasets/few-sh/terminal-wrench",
            upstream_revision=WRENCH_REVISION,
            task_path="tasks/" + str(row["task_id"]),
        ),
        model=row.get("model"),
        harness="Harbor",
        reward=reward,
        outcome=outcome(reward),
        labels=labels,
        transcript_ref=ref,
        license="Apache-2.0",
    )


def goodfire(
    row: dict[str, Any],
    ref: str,
    *,
    stream: str,
    judgment: dict[str, Any] | None = None,
    label_ref: str | None = None,
) -> RunRecord:
    traces = row.get("traces") or []
    if len(traces) != 1:
        raise ValueError("Goodfire row must contain exactly one native trace")
    trace = traces[0]
    task = trace["task"]["data"]
    name = str(task.get("name", task.get("task_id", task.get("idx", row["id"]))))
    env_id = row.get("env", {}).get("id", "unknown")
    namespace = {
        "impossible_bench": "impossible-livecodebench",
        "swe_bench": "swe-bench",
        "swebench_verified": "swe-bench",
        "deep_swe": "deepswe",
    }.get(env_id, "goodfire-" + str(env_id).replace("_", "-"))
    task_repo = "https://huggingface.co/datasets/goodfire/reward-hack-data"
    task_revision = GOODFIRE_REVISION
    if trace["task"].get("type") == "ImpossibleBenchTask":
        namespace = (
            "impossible-swebench" if task.get("suite") == "swebench" else "impossible-livecodebench"
        )
        name += "/" + str(task["split"])
        dataset = (
            "fjzzq2002/impossible_swebench"
            if task.get("suite") == "swebench"
            else "fjzzq2002/impossible_livecodebench"
        )
        task_repo = "https://huggingface.co/datasets/" + dataset
        task_revision = task.get("dataset_revision")
    elif stream.startswith("swebench-verified-"):
        namespace = "swe-bench"
        name = name.rsplit("/", 1)[-1]
    rewards = trace.get("rewards") or {}
    # ImpossibleBench's shortcut_signal is not the task's executed reward.
    value = next(
        (
            rewards[key]
            for key in (
                "reward",
                "resolved",
                "official_success",
                "tau3_reward",
                "accuracy",
                "passed",
            )
            if key in rewards
        ),
        None,
    )
    reward = number(value.get("score") if isinstance(value, dict) else value)
    test_passed = number((trace.get("metrics") or {}).get("test_passed"))
    # A test verdict is an outcome, but is not an invented numeric reward.
    executed_outcome = outcome(reward if reward is not None else test_passed)
    labels = []
    if executed_outcome != "unknown":
        labels.append(label("verifier_" + executed_outcome, "executed", ref))
    if judgment is not None:
        source = label_ref or ref
        verdict = judgment.get("verdict_enacted", judgment.get("verdict"))
        if verdict is None:
            verdict = judgment.get("flagged", judgment.get("monitor_flagged"))
        if verdict is not None:
            labels.append(label("monitor_verdict:" + str(verdict).lower(), "judge", source))
            if verdict in {"yes", True}:
                labels.append(label("reward_hack_behavior", "judge", source))
            elif verdict in {"no", False}:
                labels.append(label("clean", "judge", source))
        for category in judgment.get("categories") or []:
            labels.append(label(str(category), "judge", source))
    config = trace.get("agent", {}).get("config", {})
    harness = config.get("harness", {})
    return RunRecord(
        run_id=f"goodfire-reward-hack-data:{stream}:{trace['id']}",
        collection="goodfire-reward-hack-data",
        task=TaskRef(
            source_id=namespace,
            task_id=name,
            upstream_repo=task_repo,
            upstream_revision=task_revision,
            task_path=task.get("task_dir"),
        ),
        model=config.get("model"),
        harness=harness.get("id") if isinstance(harness, dict) else str(harness),
        reward=reward,
        outcome="error" if not bool(row.get("ok", trace.get("ok", True))) else executed_outcome,
        labels=labels,
        transcript_ref=ref,
        # The release does not declare a dataset license; do not infer one from code.
        license=None,
    )


def cheatbench(row: dict[str, Any], ref: str) -> RunRecord:
    native_label = row["label"]
    labels = [label(native_label, "human", ref)]
    if native_label == "benign":
        labels.append(label("clean", "human", ref))
    elif native_label == "cheating":
        labels.append(label("reward_hacked", "human", ref))
    mechanism = row.get("cheating_mechanism")
    if mechanism and mechanism != "none":
        labels.append(label(str(mechanism), "human", ref))
    return RunRecord(
        run_id="cheatbench:" + row["trace_id"],
        collection="cheatbench",
        task=TaskRef(
            source_id="cheatbench-" + row["benchmark"].lower().replace("_", "-"),
            # The table has no source task id; a trace id is explicitly an unresolved task.
            task_id="unresolved:" + row["trace_id"],
            upstream_repo="https://huggingface.co/datasets/steinad/CheatBench",
        ),
        model=row.get("model"),
        harness=row.get("harness"),
        reward=None,
        outcome="unknown",
        labels=labels,
        transcript_ref=ref,
        split="cross_test",
        license="MIT",
    )


def redistributed_malt(row: dict[str, Any], ref: str) -> RunRecord:
    if row["source_dataset"] != "metr_malt":
        raise ValueError("not a redistributed MALT row")
    source = ref + "&field=verification"
    labels = [label(str(row["label"]), "human", source)]
    if row["label"] == "hacking":
        labels.append(label("reward_hacked", "human", source))
    elif row["label"] in {"clean", "normal"}:
        labels.append(label("clean", "human", source))
    return RunRecord(
        run_id="metr-malt:" + str(row["source_id"]),
        collection="metr-malt",
        task=TaskRef(
            source_id="metr-malt",
            task_id="unresolved-run:" + str(row["source_id"]),
            upstream_repo="https://huggingface.co/datasets/metr-evals/malt-public",
        ),
        model=row.get("response_model"),
        harness="METR (redistributed text)",
        reward=None,
        outcome="unknown",
        labels=labels,
        transcript_ref=ref + "&field=response_hacking,response_clean",
        # The redistribution omits task ids. No honest task-family split is possible.
        split="heldout",
        license="CC-BY-SA-4.0",
    )
