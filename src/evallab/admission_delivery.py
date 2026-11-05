"""Git delivery phases for dataset admission; no raw evidence enters Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from evallab.admission import digest, external, git, load, save, source_hashes


def registration_path(root: Path, path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(root.resolve() / "research/experiments"):
        raise ValueError("Registration must be under research/experiments")
    return path


def preregister(root: Path, args: argparse.Namespace) -> dict[str, str]:
    registration = registration_path(root, args.registration)
    freeze = load(registration)
    if source_hashes(root) != freeze["source_sha256"]:
        raise ValueError("Implementation changed after preparation; scoring remains blocked")
    # Commit only the registration; another writer's staged files never enter this commit.
    for name, expected in freeze["source_sha256"].items():
        committed = subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=root)
        if hashlib.sha256(committed).hexdigest() != expected:
            raise ValueError(f"Commit the implementation before registration: {name}")
    relative = str(registration.relative_to(root.resolve()))
    committed = subprocess.run(["git", "show", f"HEAD:{relative}"], cwd=root, capture_output=True)
    if committed.returncode == 0 and committed.stdout != registration.read_bytes():
        raise ValueError("Existing committed registration is immutable; use a new registration path")
    if committed.returncode != 0:
        git(root, "add", "--", relative)
        git(root, "commit", "-m", "research: preregister dataset admission before scoring", "--", relative)
    commit = git(root, "rev-parse", "HEAD")
    git(root, "push", "-u", "origin", "HEAD")
    return {"status": "preregistered_and_pushed", "commit": commit, "registration": relative}


def publish(root: Path, args: argparse.Namespace) -> dict[str, str]:
    registration = registration_path(root, args.registration)
    output = external(args.output)
    freeze = load(registration)
    if load(output / "freeze.json") != freeze:
        raise ValueError("Output does not match registration")
    scoring = load(output / "SCORING.json")
    if scoring["freeze_sha256"] != digest(registration):
        raise ValueError("Scoring receipt does not match registration")
    relative = str(registration.relative_to(root.resolve()))
    committed = subprocess.check_output(["git", "show", f"{scoring['preregistration_commit']}:{relative}"], cwd=root)
    if committed != registration.read_bytes():
        raise ValueError("Scoring registration differs from the recorded pre-scoring commit")
    report = load(output / "aggregate-results.json")
    if report["preregistration_commit"] != scoring["preregistration_commit"]:
        raise ValueError("Scoring report and execution receipt disagree")
    # Publish only the scorer's closed aggregate schema, never arbitrary output files.
    if set(report) != {"preregistration_commit", "counts", "command_blocks", "exposure", "scope", "signals"}:
        raise ValueError("Unexpected scoring report fields; publication refused")
    if any("run_keys" in baseline for signal in report["signals"].values()
           for baseline in signal["matched_length"].values()):
        raise ValueError("Per-record identities must not enter aggregate publication")
    destination = registration.parent / "aggregate-results.json"
    if destination.exists() and load(destination) != report:
        raise ValueError("Existing aggregate evidence differs; do not overwrite it")
    save(destination, report)
    relative_result = str(destination.relative_to(root.resolve()))
    if git(root, "status", "--porcelain", "--", relative_result):
        git(root, "add", "--", relative_result)
        git(root, "commit", "-m", "research: record preregistered dataset admission results", "--", relative_result)
    git(root, "push", "-u", "origin", "HEAD")
    branch = git(root, "branch", "--show-current")
    existing = subprocess.check_output(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"], cwd=root, text=True)
    prs = json.loads(existing)
    if prs:
        url = prs[0]["url"]
    else:
        body = (f"## Dataset admission\n\nPinned {freeze['repo']} at {freeze['revision']}.\n"
                f"Registration: `{relative}`. Pre-scoring commit: `{report['preregistration_commit']}`.\n"
                f"Aggregate evidence: `{relative_result}`.\n\n"
                "Raw bytes and converted views remain outside Git. Local DetBench detectors and frozen length baselines only.\n"
                "No labelled negatives: AUROC, FPR and superiority over length remain unavailable.\n\n"
                "Verification: real pinned download, exact prior-exposure join, gated HTTP 403 refusal, "
                "unpushed scoring refusal; `uv run pytest tests/test_admission.py`.\n")
        url = subprocess.check_output(["gh", "pr", "create", "--base", "main", "--head", branch,
                                       "--title", "feat: automate pinned dataset admission (W3)", "--body", body], cwd=root, text=True).strip()
    save(output / "PR.json", {"url": url, "head": git(root, "rev-parse", "HEAD")})
    return {"status": "pr_created", "url": url, "next": "uv run evallab ship <pr> from a stable checkout"}
