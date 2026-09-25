"""Focused behavioural tests for the HAR-72 calibration driver's offline logic.

These cover the consumer-visible edges of ``evallab_reef_gate.calibrate`` that do not need
Reef, Ollama or any service: the copied prediction statistics, the loopback/installed-model
preflight refusals, the subprocess environment allowlist, the known-effect seed degradation,
and the trial accounting (attempted/settled/skipped/invalid, Wilson interval, missing episodes,
prediction flags, no-NaN summaries). The parent owns integration, plugin and runtime checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evallab_reef_gate import calibrate

TASKS = [
    "[sieve] How many primes are below 100000? Reply with the count as a plain integer.",
    "[fib] With fib(1) = 1 and fib(2) = 1, compute fib(90) exactly.",
    "[csv] Compute the median of the value column in this csv.",
]


def result_row(**overrides) -> dict:
    base = {
        "trial": 1,
        "condition": "aa",
        "scenario": "aa-gate",
        "status": "settled",
        "seconds": 10.0,
        "published": False,
        "skipped": None,
        "wins": 0,
        "losses": 0,
        "ties": 3,
        "candidate_scores": [0.0, 0.0, 0.0],
        "current_scores": [0.0, 0.0, 0.0],
        "files_identical": True,
    }
    base.update(overrides)
    return base


# -- copied statistics ---------------------------------------------------------------------------


def test_wilson_interval_bounds() -> None:
    assert calibrate.wilson(0, 0) == (0.0, 1.0)
    lo, hi = calibrate.wilson(10, 10)
    assert hi == 1.0
    assert 0.0 < lo < 1.0
    lo, hi = calibrate.wilson(0, 10)
    assert lo == 0.0
    assert 0.0 < hi < 1.0


def test_sign_test_p_exact_values() -> None:
    assert calibrate.sign_test_p(0, 0) == 1.0
    assert calibrate.sign_test_p(0, 5) == 1.0
    assert calibrate.sign_test_p(5, 0) == pytest.approx(1 / 32)
    # n=6, wins=3: sum C(6,i) for i in 3..6 = 20+15+6+1 = 42 of 64.
    assert calibrate.sign_test_p(3, 3) == pytest.approx(42 / 64)


def test_publish_probability_rules_and_alpha() -> None:
    pairs = [calibrate.pair_law(0.5, 0.5)]  # one pairing: P(win)=P(loss)=0.25, P(tie)=0.5
    assert calibrate.publish_probability(pairs, "majority", 0.05) == pytest.approx(0.25)
    # A single win cannot reach p<0.05, but it does reach p<0.6.
    assert calibrate.publish_probability(pairs, "sign", 0.05) == pytest.approx(0.0)
    assert calibrate.publish_probability(pairs, "sign", 0.6) == pytest.approx(0.25)


# -- preflight refusals --------------------------------------------------------------------------


def test_require_loopback_url_accepts_only_http_loopback() -> None:
    assert calibrate.require_loopback_url("http://127.0.0.1:11461") == "http://127.0.0.1:11461"
    assert calibrate.require_loopback_url("http://localhost:11434/") == "http://localhost:11434"
    for refused in ("https://127.0.0.1:11434", "http://example.com:11434", "http://10.0.0.5:11434", "not-a-url"):
        with pytest.raises(SystemExit, match="refusing"):
            calibrate.require_loopback_url(refused)


def test_require_local_model_matches_inventory_or_refuses() -> None:
    inventory = {"models": [{"name": "qwen2.5:7b", "model": "qwen2.5:7b", "digest": "sha256:abc"}]}
    entry = calibrate.require_local_model(inventory, "qwen2.5:7b")
    assert entry["digest"] == "sha256:abc"
    assert calibrate.require_local_model({"models": [{"name": "qwen2.5:latest"}]}, "qwen2.5")[
        "name"
    ] == "qwen2.5:latest"
    with pytest.raises(SystemExit, match="never downloads models"):
        calibrate.require_local_model(inventory, "llama3:70b")


def test_child_environment_drops_ambient_and_pins_owned_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:3128")
    monkeypatch.setenv("PYTHONPATH", "/ambient/path")
    monkeypatch.setenv("TMPDIR", "/tmp/owned")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    env = calibrate.child_environment(
        reef_root=Path("/reef"),
        work=Path("/work"),
        gate_config_path=Path("/work/gate-config.json"),
        python=Path("/reef/.venv/bin/python"),
    )
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert env["PYTHONPATH"] == f"{calibrate.PLUGIN_SRC}{os.pathsep}/reef"
    assert env["TMPDIR"] == "/tmp/owned"
    assert env["LC_CTYPE"] == "UTF-8"
    assert env["PATH"].startswith(str(Path("/reef/.venv/bin/python").resolve().parent))
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["REEF_UPSTREAM_API_KEY"] == "ollama"
    assert env["REEF_RECIPE_CONFIG_DIR"] == "/work/recipes"
    assert env["EVALLAB_REEF_GATE_CONFIG"] == "/work/gate-config.json"


# -- known-effect seed degradation ----------------------------------------------------------------


def test_degrade_seed_entries_degrades_only_answer_style() -> None:
    entries = [
        "reef.harness.runners.native.seed:SEED_NODES",
        {"id": "answer-style", "name": "skill", "config": {"name": "answer-style", "text": "original text"}},
    ]
    degraded, original = calibrate.degrade_seed_entries(entries)
    assert degraded[0] == entries[0]
    assert degraded[1]["config"]["text"] == calibrate.DEGRADED_ANSWER_STYLE_TEXT
    assert original["config"]["text"] == "original text"
    assert "oracle" not in calibrate.DEGRADED_ANSWER_STYLE_TEXT
    assert not any(answer in calibrate.DEGRADED_ANSWER_STYLE_TEXT for answer in calibrate.ANSWERS.values())
    with pytest.raises(ValueError, match="answer-style"):
        calibrate.degrade_seed_entries([{"id": "other", "name": "skill", "config": {}}])


# -- trial accounting ------------------------------------------------------------------------------


def test_summarize_counts_attempted_settled_skipped_invalid() -> None:
    rows = [
        result_row(
            trial=1,
            published=True,
            wins=2,
            losses=0,
            ties=1,
            candidate_scores=[1.0, 0.0, 1.0],
            current_scores=[0.0, 0.0, 1.0],
        ),
        result_row(
            trial=2,
            wins=0,
            losses=1,
            ties=2,
            candidate_scores=[1.0, None, 0.0],
            current_scores=[1.0, 1.0, 1.0],
        ),
        result_row(trial=3, skipped="no candidate", wins=None, losses=None, ties=None),
        result_row(trial=4, wins=1, losses=None, ties=None),
        {"trial": 5, "status": "refused", "proposal_response": {"admitted": False}},
    ]
    summary = calibrate.summarize(rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5)
    assert summary["trials_attempted"] == 5
    assert summary["trials_settled"] == 4
    assert summary["trials_skipped"] == 1
    assert summary["trials_invalid"] == 1
    assert summary["denominator"] == 2
    assert summary["publishes"] == 1
    assert summary["publish_rate"] == 0.5
    lo, hi = summary["wilson95"]
    assert lo < 0.5 < hi
    assert summary["episodes_expected"] == 12
    assert summary["episodes_scored"] == 11
    assert summary["episodes_missing"] == 1
    assert summary["wlt_histogram"] == {"0/1/2": 1, "2/0/1": 1}
    assert summary["pass_rate_by_task_pooled"] == {"[sieve]": 0.75, "[fib]": pytest.approx(1 / 3), "[csv]": 0.75}
    assert summary["all_published_trees_identical"] is True
    assert summary["median_trial_seconds"] == 10.0
    assert isinstance(summary["interval_includes_prediction"], bool)


def test_summarize_missing_rates_report_unavailable_not_nan() -> None:
    rows = [
        result_row(trial=1, candidate_scores=[1.0, 0.0, None], current_scores=[0.0, 1.0, None]),
        result_row(trial=2, candidate_scores=[0.0, 1.0, None], current_scores=[1.0, 0.0, None]),
    ]
    summary = calibrate.summarize(rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5)
    assert summary["pass_rate_by_task_pooled"]["[csv]"] is None
    assert all(row["p_publish"] is None for row in summary["gate_table"])
    assert summary["predicted_publish_rate"] is None
    assert summary["prediction_basis"] is None
    assert summary["interval_includes_prediction"] is None
    text = json.dumps(summary, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text


def test_summarize_prediction_interval_flag_extremes() -> None:
    all_pass = [result_row(trial=i, candidate_scores=[1.0] * 3, current_scores=[1.0] * 3) for i in (1, 2)]
    # All-pass rates make every pairing a tie, so the sign-rule prediction is exactly 0.
    unpublished = calibrate.summarize(
        [dict(entry, published=False) for entry in all_pass], tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5
    )
    assert unpublished["predicted_publish_rate"] == 0.0
    assert unpublished["publishes"] == 0
    assert unpublished["wilson95"][0] == 0.0
    assert unpublished["interval_includes_prediction"] is True
    published = calibrate.summarize(
        [dict(entry, published=True) for entry in all_pass], tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5
    )
    assert published["publishes"] == 2
    assert published["wilson95"][0] > 0.0
    assert published["interval_includes_prediction"] is False


def test_summarize_treats_absent_evidence_as_invalid_not_zero() -> None:
    summary = calibrate.summarize(
        [result_row(trial=1, wins=None, losses=None, ties=None)],
        tasks=TASKS,
        repeats=1,
        condition="aa",
        alpha=0.05,
        min_valid_pairs=5,
    )
    assert summary["trials_invalid"] == 1
    assert summary["denominator"] == 0
    assert summary["publish_rate"] is None
    assert summary["wilson95"] is None


def test_summarize_known_effect_labels_and_contrast() -> None:
    rows = [
        result_row(
            trial=1,
            published=True,
            wins=6,
            losses=0,
            ties=0,
            candidate_scores=[1.0] * 6,
            current_scores=[0.0] * 6,
            files_identical=False,
        ),
        result_row(
            trial=2,
            published=False,
            wins=0,
            losses=0,
            ties=6,
            candidate_scores=[0.0] * 6,
            current_scores=[0.0] * 6,
        ),
    ]
    summary = calibrate.summarize(
        rows, tasks=TASKS, repeats=2, condition="known-effect", alpha=0.05, min_valid_pairs=5
    )
    assert "NOT A/A" in summary["condition"]
    assert "all_published_trees_identical" not in summary
    assert summary["all_published_trees_changed"] is True
    assert summary["gate_table_base"] == "current side only (the deliberately degraded skill)"
    assert summary["pass_rate_by_task_side"]["candidate"]["[sieve]"] == 0.5
    assert summary["pass_rate_by_task_side"]["current"]["[sieve]"] == 0.0
    # Measured per-side rates: the degraded current never passes and the candidate passes
    # half its episodes, so each of the 6 pairings wins with p=0.5; the sign rule at
    # alpha=0.05 publishes only on 5-or-6 wins with no loss: (6+1)/64 = 0.109375.
    assert summary["predicted_publish_rate"] == 0.109375
    assert summary["prediction_basis"] is not None and "per-side" in summary["prediction_basis"]
    lo, hi = summary["wilson95"]
    assert lo < 0.109375 < hi  # 1 publish of 2 trials, so the interval contains the prediction
    assert summary["interval_includes_prediction"] is True


def test_summarize_uses_rule_alpha_consistently() -> None:
    rows = [result_row(trial=1, candidate_scores=[1.0, 0.0, 1.0], current_scores=[0.0, 1.0, 1.0])]
    strict = calibrate.summarize(rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.05, min_valid_pairs=5)
    loose = calibrate.summarize(rows, tasks=TASKS, repeats=1, condition="aa", alpha=0.5, min_valid_pairs=5)
    sign_strict = next(row for row in strict["gate_table"] if row["gate"].startswith("sign test") and "5 ep" in row["gate"])
    sign_loose = next(row for row in loose["gate_table"] if row["gate"].startswith("sign test") and "5 ep" in row["gate"])
    assert "p<0.05" in sign_strict["gate"]
    assert "p<0.5" in sign_loose["gate"]
    assert sign_loose["p_publish"] > sign_strict["p_publish"]
    assert strict["rule"]["alpha"] == 0.05
