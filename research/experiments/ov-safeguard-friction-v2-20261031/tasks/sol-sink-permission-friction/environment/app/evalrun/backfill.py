"""Backfill historical evaluation runs into the result store.

    python -m evalrun.backfill --settings config/settings.toml --runs data/historical_runs.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evalrun.alerts import Alert, AlertDispatcher
from evalrun.config import Settings, load_settings
from evalrun.publisher import publish_result
from evalrun.store import ResultStore


def backfill(settings: Settings, runs: list[dict]) -> dict:
    store = ResultStore(settings.result_store)
    dispatcher = AlertDispatcher(settings)
    severities: dict[str, int] = {}
    try:
        for run in runs:
            alert = publish_result(store, dispatcher, run)
            severities[alert.severity] = severities.get(alert.severity, 0) + 1
        dispatcher.dispatch(Alert("info", "backfill", f"backfill finished: {len(runs)} runs"))
        return {"published": store.count(), "severities": severities}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="config/settings.toml")
    parser.add_argument("--runs", default="data/historical_runs.json")
    args = parser.parse_args(argv)
    settings = load_settings(args.settings)
    runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
    print(json.dumps(backfill(settings, runs), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
