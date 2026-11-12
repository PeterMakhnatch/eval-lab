from __future__ import annotations

import importlib.util
import json
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
