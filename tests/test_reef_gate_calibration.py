"""Focused behavioural tests for the HAR-72 calibration driver's offline logic.

These cover the consumer-visible edges of ``evallab_reef_gate.calibrate`` that do not need
Reef, Ollama beyond a loopback throwaway HTTP socket, or any service: the copied prediction
statistics (reusing the package sign rule), the literal-loopback / installed-GGUF / no-download
preflight refusals (decorated URLs, cloud routing, redirects, ambient proxies, bounded reads),
the subprocess environment allowlist, the known-effect seed degradation, and the trial
accounting (attempted/settled/skipped/invalid, exact Wilson endpoints, full-precision rates,
prediction flags, no-NaN summaries, strict decision records). The parent owns integration,
plugin and runtime checks.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import threading
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


def decision_record(**overrides) -> dict:
    base = {
        "decision_seconds": 0.4,
        "evaluation_seconds": 130.2,
        "reef_commit": "4c3a6bb24949bd93a4566ca1d9877d4feeab023e",
        "reason_code": "publish",
        "pairs": [],
        "vetoes": [],
    }
    base.update(overrides)
    return base


# -- copied statistics ---------------------------------------------------------------------------


def test_wilson_interval_exact_boundaries() -> None:
    assert calibrate.wilson(0, 0) == (0.0, 1.0)
    assert calibrate.wilson(10, 10)[1] == 1.0  # k=n keeps the exact upper endpoint
    assert calibrate.wilson(0, 10)[0] == 0.0  # k=0 keeps the exact lower endpoint
    lo, hi = calibrate.wilson(10, 10)
    assert 0.0 < lo < 1.0
    lo, hi = calibrate.wilson(0, 10)
    assert 0.0 < hi < 1.0




def test_publish_probability_rules_and_alpha() -> None:
    pairs = [calibrate.pair_law(0.5, 0.5)]  # one pairing: P(win)=P(loss)=0.25, P(tie)=0.5
    assert calibrate.publish_probability(pairs, "majority", 0.05) == pytest.approx(0.25)
    # A single win cannot reach p<0.05, but it does reach p<0.6.
    assert calibrate.publish_probability(pairs, "sign", 0.05) == pytest.approx(0.0)
    assert calibrate.publish_probability(pairs, "sign", 0.6) == pytest.approx(0.25)


# -- preflight refusals --------------------------------------------------------------------------


def test_require_loopback_url_accepts_only_literal_loopback_origins() -> None:
    assert calibrate.require_loopback_url("http://127.0.0.1:11461") == "http://127.0.0.1:11461"
    assert calibrate.require_loopback_url("http://localhost:11434/") == "http://localhost:11434"
    assert calibrate.require_loopback_url("http://[::1]:11434") == "http://[::1]:11434"
    refused = [
        "https://127.0.0.1:11434",  # not plain http
        "http://example.com:11434",  # not loopback
        "http://10.0.0.5:11434",  # private but not loopback
        "not-a-url",  # no host at all
        "http://127.0.0.1",  # no explicit port
        "http://user:pass@127.0.0.1:11434",  # embedded credentials
        "http://127.0.0.1:11434/v1",  # decorated with a path
        "http://127.0.0.1:11434/?probe=1",  # decorated with a query
        "http://127.0.0.1:11434#section",  # decorated with a fragment
        "http://127.0.0.1:99999",  # invalid port
    ]
    for url in refused:
        with pytest.raises(SystemExit, match="refusing"):
            calibrate.require_loopback_url(url)


def _valid_model_entry(**overrides) -> dict:
    entry = {
        "name": "qwen2.5:7b",
        "model": "qwen2.5:7b",
        "digest": "sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
        "size": 4_700_000_000,
        "details": {"format": "gguf"},
    }
    entry.update(overrides)
    return entry


def test_require_local_model_accepts_unique_local_gguf() -> None:
    entry = _valid_model_entry()
    assert calibrate.require_local_model({"models": [entry]}, "qwen2.5:7b") is entry
    assert (
        calibrate.require_local_model(
            {"models": [_valid_model_entry(name="qwen2.5:latest", model="qwen2.5:latest")]}, "qwen2.5"
        )["name"]
        == "qwen2.5:latest"
    )


def test_require_local_model_refuses_cloud_and_non_gguf_routes() -> None:
    cases = [
        ("cloud remote_host", _valid_model_entry(remote_host="registry.ollama.ai")),
        ("cloud remote_model", _valid_model_entry(remote_model="qwen2.5:7b")),
        ("non-gguf format", _valid_model_entry(details={"format": "safetensors"})),
        ("missing details", _valid_model_entry(details=None)),
        ("zero size", _valid_model_entry(size=0)),
        ("non-integer size", _valid_model_entry(size="4.7GB")),
        ("short digest", _valid_model_entry(digest="sha256:deadbeef")),
        ("non-hex digest", _valid_model_entry(digest="z" * 64)),
        ("missing digest", _valid_model_entry(digest=None)),
    ]
    for _, entry in cases:
        with pytest.raises(SystemExit):
            calibrate.require_local_model({"models": [entry]}, "qwen2.5:7b")


def test_require_local_model_refuses_ambiguous_matches() -> None:
    inventory = {"models": [_valid_model_entry(), _valid_model_entry(size=4_701_000_000)]}
    with pytest.raises(SystemExit, match="ambiguous"):
        calibrate.require_local_model(inventory, "qwen2.5:7b")


class _TagsHandler(http.server.BaseHTTPRequestHandler):
    """A throwaway loopback /api/tags endpoint; class attributes configure the reply."""

    payload = b'{"models": []}'
    status = 200
    extra_headers: dict[str, str] = {}

    def do_GET(self) -> None:
        body = self.payload
        self.send_response(self.status)
        for key, value in self.extra_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep test output quiet
        return


@contextlib.contextmanager
def _serving(handler: type[_TagsHandler]):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fetch_local_inventory_ignores_ambient_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    with _serving(_TagsHandler) as base:
        payload = calibrate.fetch_local_inventory(base)
    assert payload == {"models": []}


def test_fetch_local_inventory_refuses_redirects() -> None:
    class Redirect(_TagsHandler):
        status = 302
        extra_headers = {"Location": "http://127.0.0.1:1/api/tags"}

    with _serving(Redirect) as base, pytest.raises(SystemExit, match="redirected"):
        calibrate.fetch_local_inventory(base)


def test_fetch_local_inventory_bounds_the_body() -> None:
    class Huge(_TagsHandler):
        payload = b"x" * 65

    with _serving(Huge) as base, pytest.raises(SystemExit, match="exceeds"):
        calibrate.fetch_local_inventory(base, max_bytes=64)


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
    assert not any(answer in calibrate.DEGRADED_ANSWER_STYLE_TEXT for answer in calibrate.ANSWERS.values())
    with pytest.raises(ValueError, match="answer-style"):
        calibrate.degrade_seed_entries([{"id": "other", "name": "skill", "config": {}}])


# -- decision records -----------------------------------------------------------------------------


def test_read_decision_reports_exact_fields_full_precision(tmp_path: Path) -> None:
    record_dir = tmp_path / "gate-decisions"
    record_dir.mkdir()
    (record_dir / "d1.json").write_text(json.dumps(decision_record()))
    (record_dir / "d2.json").write_text(
        json.dumps(
            decision_record(
                decision_seconds=1 / 3,
                reason_code="regression_veto",
                vetoes=[
                    {
                        "task_id": "[sieve]",
                        "failure_count": 2,
                        "failed_repeat_indices": [0, 3],
                        "threshold": 1,
                    }
                ],
            )
        )
    )
    out = calibrate.read_decision_records(record_dir)
    assert out["count"] == 2
    assert out["decision_walltime_s"] == [0.4, pytest.approx(1 / 3)]
    assert out["decision_walltime_median_s"] == pytest.approx((0.4 + 1 / 3) / 2)
    assert out["evaluation_walltime_median_s"] == 130.2
    assert out["reef_commits"] == ["4c3a6bb24949bd93a4566ca1d9877d4feeab023e"]
    assert out["reason_codes"] == {"publish": 1, "regression_veto": 1}


def test_read_decision_records_reports_malformed_records(tmp_path: Path) -> None:
    record_dir = tmp_path / "gate-decisions"
    record_dir.mkdir()
    bad_bodies = [
        "[1, 2]",  # not an object
        "{not json",
        json.dumps(decision_record(pairs="x")),  # pairs must be a list
        json.dumps(decision_record(vetoes=None)),  # vetoes must be a list
        json.dumps(decision_record(decision_seconds=-1)),  # negative timing
        json.dumps(decision_record(evaluation_seconds="130")),  # non-numeric timing
        json.dumps(decision_record(reef_commit="")),  # empty text field
        json.dumps(decision_record(reason_code=7)),  # non-string reason
    ]
    for bad in bad_bodies:
        (record_dir / "bad.json").write_text(bad)
        with pytest.raises(SystemExit, match="malformed decision record"):
            calibrate.read_decision_records(record_dir)
        (record_dir / "bad.json").unlink()
    (record_dir / "good.json").write_text(json.dumps(decision_record()))
    assert calibrate.read_decision_records(record_dir)["count"] == 1


def test_read_decision_records_without_directory_reports_unavailable(tmp_path: Path) -> None:
    result = calibrate.read_decision_records(tmp_path / "gate-decisions")
    assert result["count"] == 0
    assert result["decision_walltime_median_s"] is None
    assert result["evaluation_walltime_median_s"] is None


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
    assert summary["pass_rate_by_task_pooled"]["[sieve]"] == pytest.approx(0.75)
    assert summary["pass_rate_by_task_pooled"]["[fib]"] == pytest.approx(1 / 3)  # full precision, not 0.333
    assert summary["pass_rate_by_task_pooled"]["[csv]"] == pytest.approx(0.75)
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
        [dict(entry, published=False) for entry in all_pass],
        tasks=TASKS,
        repeats=1,
        condition="aa",
        alpha=0.05,
        min_valid_pairs=5,
    )
    assert unpublished["predicted_publish_rate"] == 0.0
    assert unpublished["publishes"] == 0
    assert unpublished["wilson95"][0] == 0.0  # exact k=0 endpoint, so 0.0 is inside the interval
    assert unpublished["interval_includes_prediction"] is True
    published = calibrate.summarize(
        [dict(entry, published=True) for entry in all_pass],
        tasks=TASKS,
        repeats=1,
        condition="aa",
        alpha=0.05,
        min_valid_pairs=5,
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
    sign_strict = next(
        row for row in strict["gate_table"] if row["gate"].startswith("sign test") and "5 ep" in row["gate"]
    )
    sign_loose = next(
        row for row in loose["gate_table"] if row["gate"].startswith("sign test") and "5 ep" in row["gate"]
    )
    assert "p<0.05" in sign_strict["gate"]
    assert "p<0.5" in sign_loose["gate"]
    assert sign_loose["p_publish"] > sign_strict["p_publish"]
    assert strict["rule"]["alpha"] == 0.05
