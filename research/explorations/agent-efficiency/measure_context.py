"""Replay pinned and current context compilers against the same documentation bytes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

from evallab import contextpack

CASES = (
    ("builder", None),
    ("analyst", None),
    ("runner", None),
    ("operator", None),
    ("builder", ("src/evallab/contextpack.py",)),
    ("builder", ("src/evallab/traj.py",)),
    ("analyst", ("src/evallab/token_flow.py",)),
    ("analyst", ("src/evallab/analysis_worker.py",)),
    ("runner", ("src/evallab/runner.py",)),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=contextpack.repo_root())
    parser.add_argument(
        "--corpus-root", type=Path, help="Frozen document root containing docs/; defaults to --root"
    )
    parser.add_argument("--baseline", default="5171c20c7d1f5c00d020db6087eac3041f94bf13")
    parser.add_argument("--budget", type=int, action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    corpus_root = (args.corpus_root or root).resolve()
    if not (corpus_root / "docs").is_dir():
        parser.error("the document corpus root must contain docs/")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline_bytes = subprocess.check_output(
        ["git", "show", f"{args.baseline}:src/evallab/contextpack.py"], cwd=root
    )
    baseline_source = output / "baseline_contextpack.py"
    baseline_source.write_bytes(baseline_bytes)
    spec = importlib.util.spec_from_file_location(
        "efficiency_baseline_contextpack", baseline_source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load pinned baseline compiler")
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    corpus = [
        {"path": str(path.relative_to(corpus_root)), "sha256": digest(path.read_bytes())}
        for path in sorted((corpus_root / "docs").rglob("*.md"))
        if path.is_file()
    ]
    rows = []
    for budget in args.budget or [12000]:
        for mission, paths in CASES:
            case = mission + ("-scoped-" + Path(paths[0]).stem if paths else "-default")
            for arm, compiler in (("baseline", baseline), ("candidate", contextpack)):
                started = time.perf_counter()
                result = compiler.build_context_pack(
                    mission,
                    root=corpus_root,
                    paths=paths,
                    token_budget=budget,
                )
                seconds = time.perf_counter() - started
                repeat = compiler.build_context_pack(
                    mission,
                    root=corpus_root,
                    paths=paths,
                    token_budget=budget,
                )
                if result.markdown != repeat.markdown or result.to_dict() != repeat.to_dict():
                    raise RuntimeError(f"non-deterministic compiler: {arm} {case} {budget}")
                pack_bytes = result.markdown.encode()
                filename = f"{case}-{budget}-{arm}.md"
                (output / filename).write_bytes(pack_bytes)
                rows.append(
                    {
                        "case": case,
                        "arm": arm,
                        "budget": budget,
                        "bytes": len(pack_bytes),
                        "chars": len(result.markdown),
                        "estimated_tokens": result.estimated_tokens,
                        "token_counter": "compiler_chars_divided_by_four_estimate_not_provider_usage",
                        "sha256": digest(pack_bytes),
                        "seconds": seconds,
                        "deterministic_replay": True,
                        "current_state_retained": any(d.path == "docs/NOW.md" for d in result.docs),
                        "pack_file": filename,
                        "metadata": result.to_dict(),
                    }
                )
    report = {
        "baseline_revision": args.baseline,
        "baseline_source_sha256": digest(baseline_bytes),
        "candidate_source_sha256": digest(Path(contextpack.__file__).read_bytes()),
        "corpus": corpus,
        "corpus_root": str(corpus_root),
        "corpus_manifest_sha256": digest(json.dumps(corpus, sort_keys=True).encode()),
        "quality_limit": "Context retention and instruction correctness are not coding-success parity.",
        "rows": rows,
    }
    (output / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "cases": len(CASES),
                "rows": len(rows),
                "corpus_manifest_sha256": report["corpus_manifest_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
