from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from random import Random

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from muster.lab.contrast import paired  # noqa: E402

RUNS = 2_000
INSTANCES = 10
ATTEMPTS = (1, 3, 5)
GRID = (1.0, 0.9, 0.7, 0.5, 0.3)
SEED = 261125
ALPHA = 0.05


def sample(rng: Random, p: float, k: int, heterogeneous: bool) -> tuple[float, ...]:
    assert 0 <= p <= 1 and k in ATTEMPTS
    differences = []
    for _ in range(INSTANCES):
        direction = rng.choice((-1, 1)) if heterogeneous else 0
        probe = p + direction * min(p, 1 - p) / 2
        guard = 1.0
        successes_guard = sum(rng.random() < guard for _ in range(k))
        successes_probe = sum(rng.random() < probe for _ in range(k))
        differences.append((successes_guard - successes_probe) / k)
    return tuple(sorted(differences))


@lru_cache(maxsize=8_192)
def detected(differences: tuple[float, ...]) -> tuple[bool, bool]:
    result = paired(differences, seed=SEED)
    interval = result["interval"]
    assert isinstance(interval, list)
    return interval[0] > 0 or interval[1] < 0, result["sign_p"] <= ALPHA


def power(rng: Random, p: float, k: int, heterogeneous: bool) -> tuple[float, float]:
    bootstrap = sign = 0
    for _ in range(RUNS):
        interval_excludes, sign_rejects = detected(sample(rng, p, k, heterogeneous))
        bootstrap += interval_excludes
        sign += sign_rejects
    return bootstrap / RUNS, sign / RUNS


def main() -> None:
    rng = Random(SEED)
    print(f"{RUNS} runs/cell; {INSTANCES} instances; seed {SEED}; guard pass rate 1.0")
    print("| Probe rate | Heterogeneity | Attempts | Bootstrap excludes 0 | Sign p <= 0.05 |")
    print("|---:|---|---:|---:|---:|")
    for p in GRID:
        for heterogeneous in (False, True):
            for k in ATTEMPTS:
                bootstrap, sign = power(rng, p, k, heterogeneous)
                print(f"| {p:.1f} | {'on' if heterogeneous else 'off'} | {k} | "
                      f"{bootstrap:.3f} | {sign:.3f} |")


if __name__ == "__main__":
    main()
