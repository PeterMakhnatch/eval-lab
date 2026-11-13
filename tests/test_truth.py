from __future__ import annotations

from evallab.cohort import minimum_detectable_effect


def test_power_reports_mde_and_required_n_k_tradeoffs() -> None:
    effect = minimum_detectable_effect(n_tasks=100, k=1, baseline=0.5)
    assert effect is not None and 0 < effect < 0.3
