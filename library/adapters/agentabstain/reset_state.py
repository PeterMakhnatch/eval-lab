"""Reset one isolated Harbor variant state without touching sibling trials."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def reset_trial(trial_root: str | Path, initial_state: str | Path) -> Path:
    trial = Path(trial_root).resolve()
    source = Path(initial_state).resolve()
    if trial == trial.parent or trial.name in {"", ".", "runs"}:
        raise ValueError(f"refusing unsafe trial root: {trial}")
    if not source.is_file():
        raise FileNotFoundError(source)
    state = trial / "state"
    if state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True)
    shutil.copy2(source, state / source.name)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("trial_root", type=Path)
    parser.add_argument("initial_state", type=Path)
    args = parser.parse_args()
    print(reset_trial(args.trial_root, args.initial_state))
