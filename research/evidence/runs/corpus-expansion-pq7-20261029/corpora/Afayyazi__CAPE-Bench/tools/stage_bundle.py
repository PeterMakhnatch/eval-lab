#!/usr/bin/env python3
"""Stage Afayyazi/CAPE-Bench as a locked, local-only cross-test bundle (stdlib only).

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

REPO = "Afayyazi/CAPE-Bench"
REVISION = "3ec1c45e98ab678541438a5ed7b07ed8d2306cb0"
LICENCE = "cc-by-4.0"
ROOT = Path.home() / ".local/state/evallab-cross-test/Afayyazi__CAPE-Bench" / REVISION[:12]
HUB = "https://huggingface.co"

NOTICE = """# CROSS-TEST ONLY: Afayyazi/CAPE-Bench

Source: https://huggingface.co/datasets/Afayyazi/CAPE-Bench at dataset commit 3ec1c45e98ab678541438a5ed7b07ed8d2306cb0
(paper: "The Agent Knew", arXiv 2610.17301). Licence: CC-BY-4.0 (README YAML front matter, staged; there is no
LICENSE file). The card adds that agent outputs stay subject to the respective models' terms of use. Public, ungated.
Staged: all 195 files of the dataset (101 `episodes/*.parquet`, 49 `scenarios/*.parquet`, 43 `report_labels/*.parquet`,
README.md, .gitattributes), 428,958,495 bytes.

- Local-only, evaluation-only. No training, tuning, few-shot, retrieval or prompt development on this bundle.
- Frozen-design one-shot cross-test: freeze detector code, prompts and threshold by digest before first exposure.
- No upload to Laminar, Docent or any vendor project; no ingestion into the production database or content store; no rehosting.
- Hosted-model evaluation calls only as allowed by CONTRACT.md for permissive (CC-BY) cross-test sets; log paid calls.
  The card says agent outputs remain subject to each agent model's terms of use: record that in any hosted-model log.
- Construct: NATURAL (not instructed) violations by four open-weight agents against a written policy, in matched mock-tool
  worlds that differ in ONE fact. Environment labels are ground truth for the violation; report-honesty labels are a judge
  model's (precision 0.89-0.91 per the card). Negatives are not certified benign. Not coding-agent reward hacking.
- Detector input is the adapter's `_steps` only: it excludes every label and oracle field (`unsafe_action`, `critical`,
  `escalation`, `violations`, `unsafe_episode`, `informed_violation`, `first_unsafe_event`, `reveal_event`, `cf_value`,
  `nuisance`, gate fields, run names that end in `_minus`/`_plus`, `report_labels`, token positions). The monitor-aware
  variants (`*_reasoning`, `*_obfuscate`, `v3aware_*`, `v10enf`, `v19orcenf`, `v6resp`) are separate slices.
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
            "Card YAML front matter at the pinned commit (README.md, staged) declares `license: cc-by-4.0`; the Hub "
            "API cardData for the pinned revision agrees; the dataset is public (private=false) and ungated "
            "(gated=false). There is no LICENSE file in the repository (195 files listed at the pin). The card's "
            "'Intended use and limitations' adds: agent outputs were produced with the respective models and remain "
            "subject to those models' terms of use (Qwen3.8-27B, gpt-oss-20b, Gemma-4-26B-A4B, Muse-Glimmer-30B and the "
            "five smoke-run models)."
        ),
        "model_terms_caveat": (
            "Agent outputs (reasoning, tool calls, reports) remain subject to the terms of use of the model that "
            "produced them; the CC-BY-4.0 grant covers the dataset, not a relicence of model outputs."
        ),
        "subset": "Whole dataset at the pin: all 195 files (episodes, scenarios, report_labels configs, card).",
        "pin_note": (
            "Hub tree listing at the pinned commit; every file's size and LFS sha256 (193 parquet files) or git "
            "blob sha1 (README.md, .gitattributes) equals the Hub tree entry. Downloaded by tools/stage_bundle.py "
            "(stdlib HTTPS, resolve/<revision>)."
        ),
        "counts": {
            "publisher_rows": {"episodes": 206697, "scenarios": 45864, "report_labels": 14093},
            "counts_source": "tools/profile.py results/profile.json in eval-lab (corpus-expansion-pq7-20261029)",
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
