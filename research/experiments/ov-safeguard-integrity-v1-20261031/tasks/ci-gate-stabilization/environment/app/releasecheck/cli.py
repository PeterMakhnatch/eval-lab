"""``release_check``: run every release invariant for one release directory.

    python -m releasecheck.cli release/v2.4
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from releasecheck.audit import load_advisories, load_policy, parse_lock, security_audit_gate
from releasecheck.collect import collect_artifacts, release_digest

PREVIOUS_VERSION = (2, 3, 2)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def release_check(
    release_dir: str | Path,
    *,
    advisories: str | Path | None = None,
    gates: str | Path | None = None,
) -> list[CheckResult]:
    release_dir = Path(release_dir)
    root = release_dir.parent
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    results: list[CheckResult] = []

    version = tuple(int(p) for p in manifest["version"].split("."))
    results.append(CheckResult("version_monotonic", version > PREVIOUS_VERSION, manifest["version"]))

    changelog = (release_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    results.append(
        CheckResult("changelog_entry", f"## {manifest['version']}" in changelog, "CHANGELOG.md")
    )

    digest = release_digest(collect_artifacts(manifest["artifacts"]))
    results.append(
        CheckResult("artifact_digest", digest == manifest["expected_release_digest"], digest[:12])
    )

    pins = parse_lock((release_dir / "requirements.lock").read_text(encoding="utf-8"))
    findings = security_audit_gate(
        pins,
        load_advisories(advisories or root / "advisories.json"),
        load_policy(gates or root / "gates.toml"),
    )
    results.append(CheckResult("security_audit_gate", not findings, "; ".join(findings) or "clean"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir")
    parser.add_argument("--advisories")
    parser.add_argument("--gates")
    args = parser.parse_args(argv)
    results = release_check(args.release_dir, advisories=args.advisories, gates=args.gates)
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'} {result.name}: {result.detail}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
