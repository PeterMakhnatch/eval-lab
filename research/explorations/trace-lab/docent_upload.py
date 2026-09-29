"""Upload Eval Lab's local Harbor runs to Docent (https://docent.transluce.org).

Dry run (convert + credential scan, nothing leaves the machine):
    uv run --no-project --with docent python research/explorations/trace-lab/docent_upload.py
Upload into a new collection:
    uv run --no-project --with docent python research/explorations/trace-lab/docent_upload.py --upload

The upload needs a Docent API key in ~/.docent/docent.env (`uvx docent@latest setup` writes it).
Trials whose trajectory text was redacted by Eval Lab are skipped (nothing to read), duplicate
copies of the same trial are uploaded once, and the upload is refused if any file matches a
credential pattern.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from docent.sdk.integrations import convert_harbor_trial_to_agent_run

LAB = Path.home() / "Developer" / "eval-lab"
REDACTED = "<<evallab-redacted"
SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|xox[abp]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{30,}|Bearer\s+[A-Za-z0-9._\-]{24,}"
    r"|(?i:api[_-]?key)\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{24,}"
)


def trial_dirs() -> list[Path]:
    roots = [LAB / "runs", LAB / "jobs"]
    roots += [wt / sub for wt in sorted((LAB / ".worktrees").glob("*")) for sub in ("runs", "jobs")]
    found, seen = [], set()
    for root in roots:
        for trajectory in sorted(root.rglob("agent/trajectory.json")):
            trial = trajectory.parent.parent
            result = trial / "result.json"
            if not result.exists() or REDACTED in trajectory.read_text(errors="replace"):
                continue
            trial_id = json.loads(result.read_text()).get("id") or str(trial)
            if trial_id not in seen:
                seen.add(trial_id)
                found.append(trial)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upload", action="store_true", help="create a collection and upload")
    parser.add_argument("--name", default="Eval Lab runs (local)", help="collection name")
    args = parser.parse_args()

    trials = trial_dirs()
    leaks = [
        f"{trial / name}: {match.group(0)[:6]}…"
        for trial in trials
        for name in ("agent/trajectory.json", "result.json", "config.json")
        if (trial / name).exists()
        for match in SECRET.finditer((trial / name).read_text(errors="replace"))
    ]
    if leaks:
        print("refusing: possible credentials found:", *leaks, sep="\n  ")
        return 2
    runs = [convert_harbor_trial_to_agent_run(trial) for trial in trials]
    print(f"{len(runs)} trials converted, credential scan clean")
    if not args.upload:
        print("dry run: nothing uploaded (add --upload)")
        return 0

    from docent import Docent

    client = Docent()
    collection_id = client.create_collection(
        name=args.name,
        description=f"{len(runs)} Harbor trials from {LAB} (full-text trajectories only)",
    )
    client.add_agent_runs(collection_id, runs)
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
