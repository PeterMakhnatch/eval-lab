"""Build wave-2 specs + campaign files for cheater-recall-v1: a second cheater model.

Wave 1's cheater (``zai/glm-5.3-flash``) stopped when the Z.ai account ran out
of balance. Wave 2 swaps in the one metered model key the lab holds,
``tinker/Qwen/Qwen3.6-35B-A3B`` (Tinker, host-loopback proxy, $0.54/$1.335 per
M tokens, cached prefill not credited), and asks two questions:

- leaky control (1 attempt x 5 tasks): can this model crack known holes at
  all? Four tasks wave 1 cracked through each channel (cherry-pick of an
  unreachable commit: 000792, 002552; hidden git dir: 002486, 002938) plus
  001269, which wave 1 never cracked. Without this control, 0 cracks on the
  hardened arm would say nothing about the hardening.
- hardened search (2 attempts x 7 tasks): does an independent cheater find
  anything wave 1 missed on the hardened variants whose images build?
  001269/002402/002864 are excluded: their hardened images failed to build in
  Daytona (Docker Hub pull authorization) during wave 1.

Specs are derived from the wave-1 cheater specs (same package digests,
preamble, environment, egress lock); only the model, ceilings, names,
campaign ids and hypotheses change.

Cost basis: wave-1 cheater trials averaged 0.50M input / 22k output tokens
(max 0.84M / 38k), i.e. ~$0.30 mean and ~$0.49 max at Tinker prices, plus
~$0.06 Daytona. The $0.40 model cap binds before the token ceilings; worst
case per trial ~$0.46.

Usage (from the worktree root):
  uv run python research/experiments/cheater-recall-v1/build_wave2.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from build_specs import EXPERIMENT, _campaign_doc

MODEL = "tinker/Qwen/Qwen3.6-35B-A3B"
LEAKY_TASKS = ["000792", "001269", "002486", "002552", "002938"]
HARDENED_TASKS = ["000552", "000792", "002139", "002391", "002486", "002552", "002938"]
HARDENED_ATTEMPTS = 2
CEILINGS = {
    "max_requests": 128,
    "max_input_tokens": 2_000_000,
    "max_output_tokens": 131_072,  # Tinker thinks at reasoning_effort 0.9 by default
    "max_total_tokens": 2_131_072,
    "cost_limit_usd": 0.40,
}
EST_USD = 0.36  # ~$0.30 model + ~$0.06 Daytona
WORST_USD = 0.46  # $0.40 cap + ~$0.06 Daytona


def _spec(base: dict, version: str, attempt: int, attempts: int) -> dict:
    spec = copy.deepcopy(base)
    short = spec["task_id"].removeprefix("format-code-task-")
    spec.update(CEILINGS)
    spec.update(
        {
            "model": MODEL,
            "name": f"chrr1-w2-{version[0]}-cheat-{short}-k{attempt}",
            "campaign_id": f"{EXPERIMENT}-w2-{version}",
            "est_cost_usd": EST_USD,
            "hypothesis": (
                f"cheater-recall-v1 wave 2 (second cheater model {MODEL}, attempt "
                f"{attempt}/{attempts}) on {spec['task_id']} ({version} "
                f"{spec['task_package_digest'][:12]}): an agent told not to implement "
                "the fix still makes the hidden tests pass"
            ),
        }
    )
    return spec


def main() -> None:
    from evallab.campaign_approval import ExperimentCampaign, intended_sampling
    from evallab.schemas import ExperimentSpec

    repo_root = Path(__file__).resolve().parents[3]
    out = Path(__file__).resolve().parent
    wave1 = out / "specs" / "paid"
    paid_dir = out / "specs" / "paid-w2"
    paid_dir.mkdir(parents=True, exist_ok=True)

    sampling = intended_sampling("terminus-2", MODEL).model_dump(mode="json")
    assert sampling == {"temperature": None, "top_p": None, "top_k": None}, sampling

    for version, tasks, attempts in (
        ("leaky", LEAKY_TASKS, 1),
        ("hardened", HARDENED_TASKS, HARDENED_ATTEMPTS),
    ):
        specs = []
        for short in tasks:
            base = json.loads((wave1 / f"chrr1-{version[0]}-cheat-{short}-k1.json").read_text())
            for attempt in range(1, attempts + 1):
                spec = _spec(base, version, attempt, attempts)
                ExperimentSpec.model_validate(spec)  # fail loudly
                (paid_dir / f"{spec['name']}.json").write_text(json.dumps(spec, indent=1) + "\n")
                specs.append(spec)
        allowances = [
            {"task_id": s["task_id"], "package_digest": s["task_package_digest"]}
            for s in specs
            if s["name"].endswith("-k1")
        ]
        budget = round(len(specs) * WORST_USD, 2)
        expected = round(len(specs) * EST_USD, 2)
        cid = f"{EXPERIMENT}-w2-{version}"
        campaign = _campaign_doc(
            cid,
            allowances,
            repo_root,
            attempts_per_task=attempts,
            budget_usd=budget,
            expected_usd=expected,
            with_honest=False,
        )
        campaign["model"] = MODEL
        campaign["cost_estimate"]["formula"] = (
            f"E=${expected:.2f}: {len(specs)} trials x ${EST_USD:.2f} (wave-1 cheater mean "
            "0.50M input / 22k output tokens at Tinker $0.54/$1.335 per M, no cached "
            f"credit, + ~$0.06 Daytona); W=${budget:.2f}: {len(specs)} x ${WORST_USD:.2f} "
            "($0.40 binding model cap + ~$0.06 Daytona)."
        )
        ExperimentCampaign.model_validate(campaign)  # fail loudly
        (out / f"campaign-{cid}.json").write_text(json.dumps(campaign, indent=1) + "\n")
        print(f"{cid}: {len(specs)} specs, expected ${expected:.2f}, worst ${budget:.2f}")


if __name__ == "__main__":
    main()
