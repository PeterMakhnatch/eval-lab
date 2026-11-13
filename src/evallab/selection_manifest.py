"""Observe checkpoint selection; never select or execute the CI test gate.

CI runs this driver by filename with ``python -I``. Only the base revision's
devloop implementation executes. Head Python files are data, not imports.
"""

from __future__ import annotations

# Import isolation must precede imports that a PR file could shadow.
# ruff: noqa: E402
import sys

if __name__ == "__main__" and not sys.flags.isolated:
    raise SystemExit("Selection observation requires python -I (isolated imports).")

import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

POLICIES = ("checkpoint", "checkpoint+environment")
POLICY_VERSION = 1


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def environment_rules(path: str) -> list[str]:
    """Rule 1 from the selection experiment; no fan-in or asset fallback."""
    name = PurePosixPath(path).name
    rules = []
    if name == "conftest.py":
        rules.append("environment:conftest")
    if (
        name in {
            "pyproject.toml", "Pipfile", "pytest.ini", "tox.ini",
            "setup.cfg", "setup.py", ".python-version",
        }
        or name.endswith((".lock", "-lock.json", "-lock.yaml", "-lock.yml"))
        or name.startswith(("requirements", "constraints"))
    ):
        rules.append("environment:dependencies-or-interpreter")
    if path.startswith((".github/workflows/", ".github/actions/")):
        rules.append("environment:workflow")
    if (
        name in {"Makefile", "sitecustomize.py", "usercustomize.py"}
        or name.endswith(".pth")
        or path.startswith("scripts/ci/")
        or path in {"scripts/premerge.sh", "src/evallab/devloop.py", "src/evallab/selection_manifest.py"}
        or name.startswith(("test_ci_coverage", "test_quality_gates", "test_test_hygiene"))
        or (
            path.startswith("scripts/")
            and any(word in name for word in ("pytest", "shard", "native", "coverage"))
        )
    ):
        rules.append("environment:test-execution-or-selector")
    return rules


def _regular_file(root: Path, revision: str, path: str) -> None:
    entry = git(root, "--literal-pathspecs", "ls-tree", "-z", revision, "--", path)
    if not entry.startswith(b"100"):
        raise ValueError(f"missing or non-regular selector input: {path}")


def _test_roots(root: Path, revision: str) -> list[str]:
    _regular_file(root, revision, "pyproject.toml")
    config = tomllib.loads(git(root, "show", f"{revision}:pyproject.toml").decode())
    roots = config["tool"]["pytest"]["ini_options"]["testpaths"]
    if (
        not isinstance(roots, list) or not roots
        or any(not isinstance(item, str) or not item or not PurePosixPath(item).parts for item in roots)
        or any(PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts for item in roots)
    ):
        raise ValueError("invalid testpaths inventory")
    return roots


def _snapshot(root: Path, revision: str, target: Path, allowed_roots: list[str]) -> list[str]:
    test_roots = _test_roots(root, revision)
    roots = [PurePosixPath(item) for item in test_roots]
    if any(not any(item.is_relative_to(allowed) for allowed in allowed_roots) for item in roots):
        raise ValueError("testpaths expands beyond the base-declared roots; observation unavailable")
    paths = ["pyproject.toml"]
    for record in git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode()
        candidate = PurePosixPath(path)
        if not path.endswith(".py"):
            continue
        if path.startswith("src/evallab/") or any(candidate.is_relative_to(item) for item in roots):
            if not metadata.startswith(b"100"):
                raise ValueError(f"non-regular selector input: {path}")
            paths.append(path)
    archive = git(root, "--literal-pathspecs", "archive", revision, "--", *paths)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(target, filter="data")
    return test_roots


def build_manifest(root: Path, head: str, base: str, tested: str | None = None) -> dict[str, Any]:
    head = git(root, "rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
    base = git(root, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    merge_base = git(root, "merge-base", base, head).decode().strip()
    tested = git(root, "rev-parse", "--verify", f"{tested or head}^{{commit}}").decode().strip()
    if tested != head:
        parents = git(root, "rev-list", "--parents", "-n", "1", tested).decode().split()[1:]
        if parents != [base, head]:
            raise ValueError("tested merge parents differ from the declared base and head")
    # --no-renames lists both the old deletion and the new addition, including
    # names containing whitespace. Never include working-tree or merge edits.
    changed = sorted(set(filter(None, git(
        root, "diff", "--name-only", "--no-renames", "-z", merge_base, head
    ).decode().split("\0"))))
    _regular_file(root, base, "src/evallab/devloop.py")
    source = git(root, "show", f"{base}:src/evallab/devloop.py")
    with tempfile.TemporaryDirectory(prefix="evallab-selection-") as temporary:
        temporary_path = Path(temporary)
        selector_path = temporary_path / "trusted_devloop.py"
        selector_path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("_evallab_trusted_devloop", selector_path)
        if spec is None or spec.loader is None:
            raise ValueError("cannot load the base selector")
        selector = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = selector
        try:
            spec.loader.exec_module(selector)
            snapshot = temporary_path / "head"
            snapshot.mkdir()
            test_roots = _snapshot(root, tested, snapshot, _test_roots(root, base))
            inventory = sorted(selector._test_consumers(snapshot, test_roots))
            if not inventory:
                raise ValueError("empty test module inventory")
            plan = selector.plan_devloop(changed, snapshot, checkpoint=True)
        finally:
            sys.modules.pop(spec.name, None)
    selected: dict[str, list[str]] = {}
    for node, reasons in plan.reasons.items():
        module = node.partition("::")[0]
        selected.setdefault(module, []).extend(reasons)
    matches = [
        {"path": path, "rule": rule}
        for path in changed for rule in environment_rules(path)
    ]
    policies = {}
    for name in POLICIES:
        fallback = name == "checkpoint+environment" and bool(matches)
        reasons = (
            {module: ["Full fallback: environment Rule 1"] for module in inventory}
            if fallback else selected
        )
        policies[name] = {
            "selected_modules": sorted(reasons),
            "reasons": {module: sorted(set(values)) for module, values in reasons.items()},
            "selectors": inventory if fallback else plan.modules,
            "fallback": fallback,
            "matched_rules": matches if fallback else [],
        }
    return {
        "schema_version": 1,
        "status": "ok",
        "policy_version": POLICY_VERSION,
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "tested_sha": tested,
        "head_sha": head,
        "base_sha": base,
        "merge_base_sha": merge_base,
        "selector_revision": base,
        "selector_sha256": hashlib.sha256(source).hexdigest(),
        "test_roots": test_roots,
        "inventory": inventory,
        "changed_paths": changed,
        "policies": policies,
        "limits": "Module inclusion only; node selections use whole-module costs. No gate changes.",
    }


def summary(manifest: dict[str, Any]) -> str:
    if manifest["status"] != "ok":
        return "\n".join([
            "::error::Selection observation failed (non-gating)",
            f"head={manifest.get('head_sha')} base={manifest.get('base_sha')}",
            f"error={' '.join(manifest['error'].splitlines())}",
            "No selection or cost result is available.",
            "The full required gate remains unchanged.",
        ])
    policies = manifest["policies"]
    return "\n".join([
        f"Selection observation: head={manifest['head_sha']} base={manifest['base_sha']}",
        f"selector={manifest['selector_revision']} merge-base={manifest['merge_base_sha']}",
        f"checkpoint: {len(policies['checkpoint']['selected_modules'])}/{len(manifest['inventory'])} modules",
        f"checkpoint+environment: {len(policies['checkpoint+environment']['selected_modules'])} modules; "
        f"fallback={policies['checkpoint+environment']['fallback']}",
        "Observation only. The full required gate remains unchanged.",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--tested", help="Exact tested merge commit; parents must be base then head")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build_manifest(args.root, args.head, args.base, args.tested)
    except Exception as error:
        detail = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail += ": " + error.stderr.decode(errors="replace").strip()
        manifest = {"schema_version": 1, "status": "error", "head_sha": args.head,
                    "base_sha": args.base, "error": f"{type(error).__name__}: {detail}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    message = summary(manifest)
    print(message)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a") as stream:
            stream.write(message + "\n")
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
