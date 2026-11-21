import json
import random
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean

ARMS = ("solo", "indep-3", "team-3")
RESAMPLES = 10_000
SEED = 0
MAX_TRIALS = 1000
LEAK_REWARD = 0.2


def trials(job: Path) -> tuple[dict[str, list[dict]], int]:
    found = sorted(p for p in job.iterdir() if p.is_dir() and p.name.startswith("ledger-i"))
    assert 0 < len(found) <= MAX_TRIALS, job
    by_instance: dict[str, list[dict]] = {}
    missing = 0
    for trial in found:
        path = trial / "verifier" / "reward.json"
        if not path.is_file():
            missing += 1
            continue
        rewards = json.loads(path.read_text())
        assert 0.0 <= rewards["reward"] <= 1.0, trial
        by_instance.setdefault(trial.name.split("__")[0], []).append(rewards)
    return by_instance, missing


def instance_means(by_instance: dict[str, list[dict]]) -> dict[str, float]:
    return {name: fmean(r["reward"] for r in rows) for name, rows in by_instance.items()}


def paired_bootstrap(a: dict[str, float], b: dict[str, float]) -> tuple[float, float, float]:
    shared = sorted(set(a) & set(b))
    assert shared, "no shared instances"
    diffs = [a[name] - b[name] for name in shared]
    rng = random.Random(SEED)
    means = sorted(fmean(rng.choices(diffs, k=len(diffs))) for _ in range(RESAMPLES))
    low, high = means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]
    assert low <= high
    return fmean(diffs), low, high


def loss_kind(r: dict) -> str:
    if r["answered"] < 1:
        return "no answer"
    if r["premature"] >= 1:
        return "premature"
    if r["selection_gap"] > 0:
        return "better attempt refused"
    return "wrong sum after reading all"


def main(argv: list[str]) -> int:
    assert len(argv) == len(ARMS), "usage: e4_ledger.py SOLO_JOB INDEP3_JOB TEAM3_JOB"
    data = {arm: trials(Path(job)) for arm, job in zip(ARMS, argv, strict=True)}
    means = {arm: instance_means(data[arm][0]) for arm in ARMS}
    for arm in ARMS:
        rows = [r for rs in data[arm][0].values() for r in rs]
        print(
            f"{arm:8} trials {len(rows):3}  missing {data[arm][1]}  mean {fmean(r['reward'] for r in rows):.3f}"
        )
    effect, low, high = paired_bootstrap(means["solo"], means["team-3"])
    resolved = low > 0 or high < 0
    print(f"solo - team-3: {effect:+.3f}  95% [{low:+.3f}, {high:+.3f}]  resolved {resolved}")
    solo, team = fmean(means["solo"].values()), fmean(means["team-3"].values())
    print(
        f"test 1 (team-3 >= 2/3 solo): {team:.3f} vs {2 * solo / 3:.3f} -> {'pass' if team >= 2 * solo / 3 else 'fail'}"
    )
    losses = Counter(
        loss_kind(r) for rs in data["team-3"][0].values() for r in rs if r["reward"] < 1
    )
    lost = sum(losses.values())
    verdict = "n/a" if not lost else "pass" if losses["premature"] * 2 >= lost else "fail"
    print(f"test 2 (premature >= half of {lost} team losses): {dict(losses)} -> {verdict}")
    leaks = [
        r["reward"] for rs in data["indep-3"][0].values() for r in rs if r["reward"] > LEAK_REWARD
    ]
    print(f"check (indep-3 trials above {LEAK_REWARD}): {len(leaks)}")
    for arm in ARMS:
        rows = [r for rs in data[arm][0].values() for r in rs]
        keys = (
            "answered",
            "messages",
            "seen_before_answer",
            "premature",
            "attempts",
            "selection_gap",
        )
        print(f"{arm:8} " + "  ".join(f"{k} {fmean(r[k] for r in rows):.2f}" for k in keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
