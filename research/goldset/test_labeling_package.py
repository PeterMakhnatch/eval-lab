"""Regression tests for the five review blockers on the gold-set package.

Each test names the blocker it guards. Run:
    python3 research/goldset/test_labeling_package.py
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import build_labeling_package as build_labeling_module
from build_labeling_package import (  # noqa: E402
    ALLOWED_VALUES,
    BUNDLE_SCHEMA,
    CANNOT_JUDGE,
    GENESIS_HASH,
    HUMAN_JUDGED_FIELDS,
    INSUFFICIENT_CONTEXT,
    KEYSTORE_SCHEMA_VERSION,
    LEDGER_SCHEMA,
    MIN_EFFECTIVE_CLUSTERS,
    RATING_SCHEMA_VERSION,
    RATING_SIGNED_FIELDS,
    REGISTRY_SCHEMA_VERSION,
    REQUIRED_RATERS_PER_ITEM,
    BundleContaminationError,
    BundleVerificationError,
    LabelItem,
    LedgerError,
    LedgerRecoverableError,
    OutputPathError,
    PairMismatchError,
    SourceRejectedError,
    append_rating_record,
    build_package,
    compute_build_id,
    compute_bundle_contract_digest,
    compute_item_context_digest,
    effective_clusters,
    effective_ratings,
    evaluate_cluster_adequacy,
    evaluate_readiness,
    export_rater_bundle,
    label_item_from_dict,
    ledger_head,
    ledger_id,
    load_intake,
    load_ledger,
    load_paired_artifacts,
    load_rater_registry,
    logical_trial_digest,
    prepare_rating,
    publish_anchor,
    read_source_once,
    resolve_anchor,
    sign_ledger_anchor,
    sign_rating,
    sign_registry,
    validate_rating,
    verify_against_anchor,
    verify_bundle,
    verify_rating_signature,
    write_paired_outputs,
)
from build_labeling_package import _entry_hash as _entry_hash_for_test
from build_labeling_package import _read_lock as _read_lock_for_test
from build_labeling_package import _record_id as _record_id_for_test

EXPECTED_ITEMS = 183  # distinct contexts; only byte-identical paths alias
EXPECTED_CLUSTERS = 20
EXPECTED_DIGEST = "af040dd0471da40f5442e1b1bc3ee0c2efda5ddcad5dab429c90e8556f797d59"


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


def _raises(exc: type[BaseException], fn: Callable[[], object]) -> bool:
    """True when fn() raises exc. Keeps the adversarial checks readable."""
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _err_msg(fn: Callable[[], object]) -> str:
    """The LedgerError message fn() raises, or '' when it does not.

    Lets a check assert on the SPECIFIC refusal identifier (e.g. a substring
    like ``LEDGER_UNANCHORED_SUFFIX``) instead of merely "something went
    wrong" - the vacuity that let a cycle-detection bug pass its attack tests.
    """
    try:
        fn()
    except LedgerError as exc:
        return str(exc)
    return ""


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def _headless(src: Path, dst: Path) -> Path:
    """Copy a ledger and remove ONLY head.json: records exist, manifest gone."""
    shutil.copytree(src, dst)
    (dst / "head.json").unlink()
    return dst


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
            "bundle item exposes only id, digests, identity inputs and context",
            all(
                set(i)
                == {
                    "item_id",
                    "item_context_digest",
                    "cluster_id",
                    "step_index",
                    "rater_context",
                }
                for i in bundle["items"]
            ),
        )
        check(
            "bundle contract digest covers the FULL canonical bundle",
            bundle["rating_contract_digest"] == compute_bundle_contract_digest(bundle),
        )
        check(
            "bundle carries NO artifact digest: build identity is not contract",
            "package_digest" not in bundle and "package_digest_informational_only" not in bundle,
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

    print("SEC-ISO - bundle physical isolation by exact allowlist")
    with tempfile.TemporaryDirectory() as tmp:
        # The withheld truth RENAMED to a benign nested filename. A pathname
        # denylist passed this; an exact allowlist cannot.
        dirty = Path(tmp) / "dirty"
        (dirty / "deep").mkdir(parents=True)
        (dirty / "deep" / "answers.json").write_text(
            TRUTH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        try:
            export_rater_bundle(package, dirty)
            check("renamed withheld truth is refused", False, "no raise")
        except BundleContaminationError:
            check("renamed withheld truth is refused", True)
        check(
            "no bundle is published into a contaminated destination",
            not (dirty / "rater_bundle.json").exists(),
        )

        clean = Path(tmp) / "clean"
        out = export_rater_bundle(package, clean)
        check("clean destination publishes exactly one file", out.is_file())
        check(
            "published destination holds ONLY the allowlisted file",
            sorted(p.name for p in clean.rglob("*")) == ["rater_bundle.json"],
        )

        linky = Path(tmp) / "linky"
        linky.mkdir()
        (linky / "sneak.json").symlink_to(TRUTH)
        try:
            export_rater_bundle(package, linky)
            check("symlink in destination is refused", False, "no raise")
        except BundleContaminationError:
            check("symlink in destination is refused", True)

    print("SEC-REG2 - registry raters field shape")
    with tempfile.TemporaryDirectory() as tmp:
        ks2 = Path(tmp) / "k.json"
        ks2.write_text(
            json.dumps({"schema_version": KEYSTORE_SCHEMA_VERSION, "keys": {"r1": "s1"}}),
            encoding="utf-8",
        )
        for raters, expect in (
            (5, "REGISTRY_RATERS_NOT_A_LIST"),
            ({"a": 1}, "REGISTRY_RATERS_NOT_A_LIST"),
            (["scalar"], "REGISTRY_ENTRY_NOT_AN_OBJECT"),
            ([7], "REGISTRY_ENTRY_NOT_AN_OBJECT"),
        ):
            reg2 = {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "authority_key_id": "a",
                "raters": raters,
            }
            reg2["signature"] = sign_registry(reg2, "AUTHORITY")
            rp2 = Path(tmp) / "r.json"
            rp2.write_text(json.dumps(reg2), encoding="utf-8")
            problems = load_rater_registry(rp2, "AUTHORITY", ks2)[2]
            check(
                f"raters={raters!r} fails closed with {expect}",
                any(p.startswith(expect) for p in problems),
                f"got {problems}",
            )

    print("SEC-CONTRACT - bundle is independently verifiable")
    DIST = "coordinator-distribution-key"
    with tempfile.TemporaryDirectory() as tmp:
        signed_out = export_rater_bundle(package, Path(tmp) / "signed", distribution_secret=DIST)
        sb = json.loads(signed_out.read_text(encoding="utf-8"))
        check("bundle is schema v2", sb["schema_version"] == BUNDLE_SCHEMA)
        check("coordinator signature present", "coordinator_signature" in sb)
        check(
            "identity inputs are SIGNED bundle fields, so digests are recomputable",
            all("cluster_id" in i and "step_index" in i for i in sb["items"]),
        )
        check("verify_bundle accepts the genuine bundle", bool(verify_bundle(sb, DIST)))
        check(
            "verification without a distribution key FAILS CLOSED",
            _raises(BundleVerificationError, lambda: verify_bundle(sb, None)),
        )
        for name, mutate in (
            ("poisoned taxonomy", lambda d: d.update({"taxonomy": {"hacked": True}})),
            (
                "poisoned item context",
                lambda d: d["items"][0]["rater_context"].update({"instruction": {}}),
            ),
            (
                "swapped contract digest",
                lambda d: d.update({"rating_contract_digest": "0" * 64}),
            ),
            (
                "tampered item_context_digest",
                lambda d: d["items"][0].update({"item_context_digest": "f" * 64}),
            ),
            (
                "tampered instructions",
                lambda d: d["instructions_to_rater"].update({"required_fields": []}),
            ),
        ):
            poisoned = json.loads(json.dumps(sb))
            mutate(poisoned)
            check(
                f"{name} is refused",
                _raises(BundleVerificationError, lambda p=poisoned: verify_bundle(p, DIST)),
            )
        check(
            "a non-mapping bundle is refused",
            _raises(
                BundleVerificationError,
                lambda: verify_bundle({"x": 1}, DIST),
            ),
        )
        for name, mutate in (
            ("unsupported schema", lambda d: {**d, "schema_version": "v0"}),
            ("missing taxonomy", lambda d: {k: v for k, v in d.items() if k != "taxonomy"}),
            (
                "missing instructions",
                lambda d: {k: v for k, v in d.items() if k != "instructions_to_rater"},
            ),
            ("items not a list", lambda d: {**d, "items": {"a": 1}}),
            ("empty bundle", lambda d: {**d, "items": []}),
            (
                "item missing cluster_id",
                lambda d: {
                    **d,
                    "items": [{k: v for k, v in d["items"][0].items() if k != "cluster_id"}],
                },
            ),
            ("duplicate item ids", lambda d: {**d, "items": [d["items"][0], dict(d["items"][0])]}),
        ):
            malformed = mutate(json.loads(json.dumps(sb)))
            check(
                f"{name} is refused with a typed error",
                _raises(
                    BundleVerificationError,
                    lambda m=malformed: verify_bundle(m, DIST),
                ),
            )
        check(
            "wrong distribution key is refused",
            _raises(BundleVerificationError, lambda: verify_bundle(sb, "WRONG")),
        )

        labels = {f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS}
        rec = prepare_rating(
            sb,
            item_id=sb["items"][0]["item_id"],
            labels=labels,
            rater_key_id="r1",
            rater_secret="s1",
            distribution_secret=DIST,
        )
        check(
            "client RECOMPUTES rather than copying supplied digests",
            rec["rating_contract_digest"] == compute_bundle_contract_digest(sb)
            and rec["item_context_digest"]
            == compute_item_context_digest(
                sb["items"][0]["rater_context"],
                cluster_id=sb["items"][0]["cluster_id"],
                step_index=sb["items"][0]["step_index"],
            ),
        )
        bad = json.loads(json.dumps(sb))
        bad["items"][0]["rater_context"]["prior_steps"] = []
        check(
            "client REFUSES to sign a poisoned bundle",
            _raises(
                BundleVerificationError,
                lambda: prepare_rating(
                    bad,
                    item_id=bad["items"][0]["item_id"],
                    labels=labels,
                    rater_key_id="r1",
                    rater_secret="s1",
                    distribution_secret=DIST,
                ),
            ),
        )

    print("SEC-LEDGER - real append-only intake, AUTHENTICATED at append")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger"
        LC = "c" * 64
        LCTX = {"i1": "d" * 64, "i2": "d" * 64}
        LKR = {"r1": "s1", "r2": "s2"}
        LQ = ["r1", "r2"]
        VC: dict[str, Any] = {
            "rating_contract_digest": LC,
            "context_digests": LCTX,
            "keyring": LKR,
            "qualified_rater_ids": LQ,
        }

        def _lrec(
            item: str = "i1",
            rater: str = "r1",
            supersedes: str | None = None,
            secret: str | None = None,
            **over: object,
        ) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": LC,
                "item_id": item,
                "item_context_digest": LCTX[item],
                "rater_key_id": rater,
                "supersedes": supersedes,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
                **over,
            }
            body["signature"] = sign_rating(body, secret or LKR[rater])
            return body

        first = append_rating_record(ledger, _lrec(), created_at="T0", **VC)
        append_rating_record(ledger, _lrec(rater="r2"), created_at="T1", **VC)
        check("chain loads and verifies", len(load_ledger(ledger)) == 2)
        check("record carries record_id", bool(first["record_id"]))
        check("record carries created_at", first["created_at"] == "T0")
        check(
            "record carries previous_entry_hash",
            first["previous_entry_hash"] == GENESIS_HASH,
        )
        head_hash, count = ledger_head(ledger)
        check("head manifest tracks count", count == 2 and len(head_hash) == 64)
        check(
            "record files are written read-only",
            (oct((ledger / "records" / f"{first['record_id']}.json").stat().st_mode)[-3:] == "444"),
        )
        check(
            "an UNSIGNED record is refused at append",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger, {"item_id": "i1", "rater_key_id": "r1"}, created_at="TU", **VC
                ),
            ),
        )
        check(
            "replayed identical rating is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(ledger, _lrec(), created_at="T0", **VC),
            ),
        )
        check(
            "replay at a new timestamp is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(ledger, _lrec(), created_at="T9", **VC),
            ),
        )
        check(
            "supersede of an unknown record is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger, _lrec(supersedes="f" * 64), created_at="T2", **VC
                ),
            ),
        )
        # Correction intent is read SOLELY from the signed record; there is no
        # supersedes parameter to disagree with it.
        correction = append_rating_record(
            ledger,
            _lrec(supersedes=first["record_id"], step_contribution="HARMFUL"),
            created_at="T3",
            **VC,
        )
        check("correction APPENDS", len(load_ledger(ledger)) == 3)
        check(
            "superseded record is retained, never replaced",
            (ledger / "records" / f"{first['record_id']}.json").is_file(),
        )
        check(
            "effective view resolves to the correction",
            any(
                r["record_id"] == correction["record_id"]
                for r in effective_ratings(load_ledger(ledger), **VC)
            )
            and len(effective_ratings(load_ledger(ledger), **VC)) == 2,
        )
        check(
            "double-supersede is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger,
                    _lrec(supersedes=first["record_id"], step_contribution="NEUTRAL"),
                    created_at="T4",
                    **VC,
                ),
            ),
        )
        _ap = inspect.signature(append_rating_record).parameters
        _ef = inspect.signature(effective_ratings).parameters
        check(
            "append_rating_record has NO supersedes parameter",
            "supersedes" not in _ap,
        )
        check(
            "the verifier context is REQUIRED on append, never defaulted",
            all(
                _ap[f].default is inspect.Parameter.empty
                for f in (
                    "rating_contract_digest",
                    "context_digests",
                    "keyring",
                    "qualified_rater_ids",
                )
            ),
        )
        check(
            "the verifier context is REQUIRED on effective_ratings, never defaulted",
            all(
                f in _ef and _ef[f].default is inspect.Parameter.empty
                for f in (
                    "rating_contract_digest",
                    "context_digests",
                    "keyring",
                    "qualified_rater_ids",
                )
            ),
        )
        target = ledger / "records" / f"{correction['record_id']}.json"
        os.chmod(target, 0o644)
        target.write_text('{"tampered": true}', encoding="utf-8")
        check(
            "overwritten record is detected",
            _raises(LedgerError, lambda: load_ledger(ledger)),
        )

    print("E2E-FULL - export -> verify -> prepare -> ledger -> rebuild")
    with tempfile.TemporaryDirectory() as tmp:
        T = Path(tmp)
        # Synthetic corpus so the acceptance E2E runs EVERYWHERE, including CI
        # where runs/ is gitignored. Skipping it would defeat its purpose.
        e2e_runs = _synthetic_runs(T / "corpus")
        DIST2 = "dist-key"
        KEYS = {"r1": "s1", "r2": "s2", "r3": "s3"}
        pkg2, truth2 = build_package(e2e_runs, core_n=None, boost_per_stratum=0, ratings_dir=None)
        write_paired_outputs(pkg2, truth2, T / "pkg.json", T / "truth.json")
        server_contract = pkg2["readiness"]["authentication"]["rating_contract_digest"]
        exported = export_rater_bundle(pkg2, T / "bundle", distribution_secret=DIST2)
        eb = json.loads(exported.read_text(encoding="utf-8"))
        check(
            "ONE canonical contract digest: package == exported bundle",
            server_contract == eb["rating_contract_digest"],
        )
        labels2 = {f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS}
        ledger2 = T / "ledger"
        chosen = [i["item_id"] for i in eb["items"][:2]]
        expected_records = len(chosen) * len(KEYS)
        # An E2E that exercises nothing reports success. Assert it carries load,
        # so a change to the synthetic corpus cannot silently hollow it out.
        check(
            "E2E is not vacuous: it rates real items with real raters",
            len(chosen) >= 2 and len(KEYS) >= 3 and expected_records >= 6,
        )
        for iid in chosen:
            for key_id, secret in KEYS.items():
                prepared = prepare_rating(
                    eb,
                    item_id=iid,
                    labels=labels2,
                    rater_key_id=key_id,
                    rater_secret=secret,
                    distribution_secret=DIST2,
                )
                append_rating_record(
                    ledger2,
                    prepared,
                    created_at=f"T-{iid[:6]}-{key_id}",
                    rating_contract_digest=server_contract,
                    context_digests={i["item_id"]: i["item_context_digest"] for i in eb["items"]},
                    keyring=KEYS,
                    qualified_rater_ids=list(KEYS),
                )
        reg3 = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "authority_key_id": "a",
            "raters": [{"key_id": k, "qualified": True} for k in KEYS],
        }
        reg3["signature"] = sign_registry(reg3, "AUTH")
        (T / "reg.json").write_text(json.dumps(reg3), encoding="utf-8")
        (T / "ks.json").write_text(
            json.dumps({"schema_version": KEYSTORE_SCHEMA_VERSION, "keys": KEYS}),
            encoding="utf-8",
        )
        # A nonempty ledger intake REQUIRES a coordinator-signed external anchor.
        # The coordinator publishes it after the append batch, into the fixed
        # authority root keyed by the contract digest - never a caller path.
        ANCHOR_SECRET = "anchor-trust"
        publish_anchor(
            T / "authority",
            ledger2,
            rating_contract_digest=server_contract,
            secret=ANCHOR_SECRET,
        )
        unanchored, _ = build_package(
            e2e_runs,
            core_n=None,
            boost_per_stratum=0,
            ratings_dir=ledger2,
            registry_path=T / "reg.json",
            authority_secret="AUTH",
            keystore_path=T / "ks.json",
        )
        check(
            "an UNANCHORED nonempty ledger blocks readiness and yields no ratings",
            any(b.startswith("LEDGER_ANCHOR_MISSING") for b in unanchored["readiness"]["blockers"])
            and unanchored["readiness"]["rating_intake"]["records_accepted"] == 0,
        )
        rebuilt, _ = build_package(
            e2e_runs,
            core_n=None,
            boost_per_stratum=0,
            ratings_dir=ledger2,
            registry_path=T / "reg.json",
            authority_secret="AUTH",
            keystore_path=T / "ks.json",
            anchor_root=T / "authority",
            anchor_secret=ANCHOR_SECRET,
        )
        ri2 = rebuilt["readiness"]["rating_intake"]
        check(
            "intake is LEDGER-ONLY; no legacy glob mode exists",
            ri2["intake_mode"] == "ledger"
            and not hasattr(build_labeling_module, "load_rating_records")
            and not hasattr(build_labeling_module, "is_ledger_dir"),
        )
        check(
            "every genuine client rating is ACCEPTED end to end",
            ri2["records_accepted"] == expected_records and ri2["records_rejected"] == 0,
            f"got accepted={ri2['records_accepted']} rejected={ri2['records_rejected']} "
            f"reasons={ri2['primary_rejection_reasons']}",
        )
        counts2 = rebuilt["readiness"]["context_diagnostic_2x2"]["counts"]
        check("diagnostic total equals accepted", sum(counts2.values()) == ri2["records_accepted"])

    print("SEC-SUPERSEDE - correction authorization from the SIGNED record only")
    with tempfile.TemporaryDirectory() as tmp:
        ledger3 = Path(tmp) / "l"
        SC = "c" * 64
        SCTX = {"i1": "d" * 64, "i2": "e" * 64}
        SKR = {"victim": "kv", "attacker": "ka"}
        SVC: dict[str, Any] = {
            "rating_contract_digest": SC,
            "context_digests": SCTX,
            "keyring": SKR,
            "qualified_rater_ids": list(SKR),
        }

        def _rec(
            item: str,
            rater: str,
            sup: str | None = None,
            secret: str | None = None,
            **over: object,
        ) -> dict:
            r = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": SC,
                "item_id": item,
                "item_context_digest": SCTX[item],
                "rater_key_id": rater,
                "supersedes": sup,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
                **over,
            }
            r["signature"] = sign_rating(r, secret or SKR.get(rater, "kx"))
            return r

        victim = append_rating_record(ledger3, _rec("i1", "victim"), created_at="T0", **SVC)
        append_rating_record(ledger3, _rec("i2", "victim"), created_at="T1", **SVC)
        vid = victim["record_id"]
        # THE SEAM ATTACK: the signed record names the victim and the caller adds
        # nothing. While authorization was gated on a separate parameter, this
        # path skipped every check and the effective view dropped the victim.
        check(
            "prepared-record cross-RATER supersede, DEFAULT append, is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger3, _rec("i1", "attacker", vid), created_at="TX", **SVC
                ),
            ),
        )
        check(
            "prepared-record cross-ITEM supersede, DEFAULT append, is refused",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger3, _rec("i2", "victim", vid), created_at="TY", **SVC
                ),
            ),
        )
        check(
            "BOGUS-SIGNATURE correction cannot suppress a valid rating (DoS)",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger3,
                    _rec("i1", "victim", vid, secret="WRONG-SECRET"),
                    created_at="TD",
                    **SVC,
                ),
            ),
        )
        check(
            "UNQUALIFIED rater's correction is refused at append",
            _raises(
                LedgerError,
                lambda: append_rating_record(
                    ledger3, _rec("i1", "rogue", vid, secret="kx"), created_at="TR", **SVC
                ),
            ),
        )
        check("supersedes is inside the signed fields", "supersedes" in RATING_SIGNED_FIELDS)
        append_rating_record(
            ledger3,
            _rec("i1", "victim", vid, step_contribution="HARMFUL"),
            created_at="T3",
            **SVC,
        )
        check(
            "own correction is accepted and the victim record is retained",
            (ledger3 / "records" / f"{vid}.json").is_file()
            and len(effective_ratings(load_ledger(ledger3), **SVC)) == 2,
        )
        # The load path is the second line of defence: even if a correction was
        # admissible when appended, it must not remove anything once it no longer
        # validates - here the rater loses qualification.
        check(
            "a correction by a NOW-UNQUALIFIED rater removes nothing on load",
            effective_ratings(
                load_ledger(ledger3),
                rating_contract_digest=SC,
                context_digests=SCTX,
                keyring=SKR,
                qualified_rater_ids=["attacker"],
            )
            == [],
        )

    print("SEC-ANCHOR - rollback and forks are caught only by a signed anchor")
    with tempfile.TemporaryDirectory() as tmp:
        ledger4 = Path(tmp) / "l"
        ASEC = "anchor-trust"
        AC = "c" * 64
        ACTX = {"i1": "d" * 64}
        AKR = {f"r{n}": f"s{n}" for n in range(6)}
        AVC: dict[str, Any] = {
            "rating_contract_digest": AC,
            "context_digests": ACTX,
            "keyring": AKR,
            "qualified_rater_ids": list(AKR),
        }

        def _r4(rater: str) -> dict:
            r = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": AC,
                "item_id": "i1",
                "item_context_digest": ACTX["i1"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            r["signature"] = sign_rating(r, AKR[rater])
            return r

        for n in range(3):
            append_rating_record(ledger4, _r4(f"r{n}"), created_at=f"T{n}", **AVC)
        good_head, good_count = ledger_head(ledger4)
        anchor = sign_ledger_anchor(
            good_head,
            good_count,
            ASEC,
            ledger=ledger_id(ledger4),
            rating_contract_digest=AC,
        )
        verify_against_anchor(ledger4, anchor, anchor_secret=ASEC, rating_contract_digest=AC)
        check("a matching signed anchor verifies", True)
        check(
            "an anchor signed with the WRONG key is refused",
            _raises(
                LedgerError,
                lambda: verify_against_anchor(
                    ledger4, anchor, anchor_secret="not-the-key", rating_contract_digest=AC
                ),
            ),
        )
        check(
            "a TAMPERED anchor count is refused",
            _raises(
                LedgerError,
                lambda: verify_against_anchor(
                    ledger4,
                    {**anchor, "entry_count": 99},
                    anchor_secret=ASEC,
                    rating_contract_digest=AC,
                ),
            ),
        )
        check(
            "an UNSIGNED anchor is refused",
            _raises(
                LedgerError,
                lambda: verify_against_anchor(
                    ledger4,
                    {k: v for k, v in anchor.items() if k != "signature"},
                    anchor_secret=ASEC,
                    rating_contract_digest=AC,
                ),
            ),
        )

        def _rollback(dst: Path) -> None:
            shutil.copytree(ledger4, dst)
            lines = (dst / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            entry0 = json.loads(lines[0])
            (dst / "ledger.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
            (dst / "head.json").write_text(
                json.dumps(
                    {
                        "schema": LEDGER_SCHEMA,
                        "head_hash": entry0["entry_hash"],
                        "count": 1,
                    }
                ),
                encoding="utf-8",
            )
            for stale in (dst / "records").glob("*.json"):
                if stale.stem != entry0["record_id"]:
                    os.chmod(stale, 0o644)
                    stale.unlink()

        truncated = Path(tmp) / "truncated"
        _rollback(truncated)
        check(
            "a consistently-rewritten rollback passes LOCAL verification",
            len(load_ledger(truncated)) == 1,
        )
        check(
            "the signed anchor DETECTS the truncation as LEDGER_ROLLBACK_DETECTED",
            "LEDGER_ROLLBACK_DETECTED"
            in _err_msg(
                lambda: verify_against_anchor(
                    truncated, anchor, anchor_secret=ASEC, rating_contract_digest=AC
                )
            ),
        )
        # EXACT equality, and the count check runs BEFORE the head comparison.
        # The old code required only `local_count >= anchor_count` with a
        # matching hash at the anchored position, so a STALE anchor verified
        # against a LONGER ledger, leaving every later entry unanchored. That
        # same fork now trips LEDGER_UNANCHORED_SUFFIX rather than
        # LEDGER_FORK_DETECTED, because the counts no longer match.
        extended = Path(tmp) / "extended"
        _rollback(extended)
        for rater, when in (("r3", "LATER"), ("r4", "LATER2"), ("r5", "LATER3")):
            append_rating_record(extended, _r4(rater), created_at=when, **AVC)
        check(
            "a fork re-extended BEYOND the anchored count passes LOCAL verification",
            len(load_ledger(extended)) == 4,
        )
        check(
            "the stale anchor is refused as LEDGER_UNANCHORED_SUFFIX, not FORK_DETECTED",
            "LEDGER_UNANCHORED_SUFFIX"
            in _err_msg(
                lambda: verify_against_anchor(
                    extended, anchor, anchor_secret=ASEC, rating_contract_digest=AC
                )
            ),
        )
        # A fork re-extended to EXACTLY the anchored length passes the count
        # check, so the head comparison is what catches it.
        forked = Path(tmp) / "forked"
        _rollback(forked)
        append_rating_record(forked, _r4("r3"), created_at="LATER", **AVC)
        append_rating_record(forked, _r4("r4"), created_at="LATER2", **AVC)
        check(
            "a fork at EXACTLY the anchored length passes LOCAL verification",
            len(load_ledger(forked)) == 3,
        )
        check(
            "the signed anchor DETECTS an equal-length fork as LEDGER_FORK_DETECTED",
            "LEDGER_FORK_DETECTED"
            in _err_msg(
                lambda: verify_against_anchor(
                    forked, anchor, anchor_secret=ASEC, rating_contract_digest=AC
                )
            ),
        )
        check(
            "a ledger with records but NO head manifest fails closed",
            _raises(LedgerError, lambda: ledger_head(_headless(ledger4, Path(tmp) / "headless"))),
        )

    print("SEC-ANCHOR-REPLAY - the publication log makes stale anchors unreplayable")
    with tempfile.TemporaryDirectory() as tmp:
        R = Path(tmp)
        root = R / "authority"
        ledger = R / "ratings" / "l"
        SEC = "replay-secret"
        RCON = "c" * 64
        RCON2 = "d" * 64
        RCTX = {"i1": "d" * 64}
        RKR = {f"r{n}": f"s{n}" for n in range(1, 7)}
        RVC: dict[str, Any] = {
            "rating_contract_digest": RCON,
            "context_digests": RCTX,
            "keyring": RKR,
            "qualified_rater_ids": list(RKR),
        }

        def _rrec(rater: str, created_at: str) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": RCON,
                "item_id": "i1",
                "item_context_digest": RCTX["i1"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            body["signature"] = sign_rating(body, RKR[rater])
            return body

        # Batch 1: three genuine ratings, then the coordinator publishes.
        for n in range(1, 4):
            append_rating_record(ledger, _rrec(f"r{n}", f"T{n}"), created_at=f"T{n}", **RVC)
        head3, count3 = ledger_head(ledger)
        publish_anchor(root, ledger, rating_contract_digest=RCON, secret=SEC)
        recs1, _mode1, probs1 = load_intake(ledger, anchor_root=root, anchor_secret=SEC, **RVC)
        check(
            "a current-head anchor accepts the published batch",
            len(recs1) == 3 and not probs1,
            f"got {len(recs1)} records, {probs1}",
        )

        # Batch 2: three more ratings WITHOUT republishing. The anchor still names
        # the old head/count, so the suffix is refused outright.
        for n in range(4, 7):
            append_rating_record(ledger, _rrec(f"r{n}", f"T{n}"), created_at=f"T{n}", **RVC)
        recs2, _mode2, probs2 = load_intake(ledger, anchor_root=root, anchor_secret=SEC, **RVC)
        check(
            "an unrepublished suffix is refused as LEDGER_UNANCHORED_SUFFIX",
            recs2 == [] and any("LEDGER_UNANCHORED_SUFFIX" in p for p in probs2),
            f"got {len(recs2)} records, {probs2}",
        )

        # Legitimate-path canary: republishing admits the whole 6-entry ledger.
        # Without this, a guard that refused EVERY intake would pass cases 2, 4
        # and 5 while silently destroying honest intake.
        publish_anchor(root, ledger, rating_contract_digest=RCON, secret=SEC)
        recs3, _mode3, probs3 = load_intake(ledger, anchor_root=root, anchor_secret=SEC, **RVC)
        check(
            "republishing after the batch admits ALL entries with no problems",
            len(recs3) == 6 and not probs3,
            f"got {len(recs3)} records, {probs3}",
        )

        # STALE-PREFIX REPLAY: a VALIDLY re-signed anchor for the OLD head/count
        # is written over the current one. The signature verifies - the append-only
        # publication log is what refuses it, because 3 < highest published 6.
        stale_anchor = sign_ledger_anchor(
            head3,
            count3,
            SEC,
            ledger=ledger_id(ledger),
            rating_contract_digest=RCON,
        )
        (root / f"{RCON}.anchor.json").write_text(json.dumps(stale_anchor), encoding="utf-8")
        recs4, _mode4, probs4 = load_intake(ledger, anchor_root=root, anchor_secret=SEC, **RVC)
        check(
            "a stale re-signed anchor is refused as ANCHOR_ROLLED_BACK",
            recs4 == [] and any("ANCHOR_ROLLED_BACK" in p for p in probs4),
            f"got {len(recs4)} records, {probs4}",
        )

        # TRUNCATE-TO-OLD-HEAD: the ledger itself is cut back to the old head and
        # head.json rewritten consistently, orphaned record files removed. Locally
        # valid - only the monotonic log still says 6 were published.
        trunc = R / "truncated"
        shutil.copytree(ledger, trunc)
        lines = (trunc / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        kept_ids = {json.loads(line)["record_id"] for line in lines[:3]}
        (trunc / "ledger.jsonl").write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
        (trunc / "head.json").write_text(
            json.dumps({"schema": LEDGER_SCHEMA, "head_hash": head3, "count": 3}),
            encoding="utf-8",
        )
        for stale in (trunc / "records").glob("*.json"):
            if stale.stem not in kept_ids:
                stale.chmod(0o644)
                stale.unlink()
        check(
            "the truncated copy is locally consistent (canary)",
            len(load_ledger(trunc)) == 3,
        )
        (root / f"{RCON}.anchor.json").write_text(
            json.dumps(
                sign_ledger_anchor(
                    head3,
                    count3,
                    SEC,
                    ledger=ledger_id(trunc),
                    rating_contract_digest=RCON,
                )
            ),
            encoding="utf-8",
        )
        recs5, _mode5, probs5 = load_intake(trunc, anchor_root=root, anchor_secret=SEC, **RVC)
        check(
            "a ledger truncated to the old head is refused as ANCHOR_ROLLED_BACK",
            recs5 == [] and any("ANCHOR_ROLLED_BACK" in p for p in probs5),
            f"got {len(recs5)} records, {probs5}",
        )

        # The caller-selectable path is GONE, and the fixed default is what a
        # production caller without an explicit root actually gets.
        sig_params = inspect.signature(load_intake).parameters
        check(
            "load_intake has NO caller-supplied anchor_path parameter",
            "anchor_path" not in sig_params,
        )
        check(
            "the production default root is the fixed home path",
            Path.home() / ".goldset" / "anchors" == build_labeling_module.DEFAULT_ANCHOR_ROOT,
        )
        # Re-publish so the anchor file is current before exercising the default.
        publish_anchor(root, ledger, rating_contract_digest=RCON, secret=SEC)
        saved_default = build_labeling_module.DEFAULT_ANCHOR_ROOT
        try:
            build_labeling_module.DEFAULT_ANCHOR_ROOT = root
            recs6, _mode6, probs6 = load_intake(
                ledger,
                anchor_secret=SEC,
                **RVC,  # anchor_root omitted on purpose
            )
        finally:
            build_labeling_module.DEFAULT_ANCHOR_ROOT = saved_default
        check(
            "omitting anchor_root uses the FIXED DEFAULT_ANCHOR_ROOT",
            len(recs6) == 6 and not probs6,
            f"got {len(recs6)} records, {probs6}",
        )

        # ANCHOR ROOT HARDENING: the "external" root must actually be outside the
        # rater-writable ratings tree - not inside it, not a symlink, not
        # group/world-writable, and present at all.
        check(
            "an anchor root INSIDE the ratings tree is refused",
            "ANCHOR_ROOT_INSIDE_RATINGS_TREE"
            in _err_msg(
                lambda: resolve_anchor(ledger, rating_contract_digest=RCON, ratings_dir=ledger)
            ),
        )
        real_dir = R / "real_dir"
        real_dir.mkdir()
        link_root = R / "authority_link"
        link_root.symlink_to(real_dir)
        check(
            "a symlinked anchor root is refused",
            "ANCHOR_ROOT_IS_SYMLINK"
            in _err_msg(
                lambda: resolve_anchor(link_root, rating_contract_digest=RCON, ratings_dir=ledger)
            ),
        )
        writable_root = R / "writable_authority"
        writable_root.mkdir()
        writable_root.chmod(0o777)
        check(
            "a group/world-writable anchor root is refused",
            "ANCHOR_ROOT_GROUP_OR_WORLD_WRITABLE"
            in _err_msg(
                lambda: resolve_anchor(
                    writable_root, rating_contract_digest=RCON, ratings_dir=ledger
                )
            ),
        )
        check(
            "an absent anchor root is refused",
            "ANCHOR_ROOT_ABSENT"
            in _err_msg(
                lambda: resolve_anchor(
                    R / "no_such_root", rating_contract_digest=RCON, ratings_dir=ledger
                )
            ),
        )

        # CROSS-LEDGER and CROSS-CAMPAIGN: the anchor is bound to BOTH the ledger
        # identity and the contract digest, so it cannot be replayed against a
        # different ledger with the SAME entry count (identity fires, not count),
        # nor against a different campaign.
        other_ledger = R / "ratings" / "other"
        for n in range(1, 7):
            append_rating_record(other_ledger, _rrec(f"r{n}", f"U{n}"), created_at=f"U{n}", **RVC)
        root2 = R / "authority2"
        publish_anchor(root2, ledger, rating_contract_digest=RCON, secret=SEC)
        recsC, _modeC, probsC = load_intake(ledger, anchor_root=root2, anchor_secret=SEC, **RVC)
        check(
            "the anchor verifies against its OWN ledger (control)",
            len(recsC) == 6 and not probsC,
            f"got {len(recsC)} records, {probsC}",
        )
        recsX, _modeX, probsX = load_intake(
            other_ledger, anchor_root=root2, anchor_secret=SEC, **RVC
        )
        check(
            "a same-count OTHER ledger is refused as ANCHOR_LEDGER_MISMATCH",
            recsX == [] and any("ANCHOR_LEDGER_MISMATCH" in p for p in probsX),
            f"got {len(recsX)} records, {probsX}",
        )
        head6, count6 = ledger_head(ledger)
        (root2 / f"{RCON}.anchor.json").write_text(
            json.dumps(
                sign_ledger_anchor(
                    head6,
                    count6,
                    SEC,
                    ledger=ledger_id(ledger),
                    rating_contract_digest=RCON2,
                )
            ),
            encoding="utf-8",
        )
        recsY, _modeY, probsY = load_intake(ledger, anchor_root=root2, anchor_secret=SEC, **RVC)
        check(
            "an anchor for a DIFFERENT campaign is refused as ANCHOR_CONTRACT_MISMATCH",
            recsY == [] and any("ANCHOR_CONTRACT_MISMATCH" in p for p in probsY),
            f"got {len(recsY)} records, {probsY}",
        )

    print("SEC-DOWNGRADE - there is no non-ledger intake to fall back to")
    with tempfile.TemporaryDirectory() as tmp:
        DC = "c" * 64
        DCTX = {"i1": "d" * 64}
        DKR = {f"r{n}": f"s{n}" for n in range(4)}
        DVC: dict[str, Any] = {
            "rating_contract_digest": DC,
            "context_digests": DCTX,
            "keyring": DKR,
            "qualified_rater_ids": list(DKR),
        }

        def _drec(rater: str, supersedes: str | None = None) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": DC,
                "item_id": "i1",
                "item_context_digest": DCTX["i1"],
                "rater_key_id": rater,
                "supersedes": supersedes,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            body["signature"] = sign_rating(body, DKR[rater])
            return body

        dled = Path(tmp) / "l"
        for n in range(4):
            append_rating_record(dled, _drec(f"r{n}"), created_at=f"T{n}", **DVC)
        check(
            "an intact but UNANCHORED ledger yields nothing",
            load_intake(dled, anchor_root=None, anchor_secret=None, **DVC)[0] == [],
        )
        # THE DOWNGRADE: remove every marker the old code sniffed for. Selecting an
        # intake path by looking for markers is bypassable by deleting them, so the
        # choice itself was removed rather than the heuristic improved.
        (dled / "head.json").unlink()
        (dled / "ledger.jsonl").unlink()
        drecs, dmode, dprobs = load_intake(dled, anchor_root=None, anchor_secret=None, **DVC)
        check(
            "removing ALL ledger markers refuses instead of downgrading",
            drecs == []
            and dmode == "ledger"
            and any(p.startswith("LEDGER_INCOMPLETE") for p in dprobs),
        )
        flat = Path(tmp) / "flat"
        flat.mkdir()
        for src in (dled / "records").glob("*.json"):
            shutil.copy(src, flat / src.name)
        frecs, fmode, fprobs = load_intake(flat, anchor_root=None, anchor_secret=None, **DVC)
        check(
            "a FLAT dump of record files is refused, not ingested",
            frecs == []
            and fmode == "ledger"
            and any(p.startswith("LEDGER_INCOMPLETE") for p in fprobs),
        )
        check(
            "no ratings_dir means zero ratings, not an alternative path",
            load_intake(None, **DVC)[:2] == ([], "no_ratings_dir"),
        )

    print("SEC-CRASH - an interrupted append is recoverable, never bricking")
    with tempfile.TemporaryDirectory() as tmp:
        XC = "c" * 64
        XCTX = {"i1": "d" * 64}
        XKR = {f"r{n}": f"s{n}" for n in range(4)}
        XVC: dict[str, Any] = {
            "rating_contract_digest": XC,
            "context_digests": XCTX,
            "keyring": XKR,
            "qualified_rater_ids": list(XKR),
        }

        def _xrec(rater: str) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": XC,
                "item_id": "i1",
                "item_context_digest": XCTX["i1"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            body["signature"] = sign_rating(body, XKR[rater])
            return body

        def _xbuild(dst: Path) -> Path:
            for n in range(3):
                append_rating_record(dst, _xrec(f"r{n}"), created_at=f"T{n}", **XVC)
            return dst

        # WINDOW 2: the log entry landed, the head update did not.
        w2 = _xbuild(Path(tmp) / "w2")
        entries = (w2 / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        second = json.loads(entries[1])
        (w2 / "head.json").write_text(
            json.dumps(
                {
                    "schema": LEDGER_SCHEMA,
                    "head_hash": second["entry_hash"],
                    "count": 2,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        check(
            "a head behind the log is TYPED recoverable, not fatal",
            _raises(LedgerRecoverableError, lambda: load_ledger(w2)),
        )
        check(
            "intake reports it as actionable rather than repairing silently",
            any(
                p.startswith("LEDGER_NEEDS_REPAIR")
                for p in load_intake(w2, anchor_root=None, anchor_secret=None, **XVC)[2]
            ),
        )
        check(
            "explicit repair rolls the head forward and the ledger reloads clean",
            len(load_ledger(w2, repair=True)) == 3 and len(load_ledger(w2)) == 3,
        )

        # WINDOW 1: the record file landed, the log entry did not.
        w1 = _xbuild(Path(tmp) / "w1")
        orphan = dict(_xrec("r3"))
        orphan["created_at"] = "TX"
        orphan["previous_entry_hash"] = ledger_head(w1)[0]
        orphan_id = _record_id_for_test(orphan)
        orphan["record_id"] = orphan_id
        (w1 / "records" / f"{orphan_id}.json").write_text(
            json.dumps(orphan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        check(
            "a verifiable uncommitted record is TYPED recoverable, not fatal",
            _raises(LedgerRecoverableError, lambda: load_ledger(w1)),
        )
        check(
            "explicit repair quarantines it and the ledger reloads clean",
            len(load_ledger(w1, repair=True)) == 3
            and (w1 / "uncommitted" / f"{orphan_id}.json").is_file()
            and len(load_ledger(w1)) == 3,
        )

        # Corruption is NOT an interrupted append and must not be repaired.
        junk = _xbuild(Path(tmp) / "junk")
        (junk / "records" / "deadbeef.json").write_text('{"junk": true}', encoding="utf-8")
        check(
            "unverifiable junk fails hard even with repair requested",
            _raises(LedgerError, lambda: load_ledger(junk, repair=True))
            and not _raises(LedgerRecoverableError, lambda: load_ledger(junk, repair=True)),
        )
        clean = _xbuild(Path(tmp) / "clean")
        with _read_lock_for_test(clean), _read_lock_for_test(clean):
            check(
                "concurrent SHARED readers coexist without blocking",
                len(load_ledger(clean)) == 3,
            )

    print("SEC-GRAPH - resolution re-authorizes every edge it did not create")
    with tempfile.TemporaryDirectory() as tmp:
        GC = "c" * 64
        GCTX = {"i1": "d" * 64, "i2": "e" * 64}
        GKR = {f"r{n}": f"s{n}" for n in range(4)}
        GVC: dict[str, Any] = {
            "rating_contract_digest": GC,
            "context_digests": GCTX,
            "keyring": GKR,
            "qualified_rater_ids": list(GKR),
        }

        def _grec(rater: str, item: str = "i1", sup: str | None = None, **over) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": GC,
                "item_id": item,
                "item_context_digest": GCTX[item],
                "rater_key_id": rater,
                "supersedes": sup,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
                **over,
            }
            body["signature"] = sign_rating(body, GKR[rater])
            return body

        def _handcraft(dst: Path, bodies: list[dict]) -> Path:
            """Write a STRUCTURALLY VALID ledger without ever calling append.

            Append-time authorization cannot protect this path, which is the whole
            point: a ledger may be handcrafted or migrated, and an anchored one.
            """
            (dst / "records").mkdir(parents=True)
            previous = GENESIS_HASH
            lines = []
            for n, body in enumerate(bodies, start=1):
                stored = dict(body)
                stored["created_at"] = f"H{n}"
                stored["previous_entry_hash"] = previous
                rid = _record_id_for_test(stored)
                stored["record_id"] = rid
                (dst / "records" / f"{rid}.json").write_text(
                    json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                entry_hash = _entry_hash_for_test(previous, rid, f"H{n}")
                lines.append(
                    json.dumps(
                        {
                            "schema": LEDGER_SCHEMA,
                            "seq": n,
                            "record_id": rid,
                            "created_at": f"H{n}",
                            "previous_entry_hash": previous,
                            "entry_hash": entry_hash,
                            "supersedes": stored.get("supersedes"),
                        },
                        sort_keys=True,
                    )
                )
                previous = entry_hash
            (dst / "ledger.jsonl").write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8"
            )
            (dst / "head.json").write_text(
                json.dumps(
                    {"schema": LEDGER_SCHEMA, "head_hash": previous, "count": len(lines)},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return dst

        def _first_id(body: dict) -> str:
            stored = dict(body)
            stored["created_at"] = "H1"
            stored["previous_entry_hash"] = GENESIS_HASH
            return _record_id_for_test(stored)

        victim = _grec("r0")
        vid = _first_id(victim)
        for label, bodies in (
            ("cross-RATER", [victim, _grec("r1", sup=vid)]),
            ("cross-ITEM", [victim, _grec("r0", item="i2", sup=vid)]),
            ("DOUBLE supersede", [victim, _grec("r0", sup=vid), _grec("r1", sup=vid)]),
        ):
            ledger = _handcraft(Path(tmp) / label.replace(" ", "_"), bodies)
            resolved = effective_ratings(load_ledger(ledger), **GVC)
            check(
                f"handcrafted {label} edge is rejected and the victim SURVIVES",
                vid in {r["record_id"] for r in resolved} and len(resolved) == 1,
            )
        # And the legitimate cases must still resolve: a guard that rejects every
        # edge would pass all three attacks above while breaking corrections.
        legit = _handcraft(
            Path(tmp) / "legit",
            [victim, _grec("r0", sup=vid, step_contribution="HARMFUL")],
        )
        resolved = effective_ratings(load_ledger(legit), **GVC)
        check(
            "a legitimate handcrafted correction still WINS",
            len(resolved) == 1 and resolved[0]["step_contribution"] == "HARMFUL",
        )
        step2 = _grec("r0", sup=vid, step_contribution="HARMFUL")
        bid = _record_id_for_test(
            {
                **step2,
                "created_at": "H2",
                "previous_entry_hash": _entry_hash_for_test(GENESIS_HASH, vid, "H1"),
            }
        )
        chain = _handcraft(
            Path(tmp) / "chain",
            [victim, step2, _grec("r0", sup=bid, step_contribution="NEUTRAL")],
        )
        resolved = effective_ratings(load_ledger(chain), **GVC)
    print("SEC-INTAKE-DOS - every read/parse failure is a fail-closed diagnostic, never a crash")
    with tempfile.TemporaryDirectory() as tmp:
        DC = "c" * 64
        DCTX = {"i1": "d" * 64}
        DKR = {f"r{n}": f"s{n}" for n in range(1, 4)}
        DVC: dict[str, Any] = {
            "rating_contract_digest": DC,
            "context_digests": DCTX,
            "keyring": DKR,
            "qualified_rater_ids": list(DKR),
        }
        ASEC = "dos-anchor"

        def _dos_rec(rater: str) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": DC,
                "item_id": "i1",
                "item_context_digest": DCTX["i1"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            body["signature"] = sign_rating(body, DKR[rater])
            return body

        dos_ledger = Path(tmp) / "ledger"
        for n in range(3):
            append_rating_record(dos_ledger, _dos_rec(f"r{n + 1}"), created_at=f"T{n}", **DVC)
        dos_head, dos_count = ledger_head(dos_ledger)
        # The anchor is PUBLISHED under a coordinator root keyed by the contract
        # digest; intake looks it up there. A caller-supplied anchor file path no
        # longer exists, so every mutation probe works on copies of the root.
        AROOT_DOS = Path(tmp) / "authority"
        publish_anchor(AROOT_DOS, dos_ledger, rating_contract_digest=DC, secret=ASEC)
        first_rid = sorted((dos_ledger / "records").glob("*.json"))[0].stem

        def _fresh(name: str) -> Path:
            dst = Path(tmp) / name
            shutil.copytree(dos_ledger, dst)
            return dst

        def _fresh_root(name: str) -> Path:
            root = Path(tmp) / f"root_{name}"
            shutil.copytree(AROOT_DOS, root)
            return root

        def _rewrite(path: Path, content: str | bytes) -> None:
            # Record files are written 0444; the mutation needs write access first.
            path.chmod(0o644)
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_bytes(content)

        def _refused(fn: Callable[[], tuple[list, list]]) -> bool:
            # A mutation is contained only if intake returns ZERO records AND a
            # diagnostic. A raise, or a silent partial acceptance, is a failure.
            try:
                records, problems = fn()
            except Exception:
                return False
            return records == [] and bool(problems)

        def _probe(ledger_dir: Path, anchor_root: Path) -> tuple[list, list]:
            records, _mode, problems = load_intake(
                ledger_dir,
                anchor_root=anchor_root,
                anchor_secret=ASEC,
                **DVC,
            )
            return records, problems

        def _head_mutated(name: str, content: str | bytes) -> Callable[[], bool]:
            d = _fresh(name)
            _rewrite(d / "head.json", content)
            return lambda: _refused(lambda: _probe(d, AROOT_DOS))

        check("head malformed JSON refuses, never raises", _head_mutated("m1", "{not json")())
        check("head scalar JSON refuses, never raises", _head_mutated("m2", "42")())
        check("head list JSON refuses, never raises", _head_mutated("m3", "[1,2]")())
        check(
            "head invalid UTF-8 refuses, never raises",
            _head_mutated("m6", b"\xff\xfe bad")(),
        )

        def _head_deleted() -> bool:
            d = _fresh("m4")
            (d / "head.json").unlink()
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("head deleted refuses, never raises", _head_deleted())

        def _head_unreadable() -> bool:
            d = _fresh("m5")
            (d / "head.json").chmod(0o000)
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("head unreadable refuses, never raises", _head_unreadable())

        # record files: malformed, scalar, unreadable, not UTF-8, deleted.
        def _record_mutated(name: str, content: str | bytes) -> Callable[[], bool]:
            d = _fresh(name)
            _rewrite(d / "records" / f"{first_rid}.json", content)
            return lambda: _refused(lambda: _probe(d, AROOT_DOS))

        check(
            "record malformed JSON refuses, never raises",
            _record_mutated("r1", "{not json")(),
        )
        check("record scalar JSON refuses, never raises", _record_mutated("r2", "42")())
        check(
            "record invalid UTF-8 refuses, never raises",
            _record_mutated("r4", b"\xff\xfe bad")(),
        )

        def _record_unreadable() -> bool:
            d = _fresh("r3")
            (d / "records" / f"{first_rid}.json").chmod(0o000)
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("record unreadable refuses, never raises", _record_unreadable())

        def _record_deleted() -> bool:
            d = _fresh("r5")
            (d / "records" / f"{first_rid}.json").unlink()
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("record deleted refuses, never raises", _record_deleted())

        # ledger.jsonl: not UTF-8, deleted, unreadable.
        def _log_mutated(name: str, content: str | bytes) -> Callable[[], bool]:
            d = _fresh(name)
            _rewrite(d / "ledger.jsonl", content)
            return lambda: _refused(lambda: _probe(d, AROOT_DOS))

        check(
            "log invalid UTF-8 refuses, never raises",
            _log_mutated("g1", b"\xff\xfe bad")(),
        )

        def _log_deleted() -> bool:
            d = _fresh("g2")
            (d / "ledger.jsonl").unlink()
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("log deleted refuses, never raises", _log_deleted())

        def _log_unreadable() -> bool:
            d = _fresh("g3")
            (d / "ledger.jsonl").chmod(0o000)
            return _refused(lambda: _probe(d, AROOT_DOS))

        check("log unreadable refuses, never raises", _log_unreadable())

        # anchor file: malformed, scalar, list, not UTF-8, deleted, unreadable.
        def _anchor_case(name: str, content: str | bytes) -> Callable[[], bool]:
            root = _fresh_root(name)
            _rewrite(root / f"{DC}.anchor.json", content)
            return lambda: _refused(lambda: _probe(_fresh(f"am_{name}"), root))

        check(
            "anchor malformed JSON refuses, never raises", _anchor_case("malformed", "{not json")()
        )
        check("anchor scalar JSON refuses, never raises", _anchor_case("scalar", "42")())
        check("anchor list JSON refuses, never raises", _anchor_case("list", "[1,2]")())
        check("anchor invalid UTF-8 refuses, never raises", _anchor_case("utf8", b"\xff\xfe bad")())

        def _anchor_deleted() -> bool:
            root = _fresh_root("deleted")
            (root / f"{DC}.anchor.json").unlink()
            return _refused(lambda: _probe(_fresh("am_deleted"), root))

        check("anchor deleted refuses, never raises", _anchor_deleted())

        def _anchor_unreadable() -> bool:
            root = _fresh_root("unreadable")
            (root / f"{DC}.anchor.json").chmod(0o000)
            return _refused(lambda: _probe(_fresh("am_unreadable"), root))

        check("anchor unreadable refuses, never raises", _anchor_unreadable())

        # An UNHASHABLE rater_key_id must fail the whole intake closed, exactly as
        # an unparseable record file does, instead of being silently dropped while
        # the rest of the ledger ships. The ledger is handcrafted (append would
        # refuse the record) but structurally valid: the chain and head verify.
        def _handcraft_unhashable(name: str, bad_value: object) -> tuple[Path, Path]:
            d = Path(tmp) / name
            d.mkdir()
            records_dir = d / "records"
            records_dir.mkdir()
            bodies = [
                {**_dos_rec("r1"), "rater_key_id": bad_value},
                _dos_rec("r2"),
                _dos_rec("r3"),
            ]
            previous = GENESIS_HASH
            entries: list[dict[str, Any]] = []
            for idx, body in enumerate(bodies):
                stored = dict(body)
                stored["created_at"] = f"H{idx}"
                stored["previous_entry_hash"] = previous
                rid = _record_id_for_test(stored)
                stored["record_id"] = rid
                (records_dir / f"{rid}.json").write_text(
                    json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                ehash = _entry_hash_for_test(previous, rid, f"H{idx}")
                entries.append(
                    {
                        "schema": LEDGER_SCHEMA,
                        "seq": idx + 1,
                        "record_id": rid,
                        "created_at": f"H{idx}",
                        "previous_entry_hash": previous,
                        "entry_hash": ehash,
                        "supersedes": None,
                    }
                )
                previous = ehash
            (d / "ledger.jsonl").write_text(
                "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n",
                encoding="utf-8",
            )
            (d / "head.json").write_text(
                json.dumps(
                    {"schema": LEDGER_SCHEMA, "head_hash": previous, "count": 3},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            # The anchor is published into a FRESH root copy so the shared DOS
            # root stays pristine: this handcrafted ledger is a DIFFERENT ledger
            # (its records were never appended) under the same contract.
            root = _fresh_root(f"hc_{name}")
            publish_anchor(root, d, rating_contract_digest=DC, secret=ASEC)
            return d, root

        for label, hostile in (("LIST", ["r1"]), ("DICT", {"k": "v"})):
            d, root = _handcraft_unhashable(f"h_{label}", hostile)
            check(
                f"unhashable rater_key_id {label} fails the WHOLE intake closed",
                _refused(lambda d=d, root=root: _probe(d, root)),
            )

        # THE BYPASS: a signed three-rater loose-JSON directory. Each record is
        # individually VALID against the very contract this build produces - the
        # container, not the content, must be what refuses it. The deleted glob
        # path must not be resurrectable by supplying well-formed ratings.
        dos_runs = _synthetic_runs(Path(tmp) / "corpus")
        base, _ = build_package(dos_runs, core_n=None, boost_per_stratum=0, ratings_dir=None)
        doC = base["readiness"]["authentication"]["rating_contract_digest"]
        target_item = base["items"][0]
        doCtx = {i["item_id"]: i["item_context_digest"] for i in base["items"]}
        doKR = {"r1": "s1", "r2": "s2", "r3": "s3"}
        doVC: dict[str, Any] = {
            "rating_contract_digest": doC,
            "context_digests": doCtx,
            "keyring": doKR,
            "qualified_rater_ids": list(doKR),
        }

        def _flat_rec(rater: str) -> dict:
            body = {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": doC,
                "item_id": target_item["item_id"],
                "item_context_digest": target_item["item_context_digest"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }
            body["signature"] = sign_rating(body, doKR[rater])
            return body

        three = [_flat_rec(f"r{n}") for n in (1, 2, 3)]
        check(
            "the three loose records are individually VALID (non-vacuity)",
            all(not validate_rating(dict(r), **doVC) for r in three),
        )
        flat = Path(tmp) / "flat"
        flat.mkdir()
        for n, r in enumerate(three):
            (flat / f"rating-{n}.json").write_text(
                json.dumps(r, indent=2, sort_keys=True), encoding="utf-8"
            )
        dos_reg = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "authority_key_id": "a",
            "raters": [{"key_id": k, "qualified": True} for k in doKR],
        }
        dos_reg["signature"] = sign_registry(dos_reg, "AUTH")
        (Path(tmp) / "reg.json").write_text(json.dumps(dos_reg), encoding="utf-8")
        (Path(tmp) / "ks.json").write_text(
            json.dumps({"schema_version": KEYSTORE_SCHEMA_VERSION, "keys": doKR}),
            encoding="utf-8",
        )
        flat_pkg, _ = build_package(
            dos_runs,
            core_n=None,
            boost_per_stratum=0,
            ratings_dir=flat,
            registry_path=Path(tmp) / "reg.json",
            authority_secret="AUTH",
            keystore_path=Path(tmp) / "ks.json",
        )
        flat_ri = flat_pkg["readiness"]["rating_intake"]
        check(
            "a signed 3-rater loose-JSON directory CANNOT reach READY",
            flat_ri["records_accepted"] == 0
            and any(b.startswith("LEDGER_INCOMPLETE") for b in flat_pkg["readiness"]["blockers"])
            and flat_pkg["readiness"]["readiness"] != "READY",
        )
        # The canary: the SAME records in a proper anchored ledger ARE accepted, so
        # the refusal above is about the container, not the content. A guard that
        # rejects every intake would pass both directions vacuously.
        good_ledger = Path(tmp) / "good"
        for n, r in enumerate(three):
            append_rating_record(
                good_ledger,
                r,
                created_at=f"G{n}",
                rating_contract_digest=doC,
                context_digests=doCtx,
                keyring=doKR,
                qualified_rater_ids=list(doKR),
            )
        good_root = Path(tmp) / "authority_good"
        publish_anchor(good_root, good_ledger, rating_contract_digest=doC, secret=ASEC)
        good_pkg, _ = build_package(
            dos_runs,
            core_n=None,
            boost_per_stratum=0,
            ratings_dir=good_ledger,
            registry_path=Path(tmp) / "reg.json",
            authority_secret="AUTH",
            keystore_path=Path(tmp) / "ks.json",
            anchor_root=good_root,
            anchor_secret=ASEC,
        )
        good_ri = good_pkg["readiness"]["rating_intake"]
        check(
            "the SAME records in an anchored ledger ARE accepted (container, not content)",
            good_ri["records_accepted"] == 3,
            f"got {good_ri['records_accepted']}",
        )

    print("SEC-STRUCT - wrong TYPE is corruption; wrong VALUE is just invalid")
    with tempfile.TemporaryDirectory() as tmp:
        TC = "c" * 64
        TCTX = {"i1": "d" * 64}
        TKR = {"r0": "s0", "r1": "s1"}
        TVC: dict[str, Any] = {
            "rating_contract_digest": TC,
            "context_digests": TCTX,
            "keyring": TKR,
            "qualified_rater_ids": list(TKR),
        }
        # Coordinator authority root, OUTSIDE every ledger dir, keyed by TC.
        AROOT = Path(tmp) / "authority"

        def _tbase(rater: str) -> dict:
            return {
                "schema_version": RATING_SCHEMA_VERSION,
                "rating_contract_digest": TC,
                "item_id": "i1",
                "item_context_digest": TCTX["i1"],
                "rater_key_id": rater,
                "supersedes": None,
                **{f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS},
            }

        def _tledger(dst: Path, bodies: list[dict]) -> Path:
            (dst / "records").mkdir(parents=True)
            previous = GENESIS_HASH
            lines = []
            for n, body in enumerate(bodies, start=1):
                stored = dict(body)
                stored["created_at"] = f"H{n}"
                stored["previous_entry_hash"] = previous
                rid = _record_id_for_test(stored)
                stored["record_id"] = rid
                (dst / "records" / f"{rid}.json").write_text(
                    json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                entry_hash = _entry_hash_for_test(previous, rid, f"H{n}")
                lines.append(
                    json.dumps(
                        {
                            "schema": LEDGER_SCHEMA,
                            "seq": n,
                            "record_id": rid,
                            "created_at": f"H{n}",
                            "previous_entry_hash": previous,
                            "entry_hash": entry_hash,
                            "supersedes": stored.get("supersedes"),
                        },
                        sort_keys=True,
                    )
                )
                previous = entry_hash
            (dst / "ledger.jsonl").write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8"
            )
            (dst / "head.json").write_text(
                json.dumps(
                    {"schema": LEDGER_SCHEMA, "head_hash": previous, "count": len(lines)},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            publish_anchor(AROOT, dst, rating_contract_digest=TC, secret="A")
            return dst

        typed_fields = [
            "schema_version",
            "rating_contract_digest",
            "item_id",
            "item_context_digest",
            "rater_key_id",
            "signature",
            "supersedes",
            *HUMAN_JUDGED_FIELDS,
        ]
        leaks = []
        for index, field in enumerate(typed_fields):
            for shape, value in (("list", ["x"]), ("dict", {"k": "v"})):
                honest = _tbase("r0")
                honest["signature"] = sign_rating(honest, "s0")
                hostile = _tbase("r1")
                hostile[field] = value
                hostile["signature"] = (
                    value if field == "signature" else sign_rating(_tbase("r1"), "s1")
                )
                if field != "signature":
                    hostile[field] = value
                led = _tledger(Path(tmp) / f"t{index}{shape}", [honest, hostile])
                got, _mode, probs = load_intake(led, anchor_root=AROOT, anchor_secret="A", **TVC)
                if got or not probs:
                    leaks.append(f"{field}/{shape}")
        check(
            "a container in ANY string field refuses the whole intake",
            not leaks,
        )
        check(
            "the type check is exercised across every field, not one",
            len(typed_fields) * 2 == 22,
        )
        # THE OTHER DIRECTION. A guard that refused everything would pass the
        # check above while destroying honest intake, so both cases below must
        # still behave exactly as before.
        honest = _tbase("r0")
        honest["signature"] = sign_rating(honest, "s0")
        wrong_value = _tbase("r1")
        wrong_value["step_contribution"] = "NOT_A_REAL_LABEL"
        wrong_value["signature"] = sign_rating(wrong_value, "s1")
        led = _tledger(Path(tmp) / "value", [honest, wrong_value])
        got, _mode, probs = load_intake(led, anchor_root=AROOT, anchor_secret="A", **TVC)
        check(
            "a wrong VALUE only drops that rating; the honest one survives",
            len(got) == 1 and not probs,
        )
        clean = Path(tmp) / "clean"
        for rater, secret in TKR.items():
            body = _tbase(rater)
            body["signature"] = sign_rating(body, secret)
            append_rating_record(clean, body, created_at=f"T{rater}", **TVC)
        publish_anchor(AROOT, clean, rating_contract_digest=TC, secret="A")
        got, _mode, probs = load_intake(clean, anchor_root=AROOT, anchor_secret="A", **TVC)
        check(
            "a fully honest ledger is completely unaffected",
            len(got) == len(TKR) and not probs,
        )

    print("CLI-GATE - the shipped entry point enforces the anchor")
    with tempfile.TemporaryDirectory() as tmp:
        T = Path(tmp)
        cli_runs = _synthetic_runs(T / "corpus")
        CSEC = "cli-anchor"
        cpkg, ctruth = build_package(cli_runs, core_n=None, boost_per_stratum=0, ratings_dir=None)
        write_paired_outputs(cpkg, ctruth, T / "p.json", T / "t.json")
        cex = export_rater_bundle(cpkg, T / "b", distribution_secret="D")
        ceb = json.loads(cex.read_text(encoding="utf-8"))
        CKEYS = {"r1": "s1", "r2": "s2", "r3": "s3"}
        cled = T / "ledger"
        clabels = {f: list(ALLOWED_VALUES[f])[0] for f in HUMAN_JUDGED_FIELDS}
        for ck, csec in CKEYS.items():
            cprep = prepare_rating(
                ceb,
                item_id=ceb["items"][0]["item_id"],
                labels=clabels,
                rater_key_id=ck,
                rater_secret=csec,
                distribution_secret="D",
            )
            append_rating_record(
                cled,
                cprep,
                created_at=f"T{ck}",
                rating_contract_digest=ceb["rating_contract_digest"],
                context_digests={i["item_id"]: i["item_context_digest"] for i in ceb["items"]},
                keyring=CKEYS,
                qualified_rater_ids=list(CKEYS),
            )
        ch, cc = ledger_head(cled)
        # The CLI takes NO anchor path: `--ledger-anchor` is gone. The anchor is
        # LOOKED UP under the fixed coordinator root DEFAULT_ANCHOR_ROOT
        # (~/.goldset/anchors), keyed by the contract digest - never supplied by
        # the caller. The subprocess is sandboxed with HOME=T so that fixed root
        # resolves inside the test directory instead of the operator's real home;
        # the root itself is still the production constant, not a new flag.
        CLI_HOME = T / "cli_home"
        CLI_HOME.mkdir()
        cli_root = CLI_HOME / ".goldset" / "anchors"
        cled_contract = ceb["rating_contract_digest"]
        publish_anchor(cli_root, cled, rating_contract_digest=cled_contract, secret=CSEC)
        creg = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "authority_key_id": "a",
            "raters": [{"key_id": k, "qualified": True} for k in CKEYS],
        }
        creg["signature"] = sign_registry(creg, "AUTH")
        (T / "reg.json").write_text(json.dumps(creg), encoding="utf-8")
        (T / "ks.json").write_text(
            json.dumps({"schema_version": KEYSTORE_SCHEMA_VERSION, "keys": CKEYS}),
            encoding="utf-8",
        )

        def _cli(out: str, secret: str | None = CSEC) -> dict:
            argv = [
                sys.executable,
                str(Path(__file__).with_name("build_labeling_package.py")),
                "--runs-root",
                str(cli_runs),
                "--out",
                str(T / out),
                "--machine-truth-out",
                str(T / f"m-{out}"),
                "--ratings-dir",
                str(cled),
                "--rater-registry",
                str(T / "reg.json"),
                "--rater-keystore",
                str(T / "ks.json"),
            ]
            env = {
                **os.environ,
                "HOME": str(CLI_HOME),
                "GOLDSET_REGISTRY_AUTHORITY_SECRET": "AUTH",
            }
            if secret is not None:
                env["GOLDSET_ANCHOR_SECRET"] = secret
            subprocess.run(argv, check=True, capture_output=True, env=env)
            return json.loads((T / out).read_text(encoding="utf-8"))

        no_anchor = _cli("cli1.json", secret=None)
        check(
            "CLI: a nonempty ledger with NO anchor secret blocks and accepts nothing",
            any(b.startswith("LEDGER_ANCHOR_MISSING") for b in no_anchor["readiness"]["blockers"])
            and no_anchor["readiness"]["rating_intake"]["records_accepted"] == 0,
        )
        help_text = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("build_labeling_package.py")), "--help"],
            check=True,
            capture_output=True,
            env={**os.environ, "HOME": str(CLI_HOME)},
        ).stdout.decode("utf-8")
        check(
            "CLI no longer exposes a caller-selectable --ledger-anchor flag",
            "--ledger-anchor" not in help_text,
        )
        # Wrong-trust key, but against the LOOKED-UP root: overwrite the published
        # anchor file with one signed by the wrong key. The monotonicity log still
        # records the real publication, so only the signature is invalid.
        (cli_root / f"{cled_contract}.anchor.json").write_text(
            json.dumps(
                sign_ledger_anchor(
                    ch,
                    cc,
                    "WRONG",
                    ledger=ledger_id(cled),
                    rating_contract_digest=cled_contract,
                )
            ),
            encoding="utf-8",
        )
        wrong = _cli("cli2.json")
        check(
            "CLI: a wrongly-signed anchor blocks and accepts nothing",
            any("ANCHOR_SIGNATURE_INVALID" in b for b in wrong["readiness"]["blockers"])
            and wrong["readiness"]["rating_intake"]["records_accepted"] == 0,
        )
        publish_anchor(cli_root, cled, rating_contract_digest=cled_contract, secret=CSEC)
        good = _cli("cli3.json")
        gi = good["readiness"]["rating_intake"]
        check(
            "CLI: a valid anchor plus signed roster ACCEPTS the genuine ratings",
            not [b for b in good["readiness"]["blockers"] if "ANCHOR" in b]
            and gi["records_accepted"] == len(CKEYS)
            and gi["records_rejected"] == 0,
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
