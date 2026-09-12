"""TB4 html-js-filter canary pair compiler: mini-swe-agent vs DeepSeek harness agent.

Emits a paired :class:`ExperimentSpec` set for one TB4 task (one canary pair,
not a ranking): a baseline arm on ``mini-swe-agent`` and a candidate arm on
``evallab.harbor_dsh:DeepSeekHarnessAgent`` (referenced as a string only; this
module never imports it). Both arms share task identity and differ only in
agent.

Fail-closed by design: TB4 tasks live outside the repo and are not registered
in ``library/registry/``, so no repo-relative ``task_path`` can be embedded in
the specs. The compiler still writes the frozen documents (two spec JSON files
plus ``pair-metadata.json``) and then refuses with a clear ``ValueError``
naming the gap. ``Integration`` submits only after Peter's approval, once the
task is registered or vendored.

Fields ``ExperimentSpec`` cannot express (declared_variable, mode,
per-arm agent kwargs such as reasoning_effort, approval terms) live in the
sibling ``pair-metadata.json`` — never as invented schema fields.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR
from evallab.schemas import ExperimentSpec

PAIR_DIR = Path(__file__).resolve().parent
PAIR_DEF_PATH = PAIR_DIR / "pair.json"
REPO_ROOT = PAIR_DIR.parents[2]

#: Candidate-arm agent coordinate (lands via PR #397). String only.
DSH_AGENT_IMPORT_PATH = "evallab.harbor_dsh:DeepSeekHarnessAgent"

BASELINE_AGENT_NAME = "mini-swe-agent"

CANARY_STAGE = "canary"

REGISTRATION_GAP = (
    "TB4 tasks are not registered in library/registry, and the TB4 task checkout "
    "lives outside the repo, so no repo-relative task_path can be embedded in the "
    "spec. Nothing in this pair copies TB4 task content. Register (or vendor) the "
    "task and re-run; Integration submits only after Peter approval."
)


def load_pair_def(path: Path = PAIR_DEF_PATH) -> dict[str, Any]:
    """Load the frozen pair definition; refuse if its model pin drifted."""
    pair_def = json.loads(path.read_text())
    frozen = pair_def.get("model_selector")
    if frozen != DEEPSEEK_MODEL_SELECTOR:
        raise ValueError(
            f"pair definition pins model {frozen!r} but the lab selector is "
            f"{DEEPSEEK_MODEL_SELECTOR!r}; update pair.json, do not work around it"
        )
    return pair_def


def build_pair_specs(
    pair_def: dict[str, Any] | None = None,
) -> tuple[ExperimentSpec, ExperimentSpec]:
    """Build the baseline and DSH specs: shared task identity, agent differs."""
    pair_def = pair_def if pair_def is not None else load_pair_def()
    task_ref = pair_def["task_ref"]
    timeout = pair_def["agent_timeout_seconds"]
    attempts = pair_def["attempts"]
    arms = {arm["arm"]: arm for arm in pair_def["arms"]}

    common = {
        "purpose": "comparison",
        "task": task_ref,
        "model": DEEPSEEK_MODEL_SELECTOR,
        "environment": "docker",
        "attempts": attempts,
        "concurrency": 1,
        "timeout_seconds": timeout,
        "submitted_by": "integration",
    }
    baseline = ExperimentSpec(
        name="tb4-dsh-pair-html-js-filter-baseline",
        hypothesis=(
            "On TB4 html-js-filter with model deepseek/deepseek-v4-flash held fixed, "
            "one mini-swe-agent attempt is the baseline half of an exploratory "
            "canary pair; n=1 is not a ranking."
        ),
        agent=arms["baseline"]["agent"],
        **common,
    )
    dsh = ExperimentSpec(
        name="tb4-dsh-pair-html-js-filter-dsh",
        hypothesis=(
            "On TB4 html-js-filter with model deepseek/deepseek-v4-flash held fixed, one "
            "DeepSeekHarnessAgent attempt (reasoning_effort=max) is the candidate "
            "half of an exploratory canary pair; n=1 is not a ranking."
        ),
        agent=arms["dsh"]["agent"],
        **common,
    )
    if baseline.agent == BASELINE_AGENT_NAME and dsh.agent != DSH_AGENT_IMPORT_PATH:
        raise ValueError("pair definition swapped the arm agents")
    return baseline, dsh


def write_pair_files(
    out_dir: Path | str,
    pair_def: dict[str, Any] | None = None,
    specs: tuple[ExperimentSpec, ExperimentSpec] | None = None,
) -> dict[str, Path]:
    """Write the two spec JSON files plus pair-metadata.json into out_dir."""
    pair_def = pair_def if pair_def is not None else load_pair_def()
    specs = specs if specs is not None else build_pair_specs(pair_def)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for spec in specs:
        path = out / f"{spec.name}.json"
        path.write_text(spec.model_dump_json(indent=2) + "\n")
        paths[spec.name] = path
    metadata = {
        **pair_def,
        "model_resolved": DEEPSEEK_MODEL_SELECTOR,
        "spec_files": [f"{spec.name}.json" for spec in specs],
        "registration_gap": REGISTRATION_GAP,
        "submittable": False,
    }
    metadata_path = out / "pair-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    paths["pair-metadata"] = metadata_path
    return paths


def assert_task_submittable(spec: ExperimentSpec, *, repo_root: Path = REPO_ROOT) -> None:
    """Refuse specs whose task cannot be executed from inside the repo.

    A ``registered/`` task must resolve in ``library/registry/``; anything else
    needs a ``task_path`` pointing at a real repo-relative directory. TB4 tasks
    satisfy neither, so this raises a ``ValueError`` naming the gap.
    """
    if spec.task.startswith("registered/"):
        from evallab.registry import TaskNotRegisteredError, TaskRegistry

        try:
            TaskRegistry.from_repo(repo_root).resolve_spec(spec, repo_root)
        except TaskNotRegisteredError as exc:
            raise ValueError(
                f"refusing pair compile: task {spec.task!r} is not registered in "
                f"library/registry ({exc}); {REGISTRATION_GAP}"
            ) from exc
        return
    if spec.task_path is not None and (repo_root / spec.task_path).is_dir():
        return
    raise ValueError(
        f"refusing pair compile: task {spec.task!r} is not registered in "
        f"library/registry and carries no repo-relative task_path "
        f"(task_path={spec.task_path!r}); {REGISTRATION_GAP}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, help="only 'canary' is supported")
    parser.add_argument("--out", required=True, help="output directory for pair files")
    args = parser.parse_args(argv)
    if args.stage != CANARY_STAGE:
        raise ValueError(
            f"unsupported stage {args.stage!r}: this compiler emits only the "
            f"{CANARY_STAGE!r} pair for terminal-bench/html-js-filter"
        )
    pair_def = load_pair_def()
    specs = build_pair_specs(pair_def)
    write_pair_files(args.out, pair_def, specs)
    # Documents are frozen above; the pair is still not runnable, so fail closed.
    for spec in specs:
        assert_task_submittable(spec)
    return 0


if __name__ == "__main__":
    main()
