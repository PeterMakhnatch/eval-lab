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

from build_labeling_package import (  # noqa: E402  # noqa: E402
    ALLOWED_VALUES,
    CANNOT_JUDGE,
    HUMAN_JUDGED_FIELDS,
    INSUFFICIENT_CONTEXT,
    MIN_EFFECTIVE_CLUSTERS,
    RATING_SCHEMA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    REQUIRED_RATERS_PER_ITEM,
    BundleContaminationError,
    LabelItem,
    OutputPathError,
    PairMismatchError,
    SourceRejectedError,
    build_package,
    compute_build_id,
    effective_clusters,
    evaluate_cluster_adequacy,
    evaluate_readiness,
    export_rater_bundle,
    load_paired_artifacts,
    load_rater_registry,
    logical_trial_digest,
    read_source_once,
    sign_rating,
    sign_registry,
    validate_rating,
    write_paired_outputs,
)

EXPECTED_ITEMS = 183
EXPECTED_CLUSTERS = 20
EXPECTED_DIGEST = "5e7fef50980e852abfc7570b3da9b863b2b3e50e1958b98eec84c223792467c6"


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
            logical_step_digest=f"{n:064x}",
            context_completeness={"builder_verdict": "COMPLETE"},
            rater_context={},
        )
        for n in range(40)
    ]
    b4_keyring = {"r1": "k1", "r2": "k2", "r3": "k3"}

    def _signed(item_id: str, key_id: str, **over: object) -> dict:
        rec = {
            "schema_version": RATING_SCHEMA_VERSION,
            "package_digest": "pkg",
            "item_id": item_id,
            "item_digest": None,
            "rater_key_id": key_id,
            **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            **over,
        }
        rec["signature"] = sign_rating(rec, b4_keyring[key_id])
        return rec

    def _readiness(recs: list[dict]) -> str:
        return evaluate_readiness(
            fake,
            recs,
            list(b4_keyring),
            package_digest="pkg",
            keyring=b4_keyring,
        )["readiness"]

    good = _signed("item000", "r1", item_digest=f"{0:064x}")
    check(
        "valid record passes validation",
        validate_rating(
            good,
            package_digest="pkg",
            item_digests={"item000": f"{0:064x}"},
            keyring=b4_keyring,
        )
        == [],
    )
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
    three = [
        _signed(i.item_id, f"r{n}", item_digest=i.logical_step_digest)
        for i in fake
        for n in range(1, 4)
    ]
    check(
        "three unique qualified raters with valid labels clears readiness",
        _readiness(three) == "READY",
    )
    check(
        "same rater three times does NOT clear readiness",
        _readiness(
            [
                _signed(i.item_id, "r1", item_digest=i.logical_step_digest)
                for i in fake
                for _ in range(3)
            ]
        )
        == "NOT_READY",
    )
    check(
        "unqualified rater does NOT clear readiness",
        evaluate_readiness(
            fake, three, ["r1", "r2", "rX"], package_digest="pkg", keyring=b4_keyring
        )["readiness"]
        == "NOT_READY",
    )
    check(
        "rater key IDs present but labels null does NOT clear readiness",
        _readiness(
            [
                _signed(
                    i.item_id,
                    f"r{n}",
                    item_digest=i.logical_step_digest,
                    abstention=None,
                )
                for i in fake
                for n in range(1, 4)
            ]
        )
        == "NOT_READY",
    )
    check(
        "byte-identical resubmission FAILS as a duplicate, not set-collapsed",
        any(
            "DUPLICATE_SUBMISSIONS" in b
            for b in evaluate_readiness(
                fake,
                three + three,
                list(b4_keyring),
                package_digest="pkg",
                keyring=b4_keyring,
            )["blockers"]
        ),
    )
    check(
        "differing resubmission FAILS as a conflict",
        any(
            "CONFLICTING_SUBMISSIONS" in b
            for b in evaluate_readiness(
                fake,
                three
                + [
                    _signed(
                        i.item_id,
                        "r1",
                        item_digest=i.logical_step_digest,
                        step_contribution="HARMFUL",
                    )
                    for i in fake
                ],
                list(b4_keyring),
                package_digest="pkg",
                keyring=b4_keyring,
            )["blockers"]
        ),
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
            i["context_completeness"]["builder_verdict"] in ("COMPLETE", "INCOMPLETE")
            for i in items
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

    print("SEC1 - rater bundle isolation")
    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp) / "bundle"
        out = export_rater_bundle(package, bundle_dir)
        bundle = json.loads(out.read_text(encoding="utf-8"))
        check(
            "bundle omits the attention-check identity",
            "attention_check_field" not in bundle["taxonomy"],
        )
        check("bundle carries no truths key", "truths" not in bundle)
        check(
            "bundle binds package_digest",
            bundle["package_digest"] == package["package_digest"],
        )
        check(
            "every bundle item binds an item_digest",
            all("item_digest" in i for i in bundle["items"]),
        )
        check(
            "bundle exposes no machine-truth prose",
            "note" not in bundle["taxonomy"].get("missing_data_semantics", {}),
        )
        # contamination must be refused
        (bundle_dir / "machine_truth_LEAK.json").write_text("{}", encoding="utf-8")
        try:
            export_rater_bundle(package, bundle_dir)
            check("contaminated bundle dir is refused", False, "no raise")
        except BundleContaminationError:
            check("contaminated bundle dir is refused", True)

    print("SEC2 - logical clone digest")
    a_steps = [{"source": "agent", "message": "hi", "tool_calls": [], "observation": {}}]
    b_steps = [
        {
            "source": "agent",
            "message": " hi  ",
            "tool_calls": [],
            "observation": {},
            "timestamp": "2026-01-01T00:00:00Z",
            "step_id": "xyz",
        }
    ]
    check(
        "whitespace/metadata clones share a logical digest",
        logical_trial_digest(a_steps) == logical_trial_digest(b_steps),
    )
    check(
        "different content yields a different logical digest",
        logical_trial_digest(a_steps)
        != logical_trial_digest([{**a_steps[0], "message": "different"}]),
    )
    check(
        "cluster_id is the logical digest, not the raw sha",
        all(i["cluster_id"] != i["source_sha256"] for i in items),
    )

    print("SEC3 - rating authentication")
    keyring = {"key-1": "s3cret", "key-2": "s3cret2", "key-3": "s3cret3"}
    base = {
        "schema_version": RATING_SCHEMA_VERSION,
        "package_digest": "pkgdigest",
        "item_id": "item000",
        "item_digest": f"{0:064x}",
        "rater_key_id": "key-1",
        **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
    }
    signed = {**base, "signature": sign_rating(base, keyring["key-1"])}
    digests = {"item000": f"{0:064x}"}
    check(
        "correctly signed record validates",
        validate_rating(signed, package_digest="pkgdigest", item_digests=digests, keyring=keyring)
        == [],
    )
    check(
        "self-asserted record with no signature is rejected",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(base, package_digest="pkgdigest", item_digests=digests, keyring=keyring),
    )
    check(
        "unregistered key is rejected",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(
            {**signed, "rater_key_id": "rogue"},
            package_digest="pkgdigest",
            item_digests=digests,
            keyring=keyring,
        ),
    )
    check(
        "package_digest mismatch is rejected",
        "PACKAGE_DIGEST_MISMATCH"
        in validate_rating(signed, package_digest="OTHER", item_digests=digests, keyring=keyring),
    )
    check(
        "item_digest mismatch is rejected",
        "ITEM_DIGEST_MISMATCH"
        in validate_rating(
            {**signed, "item_digest": "f" * 64},
            package_digest="pkgdigest",
            item_digests=digests,
            keyring=keyring,
        ),
    )
    check(
        "tampering with a label invalidates the signature",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(
            {**signed, "step_contribution": "HARMFUL"},
            package_digest="pkgdigest",
            item_digests=digests,
            keyring=keyring,
        ),
    )

    print("SEC5 - TOCTOU, symlink escape, paired atomic output")
    with tempfile.TemporaryDirectory() as tmp:
        runs = _synthetic_runs(Path(tmp))
        target = runs / "campaign/trial-a/agent/trajectory.json"
        buf = read_source_once(target, runs)
        check(
            "digest describes the SAME buffer that was parsed",
            hashlib.sha256(buf.raw).hexdigest() == buf.digest
            and buf.doc == json.loads(buf.raw.decode("utf-8")),
        )
        link_dir = runs / "campaign/link/agent"
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / "trajectory.json"
        link.symlink_to(target)
        try:
            read_source_once(link, runs)
            check("symlink source is rejected", False, "no raise")
        except SourceRejectedError:
            check("symlink source is rejected", True)

        outside = Path(tmp) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        esc_dir = runs / "campaign/esc/agent"
        esc_dir.mkdir(parents=True, exist_ok=True)
        esc = esc_dir / "trajectory.json"
        esc.symlink_to(outside)
        try:
            read_source_once(esc, runs)
            check("root-escaping symlink is rejected", False, "no raise")
        except SourceRejectedError:
            check("root-escaping symlink is rejected", True)

    with tempfile.TemporaryDirectory() as tmp:
        runs = _synthetic_runs(Path(tmp))
        pkg, tru = build_package(runs, core_n=None, boost_per_stratum=0, ratings_dir=None)
        pkg_path = Path(tmp) / "out" / "pkg.json"
        tru_path = Path(tmp) / "out" / "truth.json"
        bid = write_paired_outputs(pkg, tru, pkg_path, tru_path)
        check(
            "both artifacts carry the same build_id",
            bid == json.loads(tru_path.read_text(encoding="utf-8"))["build_id"],
        )
        loaded_pkg, loaded_tru = load_paired_artifacts(pkg_path, tru_path)
        check(
            "paired loader accepts a matched pair", loaded_pkg["build_id"] == loaded_tru["build_id"]
        )
        # corrupt the pair
        bad = json.loads(tru_path.read_text(encoding="utf-8"))
        bad["build_id"] = "0" * 64
        tru_path.write_text(json.dumps(bad), encoding="utf-8")
        try:
            load_paired_artifacts(pkg_path, tru_path)
            check("mismatched pair is refused", False, "no raise")
        except PairMismatchError:
            check("mismatched pair is refused", True)
        try:
            write_paired_outputs(pkg, tru, pkg_path, pkg_path)
            check("identical output paths refused", False, "no raise")
        except OutputPathError:
            check("identical output paths refused", True)
        check(
            "build_id changes when content changes",
            compute_build_id({"a": 1}, {"b": 2}) != compute_build_id({"a": 2}, {"b": 2}),
        )
        check(
            "no temp files left behind",
            not list(pkg_path.parent.glob(".*.tmp")),
        )

    print("SEC-REG - authenticated qualified-rater registry")
    reg = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "authority_key_id": "auth-1",
        "raters": [
            {"key_id": f"r{n}", "shared_secret": f"s{n}", "qualified": True} for n in (1, 2, 3)
        ],
    }
    reg["signature"] = sign_registry(reg, "AUTHORITY")
    with tempfile.TemporaryDirectory() as tmp:
        rp = Path(tmp) / "reg.json"
        rp.write_text(json.dumps(reg), encoding="utf-8")
        q_ok, k_ok, prob_ok = load_rater_registry(rp, "AUTHORITY")
        check("signed registry yields the qualified pool", q_ok == ["r1", "r2", "r3"])
        check("signed registry has no problems", prob_ok == [])
        check("registry supplies a keyring", set(k_ok) == {"r1", "r2", "r3"})
        check(
            "wrong authority secret yields an EMPTY pool",
            load_rater_registry(rp, "WRONG")[0] == [],
        )
        tampered = {
            **reg,
            "raters": [
                *reg["raters"],
                {"key_id": "rogue", "shared_secret": "x", "qualified": True},
            ],
        }
        rp.write_text(json.dumps(tampered), encoding="utf-8")
        q_bad, _, prob_bad = load_rater_registry(rp, "AUTHORITY")
        check("tampered roster is rejected", q_bad == [])
        check(
            "tampered roster reports a signature failure",
            "REGISTRY_SIGNATURE_INVALID" in prob_bad,
        )
        rp.write_text(
            json.dumps({k: v for k, v in reg.items() if k != "signature"}),
            encoding="utf-8",
        )
        check("unsigned registry is rejected", load_rater_registry(rp, "AUTHORITY")[0] == [])
    check(
        "absent registry is an explicit problem, not a silent empty pool",
        load_rater_registry(None, None) == ([], {}, ["REGISTRY_ABSENT"]),
    )
    check(
        "package records the registry blocker",
        any("REGISTRY" in b for b in package["readiness"]["blockers"]),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
