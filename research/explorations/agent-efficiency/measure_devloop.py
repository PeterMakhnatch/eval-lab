"""Compare actual consumer reads/parses and complete plans on the same inventory."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
import types
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


def load_module(name: str, source: str):
    module = types.ModuleType(name)
    module.__file__ = name
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def synthetic_inventory(root: Path) -> list[str]:
    roots = ["tests", "tests/nested", "dashboard/tests", "research/analysis/tests"]
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = " + json.dumps(roots) + "\n"
    )
    for directory in roots[1:]:
        target = root / directory
        target.mkdir(parents=True)
        for mask in range(8):
            names = [n for bit, n in enumerate(("alpha", "beta", "gamma")) if mask & (1 << bit)]
            content = "from evallab import " + ", ".join(names or ["unrelated"]) + "\n"
            filename = f"test_consumer_{mask}.py" if mask % 2 else f"consumer_{mask}_test.py"
            (target / filename).write_text(content)
    (root / "tests/support.py").write_text("from evallab import alpha\n")
    return [
        "src/evallab/alpha.py",
        "tests/support.py",
        "src/evallab/beta.py",
        "src/evallab/gamma.py",
    ]


def measure(module, root: Path, changed: list[str]) -> dict:
    reads: Counter[str] = Counter()
    parses: Counter[str] = Counter()
    original_read, original_parse = Path.read_text, ast.parse

    def relative_python(filename):
        try:
            path = Path(filename).resolve().relative_to(root)
        except (TypeError, ValueError):
            return None
        return path.as_posix() if path.suffix == ".py" else None

    def counted_read(path, *args, **kwargs):
        relative = relative_python(path)
        if relative is not None:
            reads[relative] += 1
        return original_read(path, *args, **kwargs)

    def counted_parse(source, filename="<unknown>", *args, **kwargs):
        relative = relative_python(filename)
        if relative is not None:
            parses[relative] += 1
        return original_parse(source, filename, *args, **kwargs)

    with patch.object(Path, "read_text", counted_read), patch.object(ast, "parse", counted_parse):
        plan = asdict(module.plan_devloop(changed, root))
    return {
        "read_text_calls": sum(reads.values()),
        "ast_parse_calls": sum(parses.values()),
        "reads_by_consumer": dict(sorted(reads.items())),
        "parses_by_consumer": dict(sorted(parses.items())),
        "plan": plan,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changed", nargs="*")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--baseline", default="5171c20c7d1f5c00d020db6087eac3041f94bf13")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    relative = "src/evallab/devloop.py"
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:{relative}"], cwd=root
    ).decode()
    baseline = load_module("efficiency_devloop_baseline", source)
    candidate = load_module("efficiency_devloop_candidate", (root / relative).read_text())

    def compare(inventory_root: Path, changed: list[str]) -> dict:
        before = measure(baseline, inventory_root, changed)
        after = measure(candidate, inventory_root, changed)
        return {
            "baseline_revision": args.baseline,
            "synthetic": args.synthetic,
            "changed": changed,
            "identical_plan": before["plan"] == after["plan"],
            "baseline": before,
            "candidate": after,
            "quality_limit": "Local computation savings do not measure model tokens or transitive impact coverage.",
        }

    if args.synthetic:
        if args.changed:
            parser.error("--synthetic supplies its own changed paths")
        with tempfile.TemporaryDirectory(prefix="devloop-efficiency-") as temporary:
            fixture = Path(temporary).resolve()
            report = compare(fixture, synthetic_inventory(fixture))
    else:
        if not args.changed:
            parser.error("supply changed paths or --synthetic")
        report = compare(root, args.changed)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "identical_plan": report["identical_plan"],
                "baseline": {
                    k: report["baseline"][k] for k in ("read_text_calls", "ast_parse_calls")
                },
                "candidate": {
                    k: report["candidate"][k] for k in ("read_text_calls", "ast_parse_calls")
                },
            },
            indent=2,
        )
    )
    return 0 if report["identical_plan"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
