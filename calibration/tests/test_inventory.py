"""Drive the shipped inventory and agreement helpers on the real corpus."""

from __future__ import annotations

import json

from calibration.agreement import compare_document, extract_verdicts, per_criterion_rates
from calibration.inventory import (
    FAMILIES,
    REQUIRED_VARIANTS,
    TAXONOMY,
    audit_answer_keys,
    audit_environment_keys,
    audit_trajectory_labels,
    corpus_inventory,
    format_corpus_inventory,
    iter_completed_trials,
    load_answer_key,
    trial_label_path,
)
from calibration.rubrics import all_criterion_names


def test_corpus_meets_counts_and_variants() -> None:
    inventory = corpus_inventory()
    for family in FAMILIES:
        docs = inventory.families[family]
        assert len(docs) >= 20, f"{family} has {len(docs)} documents"
        present = {d.variant for d in docs}
        missing = [v for v in REQUIRED_VARIANTS if v not in present]
        assert missing == [], f"{family} missing variants {missing}"


def test_answer_key_audit_has_no_gaps() -> None:
    lines = audit_answer_keys()
    assert lines[-1] == "GAPS 0"
    assert any(line.startswith("OK ") for line in lines)


def test_every_key_covers_family_rubric_names() -> None:
    inventory = corpus_inventory()
    for family, docs in inventory.families.items():
        expected = {(d, n) for d, n in all_criterion_names(family)}
        for doc in docs:
            key = load_answer_key(doc)
            got = {
                (dimension, name)
                for dimension, block in key["criteria"].items()
                for name in block
            }
            assert got == expected, f"{family}/{doc.doc_id} criterion mismatch"


def test_no_answer_keys_under_environment() -> None:
    lines = audit_environment_keys()
    assert lines[-1] == "HITS 0"


def test_every_completed_trial_has_taxonomy_label() -> None:
    trials = iter_completed_trials()
    assert trials, "expected completed trials under harbor-practice/runs and evidence/runs"
    lines = audit_trajectory_labels()
    assert lines[-1] == "UNLABELED_OR_BAD 0"
    assert len([line for line in lines if line.startswith("OK ")]) == len(trials)
    for trial in trials:
        label = json.loads(trial_label_path(trial).read_text(encoding="utf-8"))
        assert label["primary_category"] in TAXONOMY
        cited = label["evidence"][0]
        assert cited["path"]
        assert (trial / cited["path"]).exists()


def test_inventory_is_deterministic() -> None:
    first = format_corpus_inventory()
    second = format_corpus_inventory()
    assert first == second
    assert audit_answer_keys() == audit_answer_keys()
    assert audit_trajectory_labels() == audit_trajectory_labels()


def test_agreement_is_perfect_when_judge_repeats_gold() -> None:
    inventory = corpus_inventory()
    agreements = []
    for family, docs in inventory.families.items():
        for doc in docs:
            gold = load_answer_key(doc)
            # Drive the real normalize+compare path, not a restated expected rate.
            judge_blob = {"criteria": extract_verdicts(gold, family)}
            agreement = compare_document(family, gold, judge_blob)
            assert agreement.rate == 1.0, f"{family}/{doc.doc_id} {agreement}"
            agreements.append(agreement)
    rates = per_criterion_rates(agreements)
    assert rates
    assert all(rate == 1.0 for rate in rates.values())


def test_agreement_drops_when_a_verdict_is_flipped() -> None:
    inventory = corpus_inventory()
    family = "checkout-pool-exhaustion"
    doc = next(d for d in inventory.families[family] if d.variant == "correct")
    gold = load_answer_key(doc)
    observed = extract_verdicts(gold, family)
    target = "identifies_the_mechanism"
    original = observed["causal_reasoning"][target]
    observed["causal_reasoning"][target] = "no" if original == "yes" else "yes"
    agreement = compare_document(family, gold, observed)
    assert agreement.rate < 1.0
    flipped = [c for c in agreement.comparisons if c.name == target][0]
    assert flipped.agree is False
    assert flipped.expected == original
