"""Integrity-gated verifier reward for MiMo tasks (HAR-169, Data lane).

A task transform variant (``rewardkit-integrity@1``) built through
``evallab.task_variants.derive_task``: the task's own grading stays
byte-identical, then integrity criteria score three RewardKit dims written
to ``/logs/verifier/reward.json`` (Harbor prefers ``reward.json`` over
``reward.txt`` when both exist, so all three dims flow into the trial's
rewards)::

    reward         raw hidden-test result, read from verifier/reward.txt
    integrity      1 clean / 0 when any deterministic rule fired
    reward_gated   reward x integrity (exact product, also for non-binary raw)

``tests/`` layout inside the variant (genuine RewardKit: programmatic
``@criterion`` files plus ``reward.toml`` aggregation; ``rewardkit /tests``
discovers exactly the three dims)::

    tests/test.sh                 original grading, then rewardkit-or-fallback
    tests/reward/raw_reward.py    -> dim ``reward``
    tests/integrity/*.py          -> dim ``integrity`` (all-pass over 4 rules)
    tests/integrity/reward.toml   ``[[reward]] aggregation = "all-pass"``
    tests/reward_gated/gated.py   -> dim ``reward_gated``
    tests/vendor/                 stdlib-only shared logic + fallback runner

Offline / in-image strategy (no network at grade time, egress lock safe):

* RewardKit itself cannot run in most task images. Evidence: stored trials
  show image interpreters 3.7 (``/usr/local/lib/python3.7/site-packages``
  tracebacks in ``HAR-126-ovn-g5-001695-stock``), 3.9 (``.../python3.9/...``
  diffs in ``HAR-116-har116-b-002308-original``), 3.10
  (``HAR-126-ovn-g5-001809-stock``), and 3.12
  (``HAR-120-har120-002356-a1-r2``), while RewardKit requires Python
  >=3.12 plus ``litellm``/``pydantic`` — uninstallable offline (the
  example's ``uvx --from harbor-rewardkit`` needs network; vendoring
  native wheels per interpreter is fragile).
* So the variant ships both paths sharing one stdlib-only core
  (a standalone ``evallab.integrity_reward_core`` snapshot under
  ``tests/vendor/``): ``test.sh`` runs ``python -m rewardkit /tests``
  when importable, else ``tests/vendor/run_integrity.py``, which writes
  RewardKit-compatible ``reward.json`` + ``reward-details.json``. Scoring
  never fails grading (``|| true``; with no ``reward.txt`` nothing is
  written and Harbor falls back to ``reward.txt`` as before).
* Rule ``copy_check_v1`` ships a standalone snapshot of the copy detector.
  Tests exercise scoring and the offline payload, not source-byte identity;
  host-side performance changes need not rewrite existing verifier payloads.

Host replay (``research/experiments/har169-integrity/replay.py``) scores
stored trials through :func:`evaluate_trial` with zero runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evallab import integrity_reward_core as core
from evallab.task_variants import VariantRecord, derive_task

#: Transform identity for the integrity-reward variant. Registered by use:
#: ``derive_variant`` derives through ``evallab.task_variants.derive_task``
#: and writes the lineage record under
#: ``library/task-variants/<slug>/<digest12>.json`` (no central transform
#: registry exists; records are the registry).
TRANSFORM = "rewardkit-integrity@1"

PAYLOAD_DIR = Path(__file__).resolve().parent / "integrity_reward_files"
#: ``tests/`` additions, as ``(payload name, tests/ relpath)``. ``test.sh``
#: is handled separately (append, never replace: per-task reset lists differ).
PAYLOAD_MAP = (
    ("vendor/integrity_core.py", "tests/vendor/integrity_core.py"),
    ("vendor/copy_check_vendored.py", "tests/vendor/copy_check_vendored.py"),
    ("vendor/run_integrity.py", "tests/vendor/run_integrity.py"),
    ("reward/raw_reward.py", "tests/reward/raw_reward.py"),
    ("integrity/copy_check_v1.py", "tests/integrity/copy_check_v1.py"),
    ("integrity/upstream_fetch.py", "tests/integrity/upstream_fetch.py"),
    ("integrity/out_of_base_read.py", "tests/integrity/out_of_base_read.py"),
    ("integrity/grader_tamper.py", "tests/integrity/grader_tamper.py"),
    ("integrity/reward.toml", "tests/integrity/reward.toml"),
    ("reward_gated/gated.py", "tests/reward_gated/gated.py"),
)

RULE_IDS = core.RULE_IDS


def payload_bytes(name: str) -> bytes:
    """Raw bytes of one payload file (read from the source tree, never generated)."""
    return (PAYLOAD_DIR / name).read_bytes()


def build_test_sh(parent_test_sh: bytes) -> bytes:
    """Parent ``tests/test.sh`` plus the scoring tail (original grading untouched)."""
    tail = payload_bytes("test_sh_tail.sh")
    text = parent_test_sh.decode("utf-8")
    if not text.endswith("\n"):
        text += "\n"
    return (text + tail.decode("utf-8")).encode("utf-8")


def variant_changes(parent_dir: Path | str) -> dict[str, bytes | None]:
    """Full ``derive_task`` change-set for the integrity variant of one task."""
    parent = Path(parent_dir)
    test_sh = parent / "tests" / "test.sh"
    if not test_sh.is_file():
        raise ValueError(f"parent has no tests/test.sh: {parent}")
    changes: dict[str, bytes | None] = {rel: payload_bytes(name) for name, rel in PAYLOAD_MAP}
    changes["tests/test.sh"] = build_test_sh(test_sh.read_bytes())
    return changes


def derive_variant(
    parent_dir: Path | str,
    *,
    created_by: str,
    parent_source: dict[str, Any],
    rationale: str = (
        "HAR-169 integrity-gated verifier reward: keep the task's own grading, "
        "then score reward/integrity/reward_gated into verifier/reward.json "
        "(RewardKit criteria with a stdlib-only offline fallback)."
    ),
    inputs: dict[str, Any] | None = None,
    repo_root: Path | str | None = None,
    records_dir: Path | str | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``rewardkit-integrity@1`` variant of one task package."""
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if records_dir is not None:
        kwargs["records_dir"] = records_dir
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent_dir,
        changes=variant_changes(parent_dir),
        transform=TRANSFORM,
        rationale=rationale,
        created_by=created_by,
        inputs={"transform": TRANSFORM, **(inputs or {})},
        parent_source=parent_source,
        **kwargs,
    )


def evaluate_trial(trial_dir: Path | str) -> dict[str, Any]:
    """Score one stored trial dir (host replay; in-image the criteria do this)."""
    return core.evaluate(trial_dir=trial_dir)


__all__ = [
    "PAYLOAD_DIR",
    "PAYLOAD_MAP",
    "RULE_IDS",
    "TRANSFORM",
    "build_test_sh",
    "derive_variant",
    "evaluate_trial",
    "payload_bytes",
    "variant_changes",
]
