"""Receipt-bound program reports, separate from trajectory reports in report.py."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evallab.insights.check import unsupported_numbers
from evallab.insights.units import printed_tolerance

_PATH = re.compile(r"`([^`\n]+)`|\[[^\]]*\]\(([^)]+)\)")
_DIGIT = re.compile(r"\d")
_RATIO = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*=\s*(\d+(?:\.\d+)?)%")


@dataclass
class Fact:
    id: str
    lane: str
    file: str
    line: int
    quote: str
    receipts: list[str]
    numbers: list[str]
    status: str = "unchecked"


def _receipts(quote: str, source: Path, root: Path) -> list[str]:
    paths = []
    for match in _PATH.finditer(quote):
        value = match[1] or match[2]
        if value.startswith(("http:", "https:")):
            continue
        candidate = Path(value).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [source.parent / candidate]
        if candidate.parts and not candidate.is_absolute() and candidate.parts[0] in {
            "research", "src", "docs", "tests",
        }:
            candidates.append(root / candidate)
        for path in candidates:
            if path.is_file() and path.resolve() != source.resolve():
                paths.append(str(path.resolve()))
                break
    return sorted(set(paths))


def index_facts(program: Path, root: Path) -> list[Fact]:
    """Index every physical number-bearing line, including headings and code.

    Full lines preserve verbatim evidence and avoid semantic sentence guessing.
    IDs bind the source path, line, and bytes, not a mutable enumeration.
    """
    facts = []
    for source in sorted(program.glob("*/RESULT.md")):
        for line, quote in enumerate(source.read_text().splitlines(), 1):
            if not _DIGIT.search(quote):
                continue
            identity = f"{source.resolve()}:{line}:{quote}".encode()
            facts.append(Fact(
                id="F-" + hashlib.sha256(identity).hexdigest()[:16],
                lane=source.parent.name, file=str(source.resolve()), line=line, quote=quote,
                receipts=_receipts(quote, source, root),
                numbers=unsupported_numbers(quote, []),
            ))
    if not facts:
        raise ValueError(f"No number-bearing RESULT statements in {program}")
    return facts


def _check(fact: Fact) -> dict[str, Any]:
    """Presence is not verification. Check exact numeric support separately."""
    if not fact.receipts:
        return {"status": "missing_receipt", "unsupported": fact.numbers}
    oversized = [path for path in fact.receipts if Path(path).stat().st_size > 262144]
    if oversized:
        return {"status": "unchecked_large_receipt", "unsupported": fact.numbers,
                "reason": "Use a bounded receipt extract; receipt exceeds 256 KiB"}
    text = "\n".join(Path(path).read_text(errors="replace") for path in fact.receipts)
    missing = unsupported_numbers(fact.quote, [text])
    arithmetic = []
    for match in _RATIO.finditer(fact.quote.replace("`", "").replace("*", "")):
        numerator, denominator = (int(value.replace(",", "")) for value in match.group(1, 2))
        stated = float(match[3]) / 100
        actual = numerator / denominator if denominator else None
        passed = actual is not None and abs(actual - stated) <= printed_tolerance(match[3], unit="%") + 1e-12
        arithmetic.append({"quote": match.group(), "recomputed": actual, "passed": passed})
    status = "unsupported_numbers" if missing else "receipt_bound_unchecked"
    if any(not item["passed"] for item in arithmetic):
        status = "arithmetic_mismatch"
    return {"status": status, "unsupported": missing, "arithmetic": arithmetic,
            "receipt_sha256": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                               for path in fact.receipts}}


def _markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("`", "")


def render(facts: list[Fact], lanes: list[str], checks: dict[str, Any]) -> str:
    lines = ["# Program report", "", "The selected statements trace to local receipts.",
             "Receipt support does not establish scientific validity.",
             "Unchecked statements require independent verification.", ""]
    for lane in lanes:
        lines.extend([f"## Lane: {_markdown(lane)}", "", "| Statement | Fact | Check |",
                      "|---|---|---|"])
        selected = [fact for fact in facts if fact.lane == lane]
        for fact in selected:
            lines.append(f"| {_markdown(fact.quote)} | [{fact.id}](facts.json) | {fact.status} |")
        if not selected:
            lines.append("| No statements selected. See the complete facts index. | — | unchecked |")
        lines.append("")
    lines.extend(["## What failed", "", "Unselected statements remain in facts.json and checks.json.",
                  "No unsupported statement enters this draft."])
    outcomes = Counter(item.get("outcome", "unchecked") for name, item in checks.items()
                       if name != "availability")
    summary = ", ".join(f"{count} {outcome}" for outcome, count in sorted(outcomes.items()))
    lines.append(f"Lane verifier: {summary or 'unchecked'}. See verification.json.")
    return "\n".join(lines) + "\n"


def verify_lanes(program: Path, root: Path, output: Path) -> dict[str, Any]:
    """Use the integrated verifier only when its command exists on origin/main."""
    available = subprocess.run(
        ["git", "grep", "-q", "verify-lane", "origin/main", "--", "src/evallab",
         ":!src/evallab/program_report.py"],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    if available.returncode != 0:
        return {"availability": {"status": "unchecked", "reason": "verify-lane absent on origin/main"}}
    results: dict[str, Any] = {}
    for source in sorted(program.glob("*/RESULT.md")):
        command = ["uv", "run", "evallab", "verify-lane", str(source.parent),
                   "--output-dir", str(output / "verification" / source.parent.name)]
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            results[source.parent.name] = {"command": command, "outcome": "unchecked",
                                          "reason": "lane verifier timed out"}
            continue
        # verify-lane exits 0 when every check passes and 1 on discrepancies; any
        # other code (usage, I/O) is a verifier error, not a finding about the lane.
        outcome = {0: "verified", 1: "with discrepancies"}.get(result.returncode, "verifier errors")
        results[source.parent.name] = {"command": command, "returncode": result.returncode,
                                      "outcome": outcome, "stdout": result.stdout,
                                      "stderr": result.stderr}
    return results


def publish_bridge(output: Path, bridge: Path) -> None:
    """Commit only the new report directory; preserve every other writer's state."""
    bridge = bridge.expanduser().resolve()
    if bridge.exists():
        raise FileExistsError(f"Bridge destination exists: {bridge}")
    probe = bridge.parent
    while not probe.exists():
        probe = probe.parent
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=probe,
                            capture_output=True, text=True, check=True)
    repo = Path(result.stdout.strip()).resolve()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                            capture_output=True, text=True, check=True).stdout.strip()
    if repo.name != "research-context" or branch != "main":
        raise ValueError("--bridge requires the research-context repository on main")
    relative = bridge.relative_to(repo)
    shutil.copytree(output, bridge)
    subprocess.run(["git", "add", "--", str(relative)], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "report: publish receipt-bound program report",
                    "--", str(relative)], cwd=repo, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo, check=True)


def build_report(program: Path, output: Path, root: Path, *, select: list[str] | None = None,
                 citations: Path | None = None, index_only: bool = False) -> Path:
    facts = index_facts(program, root)
    # Editorial bindings are explicit and quote-guarded. Never infer a lane-wide receipt.
    if citations is not None:
        bindings = json.loads(citations.read_text())
        by_id = {fact.id: fact for fact in facts}
        for binding in bindings:
            fact = by_id[binding["id"]]
            if fact.quote != binding["quote"]:
                raise ValueError(f"Citation quote changed: {fact.id}")
            for value in binding["receipts"]:
                path = Path(value).expanduser().resolve()
                if not path.is_file() or path == Path(fact.file):
                    raise ValueError(f"Invalid receipt: {path}")
                fact.receipts.append(str(path))
            fact.receipts = sorted(set(fact.receipts))
    checks = {fact.id: _check(fact) for fact in facts}
    for fact in facts:
        fact.status = checks[fact.id]["status"]
    chosen = set(select) if select is not None else {fact.id for fact in facts}
    unknown = chosen - {fact.id for fact in facts}
    if unknown:
        raise ValueError(f"Unknown fact IDs: {sorted(unknown)}")
    selected = [fact for fact in facts if fact.id in chosen]
    if not selected:
        raise ValueError("Select at least one fact")
    output.mkdir(parents=True, exist_ok=False)
    (output / "facts.json").write_text(json.dumps([asdict(fact) for fact in facts], indent=2) + "\n")
    (output / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    verification = verify_lanes(program, root, output)
    (output / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    failures = [fact for fact in selected if fact.status != "receipt_bound_unchecked"]
    (output / "rejected.json").write_text(json.dumps([asdict(fact) for fact in failures], indent=2) + "\n")
    if index_only:
        return output
    if failures:
        details = "\n".join(f"{f.file}:{f.line} [{f.id}] {f.status}: {f.quote}" for f in failures)
        raise ValueError(f"Draft blocked; unsupported selected statements:\n{details}")
    report = render(selected, sorted({Path(f.file).parent.name for f in facts}), verification)
    (output / "REPORT.md").write_text(report)
    return output


def command(args: argparse.Namespace, root: Path, **_: object) -> int:
    output = build_report(args.program, args.output_dir, root, select=args.select,
                          citations=args.citations, index_only=args.index_only)
    if args.bridge:
        if args.index_only:
            raise ValueError("Cannot publish an index-only build")
        publish_bridge(output, args.bridge)
    print(output)
    return 0


def register_parser(commands: Any) -> None:
    parser = commands.add_parser("program", help="Build a receipt-bound report from lane RESULT files")
    parser.add_argument("program", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index-only", action="store_true", help="Index without drafting or publishing")
    parser.add_argument("--select", action="append", help="Editorial fact ID; repeat (default: all facts)")
    parser.add_argument("--citations", type=Path, help="JSON quote-guarded fact-to-receipt bindings")
    parser.add_argument("--bridge", type=Path, help="Publish a new directory in research-context on main")
    parser.set_defaults(func=command)
