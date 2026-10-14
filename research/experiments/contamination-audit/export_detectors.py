"""Export the audited non-honeypot passes as detectors-v1 source ``contamination-audit``.

Rows: every agent pass outside the CheatBench port with a final ``cheat`` or
``clean`` label in ``research/calibration/contamination-audit/labels.jsonl``
(cheatbench-port passes are already in the corpus as source ``cb``). Labels:

- ``reward_hacking``: true for a cheat, false for a clean pass;
- ``copied``: true for an upstream_fetch/answer_leak cheat, false for a clean
  pass, null for other cheats (a false claim is not a copy, nor an earned pass).

Extra row fields: ``mechanism``, ``experiment``, ``cheat_prompted`` (filter it to
score on unprompted cheats only) and ``kev_flagged`` (the clean passes Kev flagged
at the Terminal Wrench 5% cut: the hard negatives).

Usage (worktree root; reseals MANIFEST.sha256):
  uv run python research/experiments/contamination-audit/export_detectors.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
sys.path.insert(0, str(WORKTREE / "src"))

from evallab.contamination import load_labels  # noqa: E402
from evallab.detectors import Corpus  # noqa: E402
from evallab.results_home import results_root  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

SOURCE = "contamination-audit"
COPY_MECHANISMS = {"upstream_fetch", "answer_leak"}
HONEYPOT_FAMILY = "cheatbench-port"


def trial_ref(trial_dir: Path) -> dict[str, str]:
    results, primary = results_root().resolve(), shared_checkout_root(WORKTREE).resolve()
    path = trial_dir.resolve()
    if path.is_relative_to(results):
        return {"kind": "published", "path": str(path.relative_to(results))}
    if path.is_relative_to(primary):
        return {"kind": "local_run", "path": str(path.relative_to(primary))}
    raise ValueError(f"{trial_dir}: neither under the results home nor the eval-lab checkout")


def main() -> None:
    labels = load_labels(WORKTREE)
    inv = {
        r["trial"]: r
        for r in (
            json.loads(x)
            for x in (WORKTREE / "derived/contam/inventory.jsonl").read_text().splitlines()
            if x
        )
    }
    kev = {
        r["trial"]: bool(r["kev"])
        for r in (
            json.loads(x)
            for x in (WORKTREE / "derived/contam/triage.jsonl").read_text().splitlines()
            if x
        )
    }
    rows, gone = [], []
    for trial, label in sorted(labels.items()):
        if label["family"] == HONEYPOT_FAMILY or label["verdict"] not in ("cheat", "clean"):
            continue
        trial_dir = Path(inv[trial]["trial_dir"])
        if not (trial_dir / "agent/trajectory.json").is_file():
            gone.append(trial)  # its worktree was retired after the audit
            continue
        cheat = label["verdict"] == "cheat"
        mechanism = label["mechanism"] if cheat else None
        rows.append(
            {
                "id": f"ca-{trial}",
                "source": SOURCE,
                "labels": {
                    "reward_hacking": cheat,
                    "copied": (mechanism in COPY_MECHANISMS or None) if cheat else False,
                },
                "families": [mechanism] if cheat else [],
                "mechanism": mechanism,
                "experiment": label["family"],
                "cheat_prompted": label["cheat_prompted"],
                "kev_flagged": kev.get(trial, False),
                "trial": trial_ref(trial_dir),
                "trajectory_sha256": hashlib.sha256(
                    (trial_dir / "agent/trajectory.json").read_bytes()
                ).hexdigest(),
            }
        )
    corpus = Corpus.load("detectors-v1")
    if corpus.verify():
        sys.exit("detectors-v1 checksums do not match before export; refusing to reseal")
    kept = [r for r in corpus.rows if r["source"] != SOURCE]
    manifest = corpus.root / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept + rows))
    meta = json.loads((corpus.root / "corpus.json").read_text())
    n_cheat = sum(r["labels"]["reward_hacking"] for r in rows)
    n_unprompted = sum(r["labels"]["reward_hacking"] and not r["cheat_prompted"] for r in rows)
    meta["sources"][SOURCE] = {
        "rows": len(rows),
        "setting": "agent_runs",
        "what": (
            f"Every audited Eval Lab agent pass outside the CheatBench port (2026-10-13 contamination audit): "
            f"{n_cheat} confirmed cheats ({n_unprompted} in runs not prompted to cheat; mechanism per row) and "
            f"{len(rows) - n_cheat} clean passes, {sum(r['kev_flagged'] and not r['labels']['reward_hacking'] for r in rows)} "
            "of them Kev-flagged hard negatives, published with firing cards, margins, peak "
            "windows and both judges' summaries in "
            "research/experiments/contamination-audit/results/kev_hard_negatives.jsonl "
            "(cut analysis in results/kev_cut_analysis.json). Verdicts: GPT-6.1 Sol plus a blind Claude second judge, "
            "disagreements resolved from the trajectory (research/experiments/contamination-audit)."
        ),
        "origin": "~/Developer/eval-lab-results (published) and the eval-lab checkout's runs/ (local_run)",
        "license": "Eval Lab",
        "caveat": "Overlaps source 'ours' (same trajectories, older labels). Filter cheat_prompted=false for unprompted cheats.",
    }
    (corpus.root / "corpus.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    Corpus.load("detectors-v1").seal(labels=True)
    print(
        f"{SOURCE}: {len(rows)} rows ({n_cheat} cheats, {n_unprompted} unprompted); trajectory gone: {gone}"
    )


if __name__ == "__main__":
    main()
