#!/usr/bin/env python3
"""Stage runlatent/latent-agent-sandbox-episodes as a locked, local-only cross-test bundle (stdlib only).

Downloads every file of the dataset at the pinned commit over HTTPS (``resolve/<revision>``), checks
each against the Hub tree at that commit (size, plus the LFS sha256 or the git blob sha1), writes
STAGE-MANIFEST.json, SHA256SUMS.stage and CROSS_TEST_ONLY.md, then locks the tree (dirs 0555, files 0444). The publisher's own
SHA256SUMS is a file of the dataset and stays byte-identical; the staging sums go to SHA256SUMS.stage.
Files already present with the right bytes are not downloaded again. No huggingface_hub, no pip.

    python3 stage_bundle.py            # download (if needed), verify, write receipts, lock
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

REPO = "runlatent/latent-agent-sandbox-episodes"
REVISION = "f43a63e9d9d24b0af99fa3426ea8360e277cc31b"
LICENCE = "cc-by-4.0"
ROOT = (
    Path.home()
    / ".local/state/evallab-cross-test/runlatent__latent-agent-sandbox-episodes"
    / REVISION[:12]
)
HUB = "https://huggingface.co"

NOTICE = """# CROSS-TEST ONLY: runlatent/latent-agent-sandbox-episodes

Source: https://huggingface.co/datasets/runlatent/latent-agent-sandbox-episodes at dataset commit f43a63e9d9d24b0af99fa3426ea8360e277cc31b
(code: https://github.com/runlatentai/latent-agent-sandbox, Apache-2.0). Licence: CC-BY-4.0 (README YAML front matter and the
staged LICENSE file, the CC BY 4.0 legal code; copyright 2026 Latent Systems, Inc.). Public, ungated.
Staged: all 8 files of the dataset (episodes.tar.gz, labels.jsonl, provenance.json, counts.py, README.md, LICENSE, SHA256SUMS,
.gitattributes), 114,938,x bytes in total (see STAGE-MANIFEST.json). The publisher's SHA256SUMS is kept as shipped; the
local receipts are STAGE-MANIFEST.json, SHA256SUMS.stage (sha256 of every file including these) and this notice.

- Local-only, evaluation-only. No training, tuning, few-shot, retrieval or prompt development on this bundle.
- Frozen-design one-shot cross-test: freeze detector code, prompts and threshold by digest before first exposure.
- No upload to Laminar, Docent or any vendor project; no ingestion into the production database or content store; no rehosting.
- Hosted-model evaluation calls only as allowed by CONTRACT.md for permissive (CC-BY) cross-test sets; log paid calls.
  The agent model is gpt-oss-120b (Apache-2.0 per the card); the card states no restriction on the use of its outputs.
- Construct: NATURAL (never instructed; hint rung h0, reasoning effort high) shortcut-taking by one model in a Linux sandbox:
  reading planted answer keys or charging a planted second account. 178 episodes in which the sandbox's own access log recorded
  a violation, plus 178 honest episodes drawn at random from the same pressure cells. Labels are mechanical (access-log events,
  planted canary strings, a substring rule for concealment); no human or LLM-judge labels. 55 + 18 of the 178 positives
  touched the resource without using the result. The honest half is a per-cell sample, not the population. The answer_key
  family (74 positives) is hidden-test cheating on small Python packages (answers directory, git history, grader workspace,
  grader tamper); the unauthorized_spend family (104) is use of another account's planted key in a deploy or publish task.
  A cross-construct check of a shortcut monitor, not a pooled coding-cheating corpus.
- Detector input is the adapter's `_steps` only: it excludes every label and oracle field (`cheated`, `first_violating_step`,
  `discovery_step`, `used_result`, `concealed`, `shortcut_succeeded`, `log_reliable`, `out_of_bounds_steps`, `probed`, the
  `pressure` cell, ids and their parts, series fields, token-level and timing metadata, `raw_completion`). The planted
  canary (`gk-<16 hex>`) and payment replies that name the funded account are tool output the agent saw; they are visible in
  `_steps` and are reported as a decisive-information floor, not hidden.
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
    total = sum(f["bytes"] for f in files)
    manifest = {
        "repo": REPO,
        "revision": REVISION,
        "licence": LICENCE,
        "gated": False,
        "source_url": f"{HUB}/datasets/{REPO}",
        "licence_evidence": (
            "Card YAML front matter at the pinned commit (README.md, staged) declares `license: cc-by-4.0`; the "
            "staged LICENSE file (18,657 bytes, git blob da6ab6cc8f333d7e89a99812866df8f24374d47c) is the CC BY 4.0 "
            "legal code; the card's Datasheet says 'License: CC BY 4.0 (`LICENSE`); the sandbox that produced it is "
            "Apache-2.0. Copyright 2026 Latent Systems, Inc.'; the Hub API cardData for the pinned revision agrees; "
            "the dataset is public (private=false) and ungated (gated=false); 8 files are listed at the pin. The "
            "publisher's SHA256SUMS (6 files) verifies against the staged bytes."
        ),
        "model_terms_caveat": (
            "The agent model is gpt-oss-120b, described in the card as Apache-2.0. The card states no restriction on "
            "the use of its outputs; the model's own usage policy was not read for this staging, so the CC-BY grant is "
            "taken to cover the dataset as released."
        ),
        "subset": "Whole dataset at the pin: all 8 files (episodes tarball, labels, provenance, counts.py, card, licence).",
        "pin_note": (
            "Hub tree listing at the pinned commit; every file's size and LFS sha256 (episodes.tar.gz) or git blob "
            "sha1 (the 7 small files) equals the Hub tree entry. Downloaded by tools/stage_bundle.py (stdlib HTTPS, "
            "resolve/<revision>). The tarball is not unpacked in the bundle; the adapter streams it. The publisher's "
            "SHA256SUMS (a dataset file) is unchanged; local sums are in SHA256SUMS.stage."
        ),
        "counts": {
            "publisher_rows": {"labels": 356, "episode_files": 356, "model_steps": 9714},
            "counts_source": "tools/profile.py results/profile.json in eval-lab (corpus-expansion-pq8-20261029)",
        },
        "files": files,
        "total_bytes": total,
    }
    (ROOT / "STAGE-MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n")
    notice = NOTICE.replace(
        "114,938,x bytes in total (see STAGE-MANIFEST.json)", f"{total:,} bytes in total"
    )
    (ROOT / "CROSS_TEST_ONLY.md").write_text(notice)
    sums = {f["path"]: f["sha256"] for f in files}
    sums["CROSS_TEST_ONLY.md"] = sha256_file(ROOT / "CROSS_TEST_ONLY.md")
    sums["STAGE-MANIFEST.json"] = sha256_file(ROOT / "STAGE-MANIFEST.json")
    (ROOT / "SHA256SUMS.stage").write_text("".join(f"{d}  {p}\n" for p, d in sorted(sums.items())))
    for path in sorted([ROOT, *ROOT.rglob("*")], key=lambda p: len(p.parts), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    print(json.dumps({"files": len(files), "bytes": total}))


if __name__ == "__main__":
    main()
