#!/usr/bin/env python3
"""Stage Hubble42/ResolveBench as a locked, local-only cross-test bundle (stdlib only).

Downloads every file of the dataset at the pinned commit over HTTPS (``resolve/<revision>``), checks
each against the Hub tree at that commit (size, plus the LFS sha256 or the git blob sha1), writes
STAGE-MANIFEST.json, SHA256SUMS and CROSS_TEST_ONLY.md, then locks the tree (dirs 0555, files 0444).
Files already present with the right bytes are not downloaded again. No huggingface_hub, no pip.

    python3 stage_bundle.py            # download (if needed), verify, write receipts, lock
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

REPO = "Hubble42/ResolveBench"
REVISION = "6e8f58fd29aa794d8b8eb9e3e01ffe44b48f4b0e"
LICENCE = "cc-by-4.0"
ROOT = Path.home() / ".local/state/evallab-cross-test/Hubble42__ResolveBench" / REVISION[:12]
HUB = "https://huggingface.co"

NOTICE = """# CROSS-TEST ONLY: Hubble42/ResolveBench

Source: https://huggingface.co/datasets/Hubble42/ResolveBench at dataset commit 6e8f58fd29aa794d8b8eb9e3e01ffe44b48f4b0e
(project: resolvebench.com, by Hubble42 Inc.). Licence: CC-BY-4.0 (README YAML front matter and the card's License
section, staged; there is no LICENSE file). The card is silent about the terms of the agent models whose outputs it
republishes. Public, ungated. Staged: all 4 files of the dataset (`resolvebench_runs.parquet`,
`resolvebench_tasks.parquet`, README.md, .gitattributes), 4,785,737 bytes.

- Local-only, evaluation-only. No training, tuning, few-shot, retrieval or prompt development on this bundle.
- Frozen-design one-shot cross-test: freeze detector code, prompts and threshold by digest before first exposure.
- No upload to Laminar, Docent or any vendor project; no ingestion into the production database or content store; no rehosting.
- Hosted-model evaluation calls only as allowed by CONTRACT.md for permissive (CC-BY) cross-test sets; log paid calls.
  The runs are outputs of commercial and open-weight models (Claude, GPT, Kimi); the card does not say under which
  model terms they are released: record that caveat in any hosted-model log.
- Construct: customer-support agents (Airlines, Hotels, Utilities; mock tools) whose runs are graded on the database
  end-state. A run is `safety_violation` when its trajectory calls a tool from the task's `must_not_call` list: an
  operational policy violation by a support agent, not coding-agent cheating. The label is derived by the benchmark's
  grader from the tool names, so it is deterministic, not judged.
- Detector input is the adapter's `_steps` only: it excludes every grader-side field (`must_not_call`, `required_tools`,
  `required_evidence`, `golden_action`, `golden_trajectory`, `passed`, `safety_violation`, `composite_score`, `score_*`,
  `score_notes`, `final_action`, the per-call `prohibited` and `ok` flags) and the model/trial identity.
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob(path: Path) -> str:
    digest = hashlib.sha1(b"blob %d\0" % path.stat().st_size)  # noqa: S324
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hub_tree() -> list[dict]:
    url = f"{HUB}/api/datasets/{REPO}/tree/{REVISION}?recursive=1&expand=0"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        return [e for e in json.load(response) if e["type"] == "file"]


def intact(path: Path, entry: dict) -> bool:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    if "lfs" in entry:
        return sha256_file(path) == entry["lfs"]["oid"]
    return git_blob(path) == entry["oid"]


def fetch(entry: dict) -> None:
    path = ROOT / entry["path"]
    if intact(path, entry):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HUB}/datasets/{REPO}/resolve/{REVISION}/{entry['path']}"
    with urllib.request.urlopen(url, timeout=600) as response, path.open("wb") as out:  # noqa: S310
        for block in iter(lambda: response.read(1 << 20), b""):
            out.write(block)
    if not intact(path, entry):
        raise ValueError(f"download differs from the Hub tree at the pin: {entry['path']}")


def main() -> None:
    tree = sorted(hub_tree(), key=lambda e: e["path"])
    ROOT.mkdir(parents=True, exist_ok=True)
    for entry in tree:
        fetch(entry)
    files = []
    for entry in tree:
        path = ROOT / entry["path"]
        item = {"path": entry["path"], "bytes": entry["size"], "sha256": sha256_file(path)}
        if "lfs" in entry:
            item["lfs_oid_sha256"] = entry["lfs"]["oid"]
        else:
            item["git_blob"] = entry["oid"]
        files.append(item)
    manifest = {
        "repo": REPO,
        "revision": REVISION,
        "licence": LICENCE,
        "gated": False,
        "source_url": f"{HUB}/datasets/{REPO}",
        "licence_evidence": (
            "Card YAML front matter at the pinned commit (README.md, staged) declares `license: cc-by-4.0`, and the "
            "card's License section says 'Released under CC BY 4.0'; the Hub API cardData for the pinned revision "
            "agrees; the dataset is public (private=false) and ungated (gated=false). There is no LICENSE file in "
            "the repository (4 files listed at the pin). The card says nothing about the terms of the agent models "
            "(Claude, GPT, Kimi) whose outputs it republishes."
        ),
        "model_terms_caveat": (
            "Agent outputs come from commercial and open-weight models; the card does not state their terms. The "
            "CC-BY-4.0 grant covers the dataset as published by Hubble42, not a relicence of model outputs."
        ),
        "subset": "Whole dataset at the pin: all 4 files (runs and tasks parquet, card, .gitattributes).",
        "pin_note": (
            "Hub tree listing at the pinned commit; every file's size and LFS sha256 (2 parquet files) or git "
            "blob sha1 (README.md, .gitattributes) equals the Hub tree entry. Downloaded by tools/stage_bundle.py "
            "(stdlib HTTPS, resolve/<revision>)."
        ),
        "counts": {
            "publisher_rows": {"runs": 6400, "tasks": 100},
            "counts_source": "tools/profile.py results/profile.json in eval-lab (corpus-expansion-pq8-20261029)",
        },
        "files": files,
        "total_bytes": sum(f["bytes"] for f in files),
    }
    (ROOT / "STAGE-MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n")
    (ROOT / "CROSS_TEST_ONLY.md").write_text(NOTICE)
    sums = {f["path"]: f["sha256"] for f in files}
    sums["CROSS_TEST_ONLY.md"] = sha256_file(ROOT / "CROSS_TEST_ONLY.md")
    sums["STAGE-MANIFEST.json"] = sha256_file(ROOT / "STAGE-MANIFEST.json")
    (ROOT / "SHA256SUMS").write_text("".join(f"{d}  {p}\n" for p, d in sorted(sums.items())))
    for path in sorted([ROOT, *ROOT.rglob("*")], key=lambda p: len(p.parts), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    print(json.dumps({"files": len(files), "bytes": manifest["total_bytes"]}))


if __name__ == "__main__":
    main()
