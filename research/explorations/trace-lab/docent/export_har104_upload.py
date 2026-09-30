"""Upload HAR-104's 10 default-recorded Python runs to Docent (HAR-109 items 2+5 prep).

Reuses ``export_harbor.convert_trial`` (stock converter path; default-recorded
trials carry ``bash_command``/``mark_task_complete`` tool_calls in one
``trajectory.json``, so no normalizing) and attaches the same blind Eval Lab
metadata as the HAR-81 backfill: ``trace_lab.stop_reason`` (report-run stop
plus binding ceiling), ``trace_lab.verdict``, ``trace_lab.domain``.

  dry-run (default): convert + secret-scan + write the payload locally.
    No Docent client is constructed: zero network calls. Use
    ``--metadata-json`` to attach precomputed Eval Lab metadata (needed when
    the interpreter lacks evallab, e.g. the docent env); otherwise metadata
    is computed in-process with ``build_run_report`` (needs the venv).
  --no-dry-run: create the PRIVATE collection "HAR-104 Python exploration
    (private)" and upload. Refuses when no trial dirs resolve (the runs do
    not exist yet): the collection is only created when runs exist.

Every string is secret-scanned before writing or uploading; any match aborts
with exit 3. $0 model spend (conversion + metadata API only).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docent.sdk.integrations.harbor import find_harbor_trial_dirs  # noqa: E402
from export_harbor import convert_trial, secret_hits  # noqa: E402

NAME = "HAR-104 Python exploration (private)"


def trial_metadata(trial: Path, precomputed: dict | None) -> dict:
    """{stop_reason, verdict, domain} from Eval Lab records (blind)."""
    if precomputed is not None:
        row = precomputed.get(trial.name)
        if row is None:
            raise KeyError(f"{trial.name}: no row in --metadata-json")
        return {k: row[k] for k in ("stop_reason", "verdict", "domain")}
    from evallab.interpretation.run_report import build_run_report

    report = build_run_report(trial)
    stop = report["outcome"]["stop_reason"]
    if stop == "trial_budget_exhausted":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from har81_metadata_backfill import binding_ceiling

        stop = f"{stop}:{binding_ceiling(trial)}"
    task = report["identity"]["task"]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from har81_metadata_backfill import task_domain

    return {"stop_reason": stop, "verdict": report["outcome"]["verdict"], "domain": task_domain(task)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+", help="Harbor job dirs or a runs/ dir holding them")
    parser.add_argument("--collection", default=NAME)
    parser.add_argument("--metadata-json", default=None,
                        help="precomputed Eval Lab metadata (trial -> stop_reason/verdict/domain)")
    parser.add_argument("--dry-run", dest="dry_run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--out", help="payload path for dry runs")
    parser.add_argument("--limit", type=int, default=None, help="convert at most N trials (dry-run probes)")
    args = parser.parse_args(argv)

    precomputed = json.loads(Path(args.metadata_json).read_text()) if args.metadata_json else None
    trials: list[Path] = []
    for root in map(Path, args.roots):
        trials.extend(find_harbor_trial_dirs(root))
    trials.sort(key=lambda p: p.name)
    if args.limit is not None:
        trials = trials[: args.limit]
    if not trials:
        roots = " ".join(args.roots)
        print(f"no trial dirs resolved under {roots}; creating nothing (HAR-104 runs do not exist yet)")
        return 0

    runs, skipped = [], []
    for trial in trials:
        try:
            run = convert_trial(trial, trial.parent, {})
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{trial.name}: {exc}")
            continue
        run.merge_metadata({"trace_lab": trial_metadata(trial, precomputed)})
        runs.append(run)
    for line in skipped:
        print(f"skipped {line}", file=sys.stderr)
    transcripts = sum(len(r.transcripts) for r in runs)
    messages = sum(len(t.messages) for r in runs for t in r.transcripts)
    print(f"{len(runs)} runs, {transcripts} transcripts, {messages} messages, {len(skipped)} skipped")

    payload = [run.model_dump(mode="json") for run in runs]
    hits = secret_hits(payload)
    if hits:
        print(f"refusing: {len(hits)} secret-like strings, e.g. {sorted(set(hits))[:5]}", file=sys.stderr)
        return 3
    print("secret scan: 0 hits")
    if args.dry_run:
        out = Path(args.out or f"{re.sub(r'[^A-Za-z0-9._-]+', '-', args.collection)}-payload.json")
        out.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        print(f"dry run: wrote {out} (nothing uploaded, no collection created)")
        return 0

    from docent import Docent

    client = Docent()
    collection_id = client.create_collection(name=args.collection, description=(
        f"{len(runs)} default-recorded HAR-104 Python trials (stock tool calls); "
        "blind Eval Lab metadata only: stop_reason, verdict, domain"))
    print(f"created {collection_id}")
    print(f"collaborators before upload: {client.get_collection_collaborators(collection_id)}")
    client.add_agent_runs(collection_id, runs)
    print(f"collaborators after upload: {client.get_collection_collaborators(collection_id)}")
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
