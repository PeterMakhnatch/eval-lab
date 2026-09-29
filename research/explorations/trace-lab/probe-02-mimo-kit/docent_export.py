"""Export a Harbor job directory to Docent agent runs (Trace Lab probe-02 MiMo kit).

Dry run (the default; nothing leaves the machine)::

    uv run --no-project --with docent python docent_export.py <job_dir> --collection <name>

Upload (explicit ``--no-dry-run``, and needs a key in ~/.docent/docent.env)::

    uv run --no-project --with docent python docent_export.py <job_dir> --collection <name> --no-dry-run

Uses the Docent SDK's Harbor integration
(`docent.sdk.integrations.convert_harbor_trial_to_agent_run`), which fits
because Terminus-2 writes exactly one ATIF trajectory file directly under each
trial's ``agent/`` dir. Trials without one (e.g. non-ATIF agents) are skipped
with a warning, not faked. Each converted run keeps the SDK's raw
``metadata["harbor"]`` (config.json + result.json payloads) and gains a flat,
queryable ``metadata["mimo"]`` block: task id, trial id, reward, infra-error
flag, agent name, model name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ENV_FILE = Path.home() / ".docent" / "docent.env"

try:
    from docent.sdk.integrations import convert_harbor_trial_to_agent_run
    from docent.sdk.integrations.harbor import find_harbor_trial_dirs
    from docent.sdk.integrations.util import ConversionError
except ImportError as exc:  # pragma: no cover - missing dependency hint
    print(
        "error: the 'docent' package is required "
        "(run with: uv run --no-project --with docent python docent_export.py ...)",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "collection"


def trial_summary(trial: Path) -> dict:
    """Extract task/trial/reward/infra-error/agent/model from trial JSON files."""
    config = json.loads((trial / "config.json").read_text(encoding="utf-8"))
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    agent = config.get("agent") or {}
    task = config.get("task")
    if isinstance(task, dict):
        task_id = task.get("name") or task.get("path") or str(task)
    else:
        task_id = task or result.get("task_name")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return {
        "task_id": task_id,
        "trial_id": result.get("id"),
        "trial_name": config.get("trial_name", trial.name),
        "reward": rewards.get("reward"),
        "rewards": rewards or None,
        "infra_error": result.get("exception_info") is not None,
        "exception_type": (result.get("exception_info") or {}).get("exception_type"),
        "agent_name": agent.get("name"),
        "model_name": agent.get("model_name"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("job_dir", help="Harbor job directory holding trial subdirs")
    parser.add_argument("--collection", required=True, help="Docent collection name")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write payload JSON locally instead of uploading "
        "(default: on; uploading needs an explicit --no-dry-run)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="payload JSON path for --dry-run (default: ./<collection>-docent-payload.json)",
    )
    args = parser.parse_args()

    job_dir = Path(args.job_dir).expanduser().resolve()
    if not job_dir.is_dir():
        print(f"error: job_dir not found: {job_dir}", file=sys.stderr)
        return 2

    trials = find_harbor_trial_dirs(job_dir)
    if not trials:
        print(f"error: no Harbor trial dirs under {job_dir}", file=sys.stderr)
        return 2

    runs, skipped = [], []
    for trial in trials:
        try:
            run = convert_harbor_trial_to_agent_run(trial, path_root=job_dir)
        except ConversionError as exc:
            skipped.append(f"{trial.name}: {exc}")
            continue
        run.merge_metadata({"mimo": trial_summary(trial)})
        runs.append(run)

    n_messages = sum(
        len(t.messages) for run in runs for tg in run.transcript_groups for t in tg.transcripts
    ) + sum(len(t.messages) for run in runs for t in run.transcripts)
    print(f"{len(trials)} trials found, {len(runs)} converted, {len(skipped)} skipped")
    for line in skipped:
        print(f"  skipped {line}")
    print(f"{n_messages} transcript messages across {len(runs)} runs")

    if args.dry_run:
        out = Path(args.out) if args.out else Path(f"{slug(args.collection)}-docent-payload.json")
        payload = [run.model_dump(mode="json") for run in runs]
        out.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        print(f"dry run: wrote {len(payload)} runs to {out} (nothing uploaded)")
        return 0

    if not ENV_FILE.exists():
        print(
            f"error: refusing to upload: {ENV_FILE} not found (run with --dry-run)",
            file=sys.stderr,
        )
        return 2

    from docent import Docent

    client = Docent()  # reads DOCENT_API_KEY from ~/.docent/docent.env; never printed
    collection_id = client.create_collection(
        name=args.collection,
        description=f"{len(runs)} Harbor trials from {job_dir}",
    )
    client.add_agent_runs(collection_id, runs)
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
