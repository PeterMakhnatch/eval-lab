"""Pinned, local-only dataset admission with a pushed pre-scoring barrier."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EXPERIMENT = Path("research/experiments/ov1105-hackbench")
REVISION = "b3d6f678e6359dcd35e20f3f2c5ee085b5af4934"
DATA = "data/hackbench_169_gpt55_glm52.numbered.jsonl"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def external(path: Path) -> Path:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Raw intake, views and receipts must remain outside Git checkouts")
    return path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def module(root: Path, filename: str) -> Any:
    directory = root / EXPERIMENT
    sys.path.insert(0, str(directory))
    name = "admission_" + filename.removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, directory / filename)
    if spec is None or spec.loader is None:
        raise ValueError(f"Adapter unavailable: {filename}")
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def access(repo: str, revision: str, filename: str) -> dict[str, Any]:
    """Read real data bytes; API metadata and a successful listing do not qualify."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Provide an org/name dataset and a full 40-hex revision")
    if Path(filename).is_absolute() or ".." in Path(filename).parts:
        raise ValueError("Provide a relative dataset data file")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is absent; use keys run -- uv run evallab admit ...")
    url = f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{urllib.parse.quote(filename)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            block = response.read(1024)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Dataset access refused: {repo}, HTTP {exc.code}. No admission or scoring. Request publisher access and rerun with keys run.") from None
    except urllib.error.URLError:
        raise ValueError(f"Dataset download failed: {repo}; no admission or scoring") from None
    if not block:
        raise ValueError("Access probe returned no data bytes; admission refused")
    return {"repo": repo, "revision": revision, "file": filename, "downloaded_bytes": len(block),
            "prefix_sha256": hashlib.sha256(block).hexdigest()}


def tree_digest(directory: Path) -> str:
    files = {str(p.relative_to(directory)): digest(p) for p in sorted(directory.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def source_hashes(root: Path) -> dict[str, str]:
    paths = sorted((root / "src/evallab").rglob("*.py"))
    for directory in (EXPERIMENT, Path("research/experiments/ov1104-detbench"),
                      Path("research/evidence/runs/corpus-expansion-lb6-20261027/tools")):
        paths.extend(sorted((root / directory).glob("*.py")))
    paths.extend(root / EXPERIMENT / name for name in ("label-map.json", "PREREG.md"))
    return {str(p.relative_to(root)): digest(p) for p in paths}


def prepare(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    output = external(args.output)
    registration = args.registration.resolve()
    if not registration.is_relative_to(root.resolve() / "research/experiments"):
        raise ValueError("Registration must be under research/experiments")
    if output.exists() or registration.exists():
        raise ValueError("Use new output and registration paths; preserve existing admission evidence")
    intake = module(root, "intake.py")
    cache = external(args.cache or intake.DEFAULT_CACHE)
    # Every invocation probes the data download, even if the pinned cache exists.
    probe = access("facebook/hackbench", REVISION, DATA)
    output.mkdir(parents=True)
    # A new download proves reproducibility without replacing another intake's bytes.
    downloaded = output / "source-download" if cache.exists() else cache
    intake.stage.fetch(downloaded)
    manifest = intake.build_manifest(downloaded)
    if downloaded != cache:
        cached = intake.build_manifest(cache)
        if cached != manifest:
            raise ValueError("Fresh download differs from the immutable pinned cache")
    save(output / "access.json", probe)
    save(output / "manifest.json", manifest)
    # README contains the publisher's license AND additional no-training/no-rehosting terms.
    (output / "publisher-terms.md").write_bytes((cache / "README.md").read_bytes())
    adapter = module(root, "adapter.py")
    conversion = adapter.convert(downloaded, output / "converted")
    if conversion["counts"]["conversion_failures"] or conversion["counts"]["excluded"]:
        raise ValueError("Conversion incomplete; no pre-registration or scoring")
    # Exact ID join against actual prior detector receipts, not an assumption based on the dataset name.
    prior = external(args.prior_exposure)
    prior_ids = {r["trace_key"] for r in map(json.loads, prior.read_text().splitlines()) if r}
    source_ids = set()
    with (cache / DATA).open() as stream:
        for line in stream:
            source_ids.add(hashlib.sha256(json.loads(line)["id"].encode()).hexdigest()[:16])
    audit = {"method": "exact sha256(publisher id)[:16] join to prior CM-11 trace_key",
             "prior_receipt_sha256": digest(prior), "source_records": len(source_ids),
             "prior_exposed": len(source_ids & prior_ids), "unmatched": len(source_ids - prior_ids)}
    if audit["unmatched"]:
        raise ValueError("Prior exposure census disagrees with the reused HackBench label map")
    save(output / "exposure.json", audit)
    (output / "label-map.json").write_bytes((root / EXPERIMENT / "label-map.json").read_bytes())
    evaluator = module(root, "evaluate.py")
    text = (root / EXPERIMENT / "PREREG.md").read_text()
    block = re.search(r"```json\n(.*?)\n```", text, re.S)
    if block is None:
        raise ValueError("Missing frozen DetBench configuration")
    historical = json.loads(block[1])
    config = {key: historical[key] for key in ("local_thresholds", "length_version", "l0_version", "line_version", "line_thresholds")}
    # The historical orchestration's source-tree pin is not a current runtime claim.
    config["l0_version"] = "admission-l0-now@" + digest(root / "research/experiments/ov1104-detbench/l0_rules.py")[:12]
    rows = evaluator.read_rows(output / "converted/runs.jsonl")
    freeze = {"schema": "dataset-admission/v1", "repo": "facebook/hackbench", "revision": REVISION,
              "manifest_sha256": digest(output / "manifest.json"), "cache": str(cache),
              "converted_sha256": tree_digest(output / "converted"), "config": config,
              "source_sha256": source_hashes(root), "exposure": audit,
              "counts": conversion["counts"], "command_blocks": conversion["command_blocks"],
              "terms_sha256": digest(output / "publisher-terms.md"),
              "label_map_sha256": digest(output / "label-map.json"),
              "scope": "All released rows; local DetBench L0 and length only; no model calls; no tuning",
              "rows": len(rows), "statistics": "DetBench task-cluster bootstrap, 1000 resamples, seed 1104, 95% intervals"}
    save(registration, freeze)
    save(output / "freeze.json", freeze)
    intake.stage.lock(downloaded)
    return {"status": "prepared_no_scores", "registration": str(registration), "output": str(output),
            "counts": freeze["counts"], "command_blocks": freeze["command_blocks"], "exposure": audit,
            "next": "Commit and push registration and code, then admit score --commit <pushed SHA>"}


def check_registration(root: Path, registration: Path, commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Scoring requires a full pushed pre-registration commit")
    relative = registration.resolve().relative_to(root.resolve())
    # Query the server, not stale remote-tracking branches or an operator attestation.
    refs = git(root, "ls-remote", "--heads", "origin").splitlines()
    if not any(line.split()[0] == commit for line in refs):
        raise ValueError("Pre-registration commit is not a current pushed branch head")
    if git(root, "merge-base", "--is-ancestor", commit, "HEAD"):
        raise ValueError("Pre-registration is not an ancestor of HEAD")
    blob = subprocess.check_output(["git", "show", f"{commit}:{relative}"], cwd=root)
    if blob != registration.read_bytes():
        raise ValueError("Registration differs from pushed commit")
    freeze = load(registration)
    for name, expected in freeze["source_sha256"].items():
        committed = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root)
        if hashlib.sha256(committed).hexdigest() != expected or digest(root / name) != expected:
            raise ValueError(f"Frozen code or label map changed: {name}")
    if source_hashes(root) != freeze["source_sha256"]:
        raise ValueError("Frozen source inventory changed")


def score(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    registration = args.registration.resolve()
    check_registration(root, registration, args.commit)
    freeze = load(registration)
    output = external(args.output)
    if load(output / "freeze.json") != freeze:
        raise ValueError("Output and pushed registration disagree")
    intake = module(root, "intake.py")
    intake.verify_source(external(Path(freeze["cache"])))
    if digest(output / "manifest.json") != freeze["manifest_sha256"] or tree_digest(output / "converted") != freeze["converted_sha256"]:
        raise ValueError("Intake or converted views changed after pre-registration")
    for name, key in (("publisher-terms.md", "terms_sha256"), ("label-map.json", "label_map_sha256")):
        if digest(output / name) != freeze[key]:
            raise ValueError(f"Frozen rights or labels changed: {name}")
    # Exclusive creation forbids retries after observing results.
    with (output / "SCORING.json").open("x") as handle:
        json.dump({"preregistration_commit": args.commit, "freeze_sha256": digest(registration)}, handle)
    evaluator = module(root, "evaluate.py")
    rows = evaluator.read_rows(output / "converted/runs.jsonl")
    for row in rows:
        for key in ("view", "view_actions_only"):
            path = Path(row[key])
            row[key] = str(path if path.is_absolute() else output / "converted" / path)
    signals = evaluator.local_signals(rows, freeze["config"], output)
    metrics = evaluator.module("admission_metrics", root / "research/experiments/ov1104-detbench/score.py")
    report: dict[str, Any] = {"preregistration_commit": args.commit, "counts": freeze["counts"],
                              "command_blocks": freeze["command_blocks"], "exposure": freeze["exposure"],
                              "scope": freeze["scope"], "signals": {}}
    for name, entries in signals.items():
        thresholds = freeze["config"]["local_thresholds"].get(name, freeze["config"]["line_thresholds"])
        result = evaluator.summary(rows, entries, thresholds, metrics, freeze["config"])
        for baseline in result["matched_length"].values():
            baseline.pop("run_keys", None)  # Aggregate-only publication.
        report["signals"][name] = result
    save(output / "aggregate-results.json", report)
    return report


def command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        if args.admit_command == "access":
            result = access(args.dataset, args.revision, args.file)
        elif args.admit_command == "prepare":
            result = prepare(root, args)
        elif args.admit_command in {"preregister", "pr"}:
            from evallab.admission_delivery import preregister, publish

            result = (preregister if args.admit_command == "preregister" else publish)(root, args)
        else:
            result = score(root, args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Never emit remote response bodies, signed URLs, headers or token-bearing tracebacks.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Admission refused: {message}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


def register_parser(commands: Any) -> None:
    parser = commands.add_parser("admit", help="Pinned dataset admission and pushed pre-scoring barrier")
    phases = parser.add_subparsers(dest="admit_command", required=True)
    probe = phases.add_parser("access", help="Download real data bytes; refuse inaccessible gated sources")
    probe.add_argument("dataset")
    probe.add_argument("--revision", required=True)
    probe.add_argument("--file", required=True)
    prepare_parser = phases.add_parser("prepare", help="Admit the approved HackBench profile without scoring")
    prepare_parser.add_argument("--cache", type=Path)
    prepare_parser.add_argument("--prior-exposure", type=Path, required=True)
    for phase in (prepare_parser, phases.add_parser("score", help="Score frozen local detectors and length baselines")):
        phase.add_argument("--output", type=Path, required=True)
        phase.add_argument("--registration", type=Path, required=True)
        phase.set_defaults(func=command)
        if phase is not prepare_parser:
            phase.add_argument("--commit", required=True)
    probe.set_defaults(func=command)
    registration = phases.add_parser("preregister", help="Commit and push the frozen registration before scoring")
    registration.add_argument("--registration", type=Path, required=True)
    registration.set_defaults(func=command)
    publication = phases.add_parser("pr", help="Commit aggregate results, push and create the admission PR")
    publication.add_argument("--registration", type=Path, required=True)
    publication.add_argument("--output", type=Path, required=True)
    publication.set_defaults(func=command)
