"""Upload the 44 normalized HAR-81 trials to a PRIVATE Docent collection (blind).

Blind metadata only: trial, task (from result.json), reward, exception_type,
n_episodes + assembly provenance. No probe-03 tags, no hand labels.
Secret scan aborts with exit 3 before any network call.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docent import Docent  # noqa: E402
from docent.sdk.integrations.harbor import find_harbor_trial_dirs  # noqa: E402
from export_harbor import convert_trial, secret_hits  # noqa: E402

NAME = "HAR-81 MiMo normalized (trace-lab, private)"
ROOT = Path.home() / "Developer" / "eval-lab" / "derived" / "trace-lab" / "normalized" / "har81"


def main() -> int:
    client = Docent()
    collection_id = client.create_collection(
        name=NAME,
        description="44 normalized HAR-81 MiMo trials (single-file ATIF, tool calls restored); blind upload for reading-vs-hand-key scoring",
    )
    print(f"created {collection_id}")
    before = client.get_collection_collaborators(collection_id)
    print(f"collaborators before upload: {before}")

    runs = []
    skipped = []
    for trial in find_harbor_trial_dirs(ROOT):
        try:
            runs.append(convert_trial(trial, ROOT, {}))
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{trial.name}: {exc}")
    print(f"{len(runs)} runs, {len(skipped)} skipped")
    for line in skipped:
        print(f"skipped {line}")

    payload = [run.model_dump(mode="json") for run in runs]
    hits = secret_hits(payload)
    if hits:
        print(f"refusing: {len(hits)} secret-like strings", file=sys.stderr)
        return 3

    client.add_agent_runs(collection_id, runs)
    after = client.get_collection_collaborators(collection_id)
    print(f"collaborators after upload: {after}")
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
