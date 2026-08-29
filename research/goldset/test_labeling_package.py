"""Regression tests for the five review blockers on the gold-set package.

Each test names the blocker it guards. Run:
    python3 research/goldset/test_labeling_package.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_labeling_package import (  # noqa: E402
    ALLOWED_VALUES,
    CANNOT_JUDGE,
    HUMAN_JUDGED_FIELDS,
    INSUFFICIENT_CONTEXT,
    MIN_EFFECTIVE_CLUSTERS,
    RATING_SCHEMA_VERSION,
    REQUIRED_RATERS_PER_ITEM,
    LabelItem,
    build_package,
    effective_clusters,
    evaluate_cluster_adequacy,
    evaluate_readiness,
    validate_rating,
)

EXPECTED_ITEMS = 183
EXPECTED_CLUSTERS = 20
EXPECTED_DIGEST = "4790e490d09fa84882f012840df65302bd1ff5ff65f8df6126c07181181895e8"


def _synthetic_runs(root: Path) -> Path:
    """Two trajectories, one duplicated at a second path, for dedup+determinism."""
    runs = root / "runs"
    doc_a = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "codex", "model_name": "test-model"},
        "steps": [
            {"source": "user", "message": "do the thing"},
            {
                "source": "agent",
                "message": "",
                "tool_calls": [{"function_name": "exec", "arguments": {"cmd": "ls"}}],
                "observation": {"results": [{"content": "a.txt"}]},
            },
            {
                "source": "agent",
                "message": "again",
                "tool_calls": [{"function_name": "exec", "arguments": {"cmd": "ls"}}],
                "observation": {"results": [{"content": "a.txt"}]},
            },
        ],
    }
    doc_b = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "codex", "model_name": "test-model"},
        "steps": [
            {"source": "user", "message": "other task"},
            {"source": "agent", "message": "thinking", "tool_calls": []},
        ],
    }
    for rel, doc in (
        ("campaign/trial-a/agent/trajectory.json", doc_a),
        ("trial-a/agent/trajectory.json", doc_a),  # byte-identical duplicate
        ("campaign/trial-b/agent/trajectory.json", doc_b),
    ):
        path = runs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return runs


# NOTE: runs/ is gitignored and absent from worktrees and CI, so the determinism
# test MUST NOT depend on it. Revision 2 used parents[3]/runs - outside the repo -
# so both builds produced an EMPTY package and the test passed vacuously.
# Determinism is now proven against a synthetic fixture; the committed package is
# asserted separately by item count and digest.
PACKAGE = Path(__file__).parent / "labeling_package.json"
TRUTH = Path(__file__).parent / "machine_truth_WITHHELD.json"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def main() -> int:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    items = package["items"]

    print("B1 - rater context is labelable")
    check(
        "every item carries a presumed task statement",
        all(
            i["rater_context"]["instruction"]["presumed_task_statement"] is not None for i in items
        ),
    )
    check(
        "prior steps carry message, tool_calls and observation keys",
        all(
            all(k in s for k in ("message", "tool_calls", "observation"))
            for i in items
            for s in i["rater_context"]["prior_steps"]
        ),
    )
    check(
        "items with empty messages still expose tool_calls or observation",
        all(
            bool(i["rater_context"]["item_step"]["tool_calls"])
            or bool(i["rater_context"]["item_step"]["observation"])
            for i in items
            if i["rater_context"]["item_step"]["message_is_empty"]
        ),
    )

    print("B2 - machine truth is withheld from raters")
    blob = json.dumps(items)
    check("'prior_error_visible' absent from rater-facing items", "prior_error_visible" not in blob)
    check("'machine_facts' absent from rater-facing items", "machine_facts" not in blob)
    check(
        "no item exposes a boolean repeats_prior_action fact",
        not any("repeats_prior_action" in json.dumps(i["rater_context"]) for i in items),
    )
    check("machine truth artifact is non-empty", len(truth["truths"]) == len(items))

    print("B3 - content-addressed dedup")
    check(
        "item_id is unique",
        len({i["item_id"] for i in items}) == len(items),
    )
    check(
        "no two items share (source_sha256, step_index)",
        len({(i["source_sha256"], i["step_index"]) for i in items}) == len(items),
    )
    check(
        "duplicate paths were dropped",
        package["census"]["duplicate_paths_dropped"] == 3,
        f"got {package['census']['duplicate_paths_dropped']}",
    )
    check(
        "unique agent steps is 183 not 237",
        package["census"]["agent_steps_unique"] == 183,
        f"got {package['census']['agent_steps_unique']}",
    )
    check(
        "aliases recorded for duplicated content",
        any(len(i["source_aliases"]) > 1 for i in items),
    )
    check(
        "cluster count is below item count",
        package["census"]["clusters_with_agent_steps"] < len(items),
    )

    print("B4 - immutable items, sidecar ratings, validated labels")
    check("no item carries a ratings field", not any("ratings" in i for i in items))
    check(
        "LabelItem has no rating attribute",
        not any("rating" in f for f in LabelItem.__dataclass_fields__),
    )
    check("readiness is NOT_READY", package["readiness"]["readiness"] == "NOT_READY")

    # 40 items in 40 distinct clusters: K_eff = 40, concentration 2.5% - clears the
    # cluster gate, so these assertions isolate the RATER gate.
    fake = [
        LabelItem(
            item_id=f"item{n:03d}",
            source_sha256=f"{n:064d}",
            step_index=0,
            source_aliases=("x",),
            model_name=None,
            agent_name=None,
            stratum="tool:first",
            sampling_weight=1.0,
            selection_arm="prevalence_core",
            cluster_id=f"{n:064d}",
            context_completeness={"builder_verdict": "COMPLETE"},
            rater_context={},
        )
        for n in range(40)
    ]
    good = {
        "schema_version": RATING_SCHEMA_VERSION,
        "item_id": "item000",
        "rater_id": "r1",
        **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
    }
    check("valid record passes validation", validate_rating(good) == [])
    check(
        "missing label is rejected",
        any(
            e.startswith("MISSING_LABEL")
            for e in validate_rating({**good, "step_contribution": None})
        ),
    )
    check(
        "out-of-enum label is rejected",
        any(
            e.startswith("OUT_OF_ENUM")
            for e in validate_rating({**good, "step_contribution": "GREAT"})
        ),
    )
    three = [{**good, "item_id": i.item_id, "rater_id": f"r{n}"} for i in fake for n in range(1, 4)]
    check(
        "three unique qualified raters with valid labels clears readiness",
        evaluate_readiness(fake, three, ["r1", "r2", "r3"])["readiness"] == "READY",
    )
    check(
        "same rater three times does NOT clear readiness",
        evaluate_readiness(fake, [{**good, "rater_id": "r1"}] * 3, ["r1", "r2", "r3"])["readiness"]
        == "NOT_READY",
    )
    check(
        "unqualified rater does NOT clear readiness",
        evaluate_readiness(fake, three, ["r1", "r2", "rX"])["readiness"] == "NOT_READY",
    )
    check(
        "rater IDs present but labels null does NOT clear readiness",
        evaluate_readiness(
            fake,
            [
                {**good, "item_id": i.item_id, "rater_id": f"r{n}", "abstention": None}
                for i in fake
                for n in range(1, 4)
            ],
            ["r1", "r2", "r3"],
        )["readiness"]
        == "NOT_READY",
    )

    print("B5 - CANNOT_JUDGE and INSUFFICIENT_CONTEXT on every human-judged field")
    for field_name in HUMAN_JUDGED_FIELDS:
        check(
            f"{field_name} offers CANNOT_JUDGE",
            CANNOT_JUDGE in ALLOWED_VALUES[field_name],
        )
        check(
            f"{field_name} offers INSUFFICIENT_CONTEXT",
            INSUFFICIENT_CONTEXT in ALLOWED_VALUES[field_name],
        )
    check(
        "CANNOT_JUDGE and INSUFFICIENT_CONTEXT are distinct values",
        CANNOT_JUDGE != INSUFFICIENT_CONTEXT,
    )
    check(
        "missing_data_semantics documents the distinction",
        CANNOT_JUDGE in package["taxonomy"]["missing_data_semantics"]
        and INSUFFICIENT_CONTEXT in package["taxonomy"]["missing_data_semantics"],
    )

    print("alias manifest and builder-declared completeness")
    manifest = package["census"]["alias_manifest"]
    check(
        "manifest covers every distinct content digest",
        len(manifest) == package["census"]["distinct_content_digests"],
    )
    check(
        "manifest duplicate_count sums to duplicate_paths_dropped",
        sum(e["duplicate_count"] for e in manifest.values())
        == package["census"]["duplicate_paths_dropped"],
    )
    check(
        "every item's source_sha256 appears in the manifest",
        all(i["source_sha256"] in manifest for i in items),
    )
    check(
        "every item declares a builder completeness verdict",
        all(
            i["context_completeness"]["builder_verdict"] in ("COMPLETE", "DEGRADED") for i in items
        ),
    )
    check(
        "completeness census is reported",
        "context_completeness" in package["census"],
    )

    print("committed package is pinned")
    check(
        f"item count is {EXPECTED_ITEMS}",
        len(items) == EXPECTED_ITEMS,
        f"got {len(items)}",
    )
    check(
        f"cluster count is {EXPECTED_CLUSTERS}",
        package["census"]["clusters_with_agent_steps"] == EXPECTED_CLUSTERS,
        f"got {package['census']['clusters_with_agent_steps']}",
    )
    actual_digest = hashlib.sha256(PACKAGE.read_bytes()).hexdigest()
    check(
        "package digest matches the pinned value",
        actual_digest == EXPECTED_DIGEST,
        f"got {actual_digest}",
    )

    print("determinism - synthetic fixture, does NOT depend on gitignored runs/")
    with tempfile.TemporaryDirectory() as tmp:
        runs = _synthetic_runs(Path(tmp))
        a, ta = build_package(runs, core_n=None, boost_per_stratum=3, ratings_dir=None)
        b, tb = build_package(runs, core_n=None, boost_per_stratum=3, ratings_dir=None)
        check("fixture yields a NON-EMPTY package", a["n_selected"] > 0, f"got {a['n_selected']}")
        check(
            "two builds are byte-identical",
            json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True),
        )
        check(
            "machine truth is byte-identical too",
            json.dumps(ta, sort_keys=True) == json.dumps(tb, sort_keys=True),
        )
        check(
            "fixture duplicate was deduplicated",
            a["census"]["duplicate_paths_dropped"] == 1,
            f"got {a['census']['duplicate_paths_dropped']}",
        )
        check(
            "fixture aliases record both paths",
            any(len(i["source_aliases"]) == 2 for i in a["items"]),
        )

    print("withdrawn machine truth - no invalid error fact")
    check(
        "prior_error_visible absent from machine truth rows",
        all("prior_error_visible" not in row for row in truth["truths"]),
    )
    check(
        "machine truth declares the error fact unavailable",
        all(row["prior_error_truth_available"] is False for row in truth["truths"]),
    )
    check(
        "withdrawal is documented in the package",
        "prior_error_visible" in package["blinding"]["withdrawn_fields"],
    )

    print("cluster adequacy gate - Tutor power verdict")
    check("K_eff formula is Kish", abs(effective_clusters([2, 2]) - 2.0) < 1e-9)
    check(
        "single dominant cluster collapses K_eff",
        effective_clusters([100, 1, 1]) < 2.0,
    )
    adequacy = package["readiness"]["cluster_adequacy"]
    check(
        "package reports K_eff = 13.33",
        abs(adequacy["effective_clusters_kish"] - 13.33) < 0.01,
        f"got {adequacy['effective_clusters_kish']}",
    )
    check(
        "K_eff below floor is a readiness blocker",
        any("EFFECTIVE_CLUSTERS_BELOW_FLOOR" in b for b in package["readiness"]["blockers"]),
    )
    check(
        "concentration above target is a readiness blocker",
        any("CLUSTER_CONCENTRATION_TOO_HIGH" in b for b in package["readiness"]["blockers"]),
    )
    check(
        "an adequate balanced design would clear the cluster gate",
        evaluate_cluster_adequacy([5] * 40)["blockers"] == [],
    )
    check(
        f"floor is {MIN_EFFECTIVE_CLUSTERS}",
        MIN_EFFECTIVE_CLUSTERS == 20.0,
    )
    check("required raters per item is 3", REQUIRED_RATERS_PER_ITEM == 3)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
