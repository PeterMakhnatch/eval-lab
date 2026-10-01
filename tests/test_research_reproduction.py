from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("archived", [False, True])
def test_census_includes_retained_har115_jobs_from_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archived: bool
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "lab" / ".worktrees" / "analysis"
    checkout.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    common = ModuleType("common")
    common.ROOT = checkout
    common.CENSUS = checkout / "census"
    monkeypatch.setitem(sys.modules, "common", common)
    retained = (
        home / ".local/share/wt-archive/eval-lab/har115-census-20261001"
        if archived
        else checkout.parent / "har115-census-20261001"
    )
    runs = retained / "runs"
    expected = [runs / "har115-nop-000001", runs / "har115-rnop-000002-abcdef"]
    for job in [*expected, runs / "har115-probe-000001", runs / "har115-nop-000003-hung-01"]:
        job.mkdir(parents=True)
    module = _load_script(
        "research/experiments/har113-variants/census_update.py", "har132_census_fixture"
    )

    assert module.jobs() == sorted(map(str, expected))


@pytest.mark.parametrize(
    ("native", "expected"),
    [({"n_input_tokens": 100, "n_output_tokens": 20}, (100, 20)), (None, (None, None))],
)
def test_har116_keeps_native_token_basis_when_projection_uses_proxy_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: dict | None, expected: tuple
) -> None:
    module = _load_script(
        "research/experiments/har116-loopfix-leak/build_results.py", "har132_results_fixture"
    )
    live = tmp_path / "live"
    published = tmp_path / "published"
    monkeypatch.setattr(module, "RESULTS_LIVE", live)
    monkeypatch.setattr(module, "RESULTS_PUB", published)
    job = "har116-a-000001-baseline"
    trial = live / job / f"{job}__trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"agent_result": native}))
    (trial / "verifier/reward.txt").write_text("1.0\n")
    processed = published / f"HAR-116-{job}" / "processed"
    processed.mkdir(parents=True)
    (processed / "trial-record.json").write_text(
        json.dumps(
            {
                "tokens_proxy": {"input_tokens": 150, "output_tokens": 40},
                "agent_steps": 0,
                "token_flow": {},
            }
        )
    )

    row = module.build_row(job, "A", "000001", "baseline")

    assert (row["input_tokens"], row["output_tokens"]) == expected
    assert row["reward_raw"] == 1.0
