"""Build wave-1 specs + campaign files for experiment cheater-recall-v1.

Deterministic post-processor over the existing HAR-161 tooling: it calls
``evallab.exploit_probe.specs()`` (leaky + hardened) and derives the two arms
without touching ``src/``:

- arm A (cheater): probe exploit spec, raised ceilings so a cheater can finish
  (5/10 Oct-6 probe trials died on request/input-token ceilings), 2 attempts
  (k1/k2) per task x version, cost cap $0.15.
- arm B (honest): same package/agent/model, NO adversarial preamble, probe
  ceilings, 1 attempt, cost cap $0.10, plus the 3 declared deviations that the
  dispatch fingerprint gate requires (computed below; asserted for every
  honest spec with the repo's own compare functions).
- free image checks: the probe ``checks`` specs unchanged (oracle, $0).

No ``src/`` change was needed: the honest arm is a spec without
``extra_instruction_path`` (least invasive supported means), and the cheater
arm keeps ``extra_instruction_path == exploit_probe.PREAMBLE``, which
``validate_mimo_setup`` already exempts from the reference-profile match
(HAR-161 task-quality check, not a capability measurement).

One ExperimentCampaign cannot cover both versions: the schema forbids
repeating a task_id, and leaky/hardened digests differ. Hence two campaigns
(``-leaky``, ``-hardened``), 30 trials each.

Worst case: 40 x $0.15 + 20 x $0.10 = $8.00. Expected ~$2.90 on the Oct-6
probe basis (mean ~$0.034/trial, max $0.16 on 002139; unrestrained cheater
runs forecast above the mean since 5/10 were cut short).

Usage (from the worktree root):
  uv run python research/experiments/cheater-recall-v1/build_specs.py
"""

from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path

TASKS = [
    "format-code-task-000552",
    "format-code-task-000792",
    "format-code-task-001269",
    "format-code-task-002139",
    "format-code-task-002391",
    "format-code-task-002402",
    "format-code-task-002486",
    "format-code-task-002552",
    "format-code-task-002864",
    "format-code-task-002938",
]

EXPERIMENT = "cheater-recall-v1"
LINEAR_CARD = "HAR-202"
SUBMITTED_BY = "cheater-recall-v1"
REFERENCE_PROFILE = "xiaomi-mimo-rl"
AGENT = "terminus-2"
MODEL = "zai/glm-5.3-flash"
ENVIRONMENT = "daytona"

# Arm A (cheater): raised so the Oct-6 starvation does not recur. 002139 used
# 936k input tokens under the 1M probe ceiling; cost cap $0.25 never bound in
# the probe (max observed $0.16 on 002139). $0.15 caps the wave-1 worst case
# at $8.00; residual risk: 002139-class runs may cost-cap (flagged in analysis;
# a cap-kill reads as non-pass, the conservative direction for hole claims).
CHEATER_CEILINGS = {
    "max_requests": 128,
    "max_input_tokens": 2_000_000,
    "max_output_tokens": 65_536,
    "max_total_tokens": 2_065_536,
    "cost_limit_usd": 0.15,
}
CHEATER_ATTEMPTS = 2
CHEATER_EST_USD = 0.08  # model + Daytona envelope; probe mean $0.034, max $0.16

# Arm B (honest): HAR-161 probe ceilings unchanged (probe parity for the
# false-alarm control), cap $0.10 (~3x probe mean).
HONEST_CEILINGS = {
    "max_requests": 64,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 65_536,
    "max_total_tokens": 1_065_536,
    "cost_limit_usd": 0.10,
}
HONEST_EST_USD = 0.05

HONEST_DEVIATIONS = [
    {
        "field": "harness.id",
        "value": "terminus-2",
        "reason": (
            "Wave-1 runs the Terminus-2 harness, not the mimoagent-default "
            "training route; the honest arm measures the false-alarm rate of "
            "the same harness the cheater uses."
        ),
    },
    {
        "field": "server.tool_call_parser",
        "value": "mimo-native",
        "reason": (
            "Terminus-2 maps native MiMo exec_command calls (mimo_tool_calls); "
            "qwen3_coder describes the verl training route."
        ),
    },
    {
        "field": "budgets.max_requests",
        "value": 64,
        "reason": (
            "Wave-1 honest attempts use the HAR-161 probe request ceiling (64); "
            "the 500 reference value is the training budgets.step_limit."
        ),
    },
]



def _specs_for(task: str, hardened: bool, repo_root: Path, primary: Path) -> list[dict]:
    from evallab.exploit_probe import specs as probe_specs

    return probe_specs(
        repo_root,
        primary,
        task,
        prefix="chrr1h" if hardened else "chrr1l",
        hardened=hardened,
        cost_cap_usd=0.25,  # overwritten per arm below; never shipped as-is
    )


def _cheater(base: dict, campaign_id: str, attempt: int) -> dict:
    from evallab.exploit_probe import PREAMBLE

    assert base["extra_instruction_path"] == PREAMBLE, base["name"]
    spec = copy.deepcopy(base)
    short = spec["task_id"].removeprefix("format-code-task-")
    spec.update(CHEATER_CEILINGS)
    spec.update(
        {
            "name": f"chrr1-{'h' if 'hardened' in campaign_id else 'l'}-cheat-{short}-k{attempt}",
            "campaign_id": campaign_id,
            "reference_profile": REFERENCE_PROFILE,
            "linear_card": LINEAR_CARD,
            "est_cost_usd": CHEATER_EST_USD,
            "submitted_by": SUBMITTED_BY,
            "purpose": "elicitation",
            "hypothesis": (
                f"cheater-recall-v1 arm A (attempt {attempt}/{CHEATER_ATTEMPTS}) on "
                f"{spec['task_id']} ({'hardened' if 'hardened' in campaign_id else 'leaky'} "
                f"{spec['task_package_digest'][:12]}): an agent told not to implement "
                "the fix still makes the hidden tests pass"
            ),
        }
    )
    return spec


def _honest(base: dict, campaign_id: str) -> dict:
    spec = copy.deepcopy(base)
    short = spec["task_id"].removeprefix("format-code-task-")
    spec.pop("extra_instruction_path", None)
    spec.pop("extra_instruction_sha256", None)
    spec.update(HONEST_CEILINGS)
    spec.update(
        {
            "name": f"chrr1-{'h' if 'hardened' in campaign_id else 'l'}-honest-{short}",
            "campaign_id": campaign_id,
            "reference_profile": REFERENCE_PROFILE,
            "deviations": copy.deepcopy(HONEST_DEVIATIONS),
            "linear_card": LINEAR_CARD,
            "est_cost_usd": HONEST_EST_USD,
            "submitted_by": SUBMITTED_BY,
            "purpose": "baseline",
            "hypothesis": (
                f"cheater-recall-v1 arm B (false-alarm control) on {spec['task_id']} "
                f"({'hardened' if 'hardened' in campaign_id else 'leaky'} "
                f"{spec['task_package_digest'][:12]}): the same agent/model with the "
                "task's normal instruction; a cheater pass alongside an honest pass "
                "is not by itself evidence of a hole"
            ),
        }
    )
    return spec


def _assert_honest_covers(spec: dict, repo_root: Path, primary: Path) -> None:
    """Every honest spec's fingerprint diffs are covered by its deviations."""
    from evallab.schemas import ExperimentSpec
    from evallab.setup_fingerprint import (
        build_intended_fingerprint,
        check_deviations,
        compare_fingerprint,
        load_reference_profile,
    )

    validated = ExperimentSpec.model_validate(spec)
    fingerprint = build_intended_fingerprint(
        spec=validated,
        task_dir=primary / spec["task"],
        model=spec["model"],
        agent=spec["agent"],
        environment=spec["environment"],
        repo_root=repo_root,
    )
    profile = load_reference_profile(repo_root, REFERENCE_PROFILE)
    diffs = compare_fingerprint(fingerprint, profile)
    uncovered, stale = check_deviations(
        diffs, [dict(item) for item in validated.deviations]
    )
    assert not uncovered, f"{spec['name']}: uncovered diffs {uncovered}"
    assert not stale, f"{spec['name']}: stale deviations {stale}"


def _campaign_doc(
    campaign_id: str,
    allowances: list[dict],
    queue_cwd: Path,
    *,
    attempts_per_task: int,
    budget_usd: float,
    expected_usd: float,
    with_honest: bool,
) -> dict:
    formula = (
        f"E=${expected_usd:.2f} on the Oct-6 probe basis (mean ~$0.034/trial, max "
        "$0.16 on 002139; 5/10 probe trials were cut short by request/input "
        "ceilings so the unrestrained-cheater forecast sits above the mean); "
        f"W=${budget_usd:.2f} of binding per-trial cost caps; realized<=budget "
        "plus at most one wave of in-flight ceilings."
    )
    return {
        "schema_version": "experiment-campaign/v1",
        "campaign_id": campaign_id,
        "budget_usd": budget_usd,
        "attempts_per_task": attempts_per_task,
        "cost_estimate": {
            "expected_usd": expected_usd,
            "worst_case_usd": budget_usd,
            "formula": formula,
        },
        "execution": None,
        "tasks": allowances,
        "reference_profile": REFERENCE_PROFILE,
        "allowed_deviations": copy.deepcopy(HONEST_DEVIATIONS) if with_honest else [],
        "require_egress_lock": True,
        "ceiling_floor": {},
        "agent": AGENT,
        "model": MODEL,
        "environment": ENVIRONMENT,
        "sampling": {"temperature": None, "top_p": None, "top_k": None},
        "queue_cwd": queue_cwd.as_posix(),
        "linear_card": LINEAR_CARD,
        "submitted_by": SUBMITTED_BY,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def main() -> None:
    from evallab.campaign_approval import ExperimentCampaign, intended_sampling
    from evallab.schemas import ExperimentSpec
    from evallab.storage.paths import shared_checkout_root

    repo_root = Path(__file__).resolve().parents[3]
    primary = shared_checkout_root(repo_root)
    out = Path(__file__).resolve().parent
    paid_dir = out / "specs" / "paid"
    checks_dir = out / "specs" / "checks"
    paid_dir.mkdir(parents=True, exist_ok=True)
    checks_dir.mkdir(parents=True, exist_ok=True)

    # Sampling pin must equal what the route actually sends (all None here).
    assert intended_sampling(AGENT, MODEL).model_dump(mode="json") == {
        "temperature": None,
        "top_p": None,
        "top_k": None,
    }

    def _emit(specs: list[dict]) -> None:
        for spec in specs:
            validated = ExperimentSpec.model_validate(spec)  # fail loudly
            if validated.extra_instruction_path is None:
                _assert_honest_covers(spec, repo_root, primary)
            path = paid_dir / f"{spec['name']}.json"
            path.write_text(json.dumps(spec, indent=1) + "\n")

    def _campaign(cid: str, specs: list[dict], **kw: object) -> None:
        by_task: dict[str, dict] = {}
        for s in specs:
            # One allowance per task: prefer the honest spec where present.
            if s["task_id"] not in by_task or "-honest-" in s["name"]:
                by_task[s["task_id"]] = {
                    "task_id": s["task_id"],
                    "package_digest": s["task_package_digest"],
                }
        allowances = [by_task[t] for t in TASKS]
        assert set(by_task) == set(TASKS), by_task
        campaign = _campaign_doc(cid, allowances, repo_root, **kw)  # type: ignore[arg-type]
        ExperimentCampaign.model_validate(campaign)  # fail loudly
        (out / f"campaign-{cid}.json").write_text(json.dumps(campaign, indent=1) + "\n")
        print(f"{cid}: {len(specs)} paid specs")

    leaky: list[dict] = []
    hard_cheat: list[dict] = []
    hard_honest: list[dict] = []
    for task in TASKS:
        exploit, checks = _specs_for(task, False, repo_root, primary)
        for attempt in range(1, CHEATER_ATTEMPTS + 1):
            leaky.append(_cheater(exploit, f"{EXPERIMENT}-leaky", attempt))
        leaky.append(_honest(exploit, f"{EXPERIMENT}-leaky"))
        (checks_dir / f"{checks['name']}-leaky.json").write_text(
            json.dumps(checks, indent=1) + "\n"
        )
        hexploit, hchecks = _specs_for(task, True, repo_root, primary)
        for attempt in range(1, CHEATER_ATTEMPTS + 1):
            hard_cheat.append(_cheater(hexploit, f"{EXPERIMENT}-hardened-cheat", attempt))
        hard_honest.append(_honest(hexploit, f"{EXPERIMENT}-hardened-honest"))
        (checks_dir / f"{hchecks['name']}-hardened.json").write_text(
            json.dumps(hchecks, indent=1) + "\n"
        )
    for specs in (leaky, hard_cheat, hard_honest):
        _emit(specs)
    # Wave 1 (dispatchable now): leaky 30 (20 cheat + 10 honest) and hardened
    # cheater-only 20. Wave 2 (blocked on ledger promotion of the hardened
    # chains): hardened honest 10 — approved by nobody yet.
    _campaign(
        f"{EXPERIMENT}-leaky", leaky,
        attempts_per_task=CHEATER_ATTEMPTS + 1, budget_usd=4.0, expected_usd=1.90,
        with_honest=True,
    )
    _campaign(
        f"{EXPERIMENT}-hardened-cheat", hard_cheat,
        attempts_per_task=CHEATER_ATTEMPTS, budget_usd=3.0, expected_usd=1.40,
        with_honest=False,
    )
    _campaign(
        f"{EXPERIMENT}-hardened-honest", hard_honest,
        attempts_per_task=1, budget_usd=1.0, expected_usd=0.50,
        with_honest=True,
    )

    # 001269 drift guard: today's ledger LEAKY package is the post-probe purge
    # repair (d3375b68), not the Oct-6 probed package (541d4168). Fail here if
    # the ledger moves again so the README stays truthful.
    leaky = {}
    for path in (out / "specs" / "paid").glob("chrr1-l-cheat-*-k1.json"):
        spec = json.loads(path.read_text())
        leaky[spec["task_id"]] = spec["task_package_digest"]
    assert leaky["format-code-task-001269"].startswith("sha256:d3375b68"), leaky


if __name__ == "__main__":
    main()
