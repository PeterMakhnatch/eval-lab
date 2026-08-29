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
    KEYSTORE_SCHEMA_VERSION,
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
    compute_item_context_digest,
    effective_clusters,
    evaluate_cluster_adequacy,
    evaluate_readiness,
    export_rater_bundle,
    label_item_from_dict,
    load_paired_artifacts,
    load_rater_registry,
    logical_trial_digest,
    read_source_once,
    sign_rating,
    sign_registry,
    validate_rating,
    verify_rating_signature,
    write_paired_outputs,
)

EXPECTED_ITEMS = 183  # distinct contexts; only byte-identical paths alias
EXPECTED_CLUSTERS = 20
EXPECTED_DIGEST = "ad169c4b76da2985285b242cfee3471916d78f098a75d11c113d91b2a13259c2"


def _write_signed_roster(root: Path, *, with_secret: bool) -> Path:
    raters = [{"key_id": "r1", "qualified": True}]
    if with_secret:
        raters[0]["shared_secret"] = "leaked"
    reg = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "authority_key_id": "auth-1",
        "raters": raters,
    }
    reg["signature"] = sign_registry(reg, "AUTHORITY")
    path = root / f"roster-{'secret' if with_secret else 'clean'}.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    return path


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
        "unique agent steps is 183 (distinct contexts preserved)",
        package["census"]["agent_steps_unique"] == EXPECTED_ITEMS,
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
            item_context_digest=f"{n:064x}",
            logical_lineage=(),
            context_completeness={"builder_verdict": "COMPLETE"},
            rater_context={},
        )
        for n in range(40)
    ]
    b4_keyring = {"r1": "k1", "r2": "k2", "r3": "k3"}

    def _signed(item_id: str, key_id: str, **over: object) -> dict:
        rec = {
            "schema_version": RATING_SCHEMA_VERSION,
            "rating_contract_digest": "contract",
            "item_id": item_id,
            "item_context_digest": None,
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
            rating_contract_digest="contract",
            keyring=b4_keyring,
        )["readiness"]

    good = _signed("item000", "r1", item_context_digest=f"{0:064x}")
    check(
        "valid record passes validation",
        validate_rating(
            good,
            rating_contract_digest="contract",
            context_digests={"item000": f"{0:064x}"},
            keyring=b4_keyring,
            qualified_rater_ids=list(b4_keyring),
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
        _signed(i.item_id, f"r{n}", item_context_digest=i.item_context_digest)
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
                _signed(i.item_id, "r1", item_context_digest=i.item_context_digest)
                for i in fake
                for _ in range(3)
            ]
        )
        == "NOT_READY",
    )
    check(
        "unqualified rater does NOT clear readiness",
        evaluate_readiness(
            fake,
            three,
            ["r1", "r2", "rX"],
            rating_contract_digest="contract",
            keyring=b4_keyring,
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
                    item_context_digest=i.item_context_digest,
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
                rating_contract_digest="contract",
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
                        item_context_digest=i.item_context_digest,
                        step_contribution="HARMFUL",
                    )
                    for i in fake
                ],
                list(b4_keyring),
                rating_contract_digest="contract",
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
        f"item count is {EXPECTED_ITEMS} (clones deduped)",
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
        blob = json.dumps(bundle)
        check("bundle withholds builder_verdict", "builder_verdict" not in blob)
        check("bundle withholds degraded_reasons", "degraded_reasons" not in blob)
        check("bundle withholds context_completeness", "context_completeness" not in blob)
        check(
            "bundle item exposes only id, context digest and context",
            all(
                set(i) == {"item_id", "item_context_digest", "rater_context"}
                for i in bundle["items"]
            ),
        )
        check(
            "bundle binds the immutable rating contract digest",
            bundle["rating_contract_digest"]
            == package["readiness"]["authentication"]["rating_contract_digest"],
        )
        check(
            "bundle labels package_digest informational-only, not signed",
            "package_digest_informational_only" in bundle and "package_digest" not in bundle,
        )
        check(
            "every bundle item binds an item_context_digest",
            all("item_context_digest" in i for i in bundle["items"]),
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

    print("SEC2 - logical clone digest, BOUNDED stripping")
    a_steps = [{"source": "agent", "message": "hi", "tool_calls": [], "observation": {}}]
    # Metadata-only differences MUST collapse.
    meta_clone = [
        {
            **a_steps[0],
            "timestamp": "2026-01-01T00:00:00Z",
            "step_id": "xyz",
            "metrics": {"cost": 1},
            "extra": {"anything": True},
        }
    ]
    check(
        "metadata-only clones share a logical digest",
        logical_trial_digest(a_steps) == logical_trial_digest(meta_clone),
    )
    # Payload whitespace MUST NOT collapse: two code blocks differing only in
    # indentation are not the same program (P2 - collision risk).
    check(
        "payload whitespace does NOT collapse",
        logical_trial_digest(a_steps) != logical_trial_digest([{**a_steps[0], "message": " hi  "}]),
    )
    check(
        "indentation-differing code payloads stay distinct",
        logical_trial_digest([{**a_steps[0], "message": "def f():\n    return 1"}])
        != logical_trial_digest([{**a_steps[0], "message": "def f():\n        return 1"}]),
    )
    check(
        "per-call volatile ids are stripped at their known depth",
        logical_trial_digest(
            [{**a_steps[0], "tool_calls": [{"function_name": "x", "tool_call_id": "a"}]}]
        )
        == logical_trial_digest(
            [{**a_steps[0], "tool_calls": [{"function_name": "x", "tool_call_id": "b"}]}]
        ),
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
        "rating_contract_digest": "contract",
        "item_id": "item000",
        "item_context_digest": f"{0:064x}",
        "rater_key_id": "key-1",
        **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
    }
    signed = {**base, "signature": sign_rating(base, keyring["key-1"])}
    digests = {"item000": f"{0:064x}"}
    check(
        "correctly signed record validates",
        validate_rating(
            signed,
            rating_contract_digest="contract",
            context_digests=digests,
            keyring=keyring,
            qualified_rater_ids=list(keyring),
        )
        == [],
    )
    check(
        "self-asserted record with no signature is rejected",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(
            base,
            rating_contract_digest="contract",
            context_digests=digests,
            keyring=keyring,
            qualified_rater_ids=list(keyring),
        ),
    )
    check(
        "unregistered key is rejected",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(
            {**signed, "rater_key_id": "rogue"},
            rating_contract_digest="contract",
            context_digests=digests,
            keyring=keyring,
            qualified_rater_ids=list(keyring),
        ),
    )
    check(
        "item_set_digest mismatch is rejected (replay defence)",
        "RATING_CONTRACT_DIGEST_MISMATCH"
        in validate_rating(
            signed, rating_contract_digest="OTHER", context_digests=digests, keyring=keyring
        ),
    )
    check(
        "item_context_digest mismatch is rejected",
        "ITEM_CONTEXT_DIGEST_MISMATCH"
        in validate_rating(
            {**signed, "item_context_digest": "f" * 64},
            rating_contract_digest="contract",
            context_digests=digests,
            keyring=keyring,
            qualified_rater_ids=list(keyring),
        ),
    )
    check(
        "tampering with a label invalidates the signature",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in validate_rating(
            {**signed, "step_contribution": "HARMFUL"},
            rating_contract_digest="contract",
            context_digests=digests,
            keyring=keyring,
            qualified_rater_ids=list(keyring),
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

    print("SEC-REG - signed roster + SEPARATE keystore")
    reg = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "authority_key_id": "auth-1",
        "raters": [{"key_id": f"r{n}", "qualified": True} for n in (1, 2, 3)],
    }
    reg["signature"] = sign_registry(reg, "AUTHORITY")
    keystore = {
        "schema_version": KEYSTORE_SCHEMA_VERSION,
        "keys": {f"r{n}": f"s{n}" for n in (1, 2, 3)},
    }
    with tempfile.TemporaryDirectory() as tmp:
        rp = Path(tmp) / "reg.json"
        rp.write_text(json.dumps(reg), encoding="utf-8")
        ks = Path(tmp) / "keystore.json"
        ks.write_text(json.dumps(keystore), encoding="utf-8")

        check(
            "roster carries NO secret material",
            not any(
                k in entry
                for entry in reg["raters"]
                for k in ("shared_secret", "secret", "key", "private_key")
            ),
        )
        check(
            "roster with an embedded secret is REJECTED outright",
            load_rater_registry(_write_signed_roster(Path(tmp), with_secret=True), "AUTHORITY", ks)[
                2
            ]
            == ["REGISTRY_CONTAINS_SECRET_MATERIAL"],
        )
        check(
            "roster without a keystore yields an EMPTY pool",
            load_rater_registry(rp, "AUTHORITY", None)[0] == [],
        )
        q_ok, k_ok, prob_ok = load_rater_registry(rp, "AUTHORITY", ks)
        check("signed roster + keystore yields the pool", q_ok == ["r1", "r2", "r3"])
        check("no problems on the happy path", prob_ok == [])
        check("keyring comes from the keystore", set(k_ok) == {"r1", "r2", "r3"})
        check(
            "wrong authority secret yields an EMPTY pool",
            load_rater_registry(rp, "WRONG", ks)[0] == [],
        )
        partial = Path(tmp) / "partial.json"
        partial.write_text(
            json.dumps({"schema_version": KEYSTORE_SCHEMA_VERSION, "keys": {"r1": "s1"}}),
            encoding="utf-8",
        )
        q_part, _, prob_part = load_rater_registry(rp, "AUTHORITY", partial)
        check("keystore missing a key drops that rater", q_part == ["r1"])
        check(
            "keystore gap is reported",
            any(p.startswith("KEYSTORE_MISSING_KEY") for p in prob_part),
        )
        tampered = {
            **reg,
            "raters": [*reg["raters"], {"key_id": "rogue", "qualified": True}],
        }
        rp.write_text(json.dumps(tampered), encoding="utf-8")
        q_bad, _, prob_bad = load_rater_registry(rp, "AUTHORITY", ks)
        check("tampered roster is rejected", q_bad == [])
        check(
            "tampered roster reports a signature failure",
            "REGISTRY_SIGNATURE_INVALID" in prob_bad,
        )
        rp.write_text(
            json.dumps({k: v for k, v in reg.items() if k != "signature"}),
            encoding="utf-8",
        )
        check(
            "unsigned registry is rejected",
            load_rater_registry(rp, "AUTHORITY", ks)[0] == [],
        )
    check(
        "absent registry is an explicit problem, not a silent empty pool",
        load_rater_registry(None, None, None) == ([], {}, ["REGISTRY_ABSENT"]),
    )
    check(
        "package records the registry blocker",
        any("REGISTRY" in b for b in package["readiness"]["blockers"]),
    )

    print("E2E - signed 3-rater fixture against the REAL package")
    real_contract = package["readiness"]["authentication"]["rating_contract_digest"]

    e2e_keys = {f"rater-{n}": f"secret-{n}" for n in (1, 2, 3)}
    real_items = [label_item_from_dict(item) for item in package["items"][:6]]

    def _e2e(item: LabelItem, key_id: str, **over: object) -> dict:
        rec = {
            "schema_version": RATING_SCHEMA_VERSION,
            "rating_contract_digest": real_contract,
            "item_id": item.item_id,
            "item_context_digest": item.item_context_digest,
            "rater_key_id": key_id,
            **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            **over,
        }
        rec["signature"] = sign_rating(rec, e2e_keys[key_id])
        return rec

    full = [_e2e(i, f"rater-{n}") for i in real_items for n in (1, 2, 3)]
    e2e_result = evaluate_readiness(
        real_items,
        full,
        list(e2e_keys),
        rating_contract_digest=real_contract,
        keyring=e2e_keys,
    )
    rater_blockers = [
        b for b in e2e_result["blockers"] if "RATER" in b or "RATING" in b or "SUBMISSION" in b
    ]
    check("3 signed raters clear every RATER-side blocker", rater_blockers == [])
    check(
        "real context digests validate end to end",
        all(
            validate_rating(
                r,
                rating_contract_digest=real_contract,
                context_digests={i.item_id: i.item_context_digest for i in real_items},
                keyring=e2e_keys,
                qualified_rater_ids=list(e2e_keys),
            )
            == []
            for r in full
        ),
    )

    print("ATTACK - tampering must invalidate")
    ctx_map = {i.item_id: i.item_context_digest for i in real_items}

    def _attack(record: dict) -> list[str]:
        return validate_rating(
            record,
            rating_contract_digest=real_contract,
            context_digests=ctx_map,
            keyring=e2e_keys,
            qualified_rater_ids=list(e2e_keys),
        )

    check(
        "altered item_context_digest is rejected",
        "ITEM_CONTEXT_DIGEST_MISMATCH" in _attack({**full[0], "item_context_digest": "f" * 64}),
    )
    check(
        "altered label is rejected (signature covers labels)",
        "SIGNATURE_INVALID_OR_UNREGISTERED_KEY"
        in _attack({**full[0], "step_contribution": "HARMFUL"}),
    )
    check(
        "replayed contract digest from another cut is rejected",
        "RATING_CONTRACT_DIGEST_MISMATCH"
        in _attack({**full[0], "rating_contract_digest": "0" * 64}),
    )
    check(
        "malformed scalar rating entry FAILS CLOSED, never crashes",
        any(
            e.startswith("MALFORMED_ENTRY")
            for e in validate_rating({"_invalid_entry": "f.json[0]: got int"})
        ),
    )
    check(
        "absent keyring FAILS CLOSED, never skips",
        "SIGNATURE_UNVERIFIABLE_NO_KEYRING"
        in validate_rating(
            full[0],
            rating_contract_digest=real_contract,
            context_digests=ctx_map,
            keyring=None,
        ),
    )
    check(
        "unenforced digests are themselves errors",
        {"RATING_CONTRACT_DIGEST_NOT_ENFORCED", "ITEM_CONTEXT_DIGEST_NOT_ENFORCED"}
        <= set(validate_rating(full[0], keyring=e2e_keys)),
    )
    check(
        "tampering the INSTRUCTION changes the context digest",
        compute_item_context_digest(
            {
                **package["items"][0]["rater_context"],
                "instruction": {"presumed_task_statement": {"message": "OTHER TASK"}},
            }
        )
        != package["items"][0]["item_context_digest"],
    )
    check(
        "tampering a PRIOR OBSERVATION changes the context digest",
        compute_item_context_digest({**package["items"][0]["rater_context"], "prior_steps": []})
        != package["items"][0]["item_context_digest"],
    )

    print("SEC-DIAG - forged records must NEVER reach the diagnostic")
    diag_items = real_items[:3]
    diag_good = [_e2e(i, f"rater-{n}") for i in diag_items for n in (1, 2, 3)]
    diag_forged = [
        # bad signature, claiming INSUFFICIENT_CONTEXT to poison the 2x2
        {
            **diag_good[0],
            "signature": "0" * 64,
            "step_contribution": INSUFFICIENT_CONTEXT,
        },
        # unqualified key
        {
            **_e2e(diag_items[0], "rater-1"),
            "rater_key_id": "rogue",
            "step_contribution": INSUFFICIENT_CONTEXT,
        },
        # replayed contract digest from another cut
        {
            **_e2e(diag_items[0], "rater-1"),
            "rating_contract_digest": "0" * 64,
            "step_contribution": INSUFFICIENT_CONTEXT,
        },
        # malformed entry as produced by the loader for a scalar JSON value
        {"_invalid_entry": "forged.json[0]: expected object, got int"},
    ]
    diag_res = evaluate_readiness(
        diag_items,
        diag_good + diag_forged,
        list(e2e_keys),
        rating_contract_digest=real_contract,
        keyring=e2e_keys,
    )
    intake = diag_res["rating_intake"]
    counts = diag_res["context_diagnostic_2x2"]["counts"]
    check(
        "every forged record is rejected",
        intake["records_rejected"] == len(diag_forged),
        f"got {intake['records_rejected']} of {len(diag_forged)}",
    )
    check(
        "diagnostic total equals ACCEPTED count, not seen count",
        sum(counts.values()) == intake["records_accepted"]
        and intake["records_accepted"] == len(diag_good),
    )
    check(
        "forged INSUFFICIENT_CONTEXT does NOT reach the 2x2",
        counts["COMPLETE|INSUFFICIENT_CONTEXT"] == 0,
    )
    check(
        "reconciliation: sum(primary reasons) == records_rejected",
        sum(intake["primary_rejection_reasons"].values())
        == intake["records_rejected"]
        == intake["records_with_reason"],
    )
    check(
        "all_reasons is NON-EXCLUSIVE and may sum higher than rejected",
        sum(intake["all_rejection_reasons_non_exclusive"].values()) >= intake["records_rejected"],
    )
    check(
        "rejection reasons are reported by category",
        {
            "SIGNATURE_INVALID_OR_UNREGISTERED_KEY",
            "RATING_CONTRACT_DIGEST_MISMATCH",
            "MALFORMED_ENTRY",
        }
        <= set(intake["primary_rejection_reasons"])
        | set(intake["all_rejection_reasons_non_exclusive"]),
        f"got {sorted(intake['primary_rejection_reasons'])}",
    )
    check(
        "records_seen accounts for every submission",
        intake["records_seen"] == len(diag_good) + len(diag_forged),
    )

    print("SEC-QUAL - signature-VALID record from an UNQUALIFIED key")
    # The keyring HOLDS this key; the qualified roster does NOT list it. Signature
    # verification therefore SUCCEEDS, which is exactly why qualification has to be
    # an acceptance criterion rather than a readiness blocker only.
    qual_keys = {**e2e_keys, "rogue": "rogue-secret"}
    qual_items = real_items[:2]

    def _q(item: LabelItem, key_id: str, **over: object) -> dict:
        rec = {
            "schema_version": RATING_SCHEMA_VERSION,
            "rating_contract_digest": real_contract,
            "item_id": item.item_id,
            "item_context_digest": item.item_context_digest,
            "rater_key_id": key_id,
            **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            **over,
        }
        rec["signature"] = sign_rating(rec, qual_keys[key_id])
        return rec

    qual_good = [_q(i, f"rater-{n}") for i in qual_items for n in (1, 2, 3)]
    rogue = _q(qual_items[0], "rogue", step_contribution=INSUFFICIENT_CONTEXT)
    check(
        "rogue signature is genuinely VALID against its own key",
        verify_rating_signature(rogue, qual_keys),
    )
    qual_res = evaluate_readiness(
        qual_items,
        [*qual_good, rogue],
        list(e2e_keys),  # roster EXCLUDES rogue
        rating_contract_digest=real_contract,
        keyring=qual_keys,
    )
    qi = qual_res["rating_intake"]
    qc = qual_res["context_diagnostic_2x2"]["counts"]
    check(
        "valid-signature unqualified record is REJECTED",
        qi["records_rejected"] == 1,
        f"got {qi['records_rejected']}",
    )
    check(
        "canonical reason is RATER_KEY_NOT_QUALIFIED",
        qi["primary_rejection_reasons"].get("RATER_KEY_NOT_QUALIFIED") == 1,
        f"got {qi['primary_rejection_reasons']}",
    )
    check(
        "unqualified record is ABSENT from the diagnostic",
        qc["COMPLETE|INSUFFICIENT_CONTEXT"] == 0,
    )
    check(
        "diagnostic total equals accepted, excluding the rogue",
        sum(qc.values()) == qi["records_accepted"] == len(qual_good),
    )
    check(
        "seen == accepted + rejected",
        qi["records_seen"] == qi["records_accepted"] + qi["records_rejected"],
    )
    check(
        "reason totals reconcile exactly on the primary tally",
        sum(qi["primary_rejection_reasons"].values())
        == qi["records_rejected"]
        == qi["records_with_reason"],
    )
    check(
        "qualification cannot be silently skipped",
        "QUALIFICATION_NOT_ENFORCED"
        in validate_rating(
            rogue, rating_contract_digest=real_contract, context_digests=ctx_map, keyring=qual_keys
        ),
    )

    print("SEC-CLONE - item-level logical dedup with lineage")
    logicals = [i["logical_step_digest"] for i in items]
    check(
        "items are unique on CONTEXT digest (identity), not step content",
        len({i["item_context_digest"] for i in items}) == len(items),
    )
    check(
        "two items MAY share a logical step digest - same message, different trials",
        len(set(logicals)) < len(logicals),
    )
    check(
        "distinct trial/step contexts are NEVER merged",
        len({i["item_context_digest"] for i in items}) == len(items),
    )
    check(
        "context digest includes trial identity and step ordinal",
        compute_item_context_digest({}, cluster_id="a", step_index=1)
        != compute_item_context_digest({}, cluster_id="b", step_index=1),
    )
    check(
        "same context, different step ordinal stays distinct",
        compute_item_context_digest({}, cluster_id="a", step_index=17)
        != compute_item_context_digest({}, cluster_id="a", step_index=18),
    )

    print("SEC-DELIVERY - incomplete items are never shipped")
    check(
        "no INCOMPLETE item is deliverable",
        all(
            i["context_completeness"]["builder_verdict"] == "COMPLETE"
            for i in items
            if i["item_id"] in set(package["deliverable_item_ids"])
        ),
    )
    check(
        "excluded_incomplete is reported",
        "excluded_incomplete" in package,
    )
    check(
        "nothing is truncated at current corpus size",
        not any(
            s["message_truncated"]
            or any(c["arguments_truncated"] for c in s["tool_calls"])
            or any(o["content_truncated"] for o in s["observation"])
            for i in items
            for s in [*i["rater_context"]["prior_steps"], i["rater_context"]["item_step"]]
        ),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
