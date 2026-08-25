#!/usr/bin/env python3
"""Fail-closed source and credential gates for the lean Tau lane."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_AGENT = "evallab.harbor_codex:PinnedCodex"


@dataclass(frozen=True)
class Decision:
    phase: str
    proceed: bool
    reason_code: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": "proceed" if self.proceed else "blocked",
            "proceed": self.proceed,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "created_trial": False,
        }


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source(root: Path, manifest: Mapping[str, Any]) -> Path:
    """Require the exact tagged tau2 checkout; never clone or fall back."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"blocked:missing_source_checkout:{root}")
    required = manifest["required_upstream"]
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        tag = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"blocked:unreadable_source_checkout:{root}") from exc
    if commit != required["commit"]:
        raise RuntimeError(f"blocked:source_commit_mismatch:expected={required['commit']}:actual={commit}")
    if tag != required["release_tag"]:
        raise RuntimeError(f"blocked:source_tag_mismatch:expected={required['release_tag']}:actual={tag}")
    for row in manifest["tasks"]:
        path = root / "data/tau2/domains/banking_knowledge/tasks" / f"{row['task_id']}.json"
        if not path.is_file() or sha256(path) != row["task_sha256"]:
            raise RuntimeError(f"blocked:source_task_digest_mismatch:{row['task_id']}")
    for spec_name in ("tasks_file", "retrieval_prompt"):
        spec = required[spec_name]
        path = root / spec["path"]
        if not path.is_file() or sha256(path) != spec["sha256"]:
            raise RuntimeError(f"blocked:source_digest_mismatch:{spec['path']}")
    return root


def _auth_file_ok(home: Path) -> bool:
    path = home.expanduser() / ".codex" / "auth.json"
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def credential_preflight(
    phase: str, *, env: Mapping[str, str], home: Path, agent: str | None = None
) -> Decision:
    """Check presence/routing only; credential values never enter the decision."""
    if phase in {"reference", "luna"} and not (env.get("OPENAI_API_KEY") or "").strip():
        return Decision(
            phase, False, "blocked:missing_openai_api_key_for_simulated_user",
            "Harness credential block: tau3 simulated-user runtime requires OPENAI_API_KEY; this is not a model or verifier failure.",
        )
    if phase == "luna" and (agent or DEFAULT_AGENT).strip() in {DEFAULT_AGENT, "PinnedCodex", "codex"}:
        if not _auth_file_ok(home):
            return Decision(
                phase, False, "blocked:missing_codex_auth_json",
                "Harness credential block: PinnedCodex requires ~/.codex/auth.json with CODEX_FORCE_AUTH_JSON=1; this is not a model failure.",
            )
    return Decision(phase, True, detail="Credential preflight passed.")


def preflight_tau_phase(
    phase: str, *, env: Mapping[str, str], home: Path, source_root: Path | None = None,
    manifest: Mapping[str, Any] | None = None, agent: str | None = None,
) -> Decision:
    if source_root is not None and manifest is not None:
        try:
            validate_source(source_root, manifest)
        except RuntimeError as exc:
            return Decision(phase, False, ":".join(str(exc).split(":")[:2]), str(exc))
    return credential_preflight(phase, env=env, home=home, agent=agent)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["oracle", "reference", "luna"], default="oracle")
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=Path("library/benchmarks/tau-knowledge/cohort.manifest.json"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = args.source or (Path(os.environ["TAU2_BENCH_ROOT"]) if os.environ.get("TAU2_BENCH_ROOT") else None)
    decision = preflight_tau_phase(args.phase, env=os.environ, home=Path.home(), source_root=source, manifest=manifest if source else None)
    print(json.dumps(decision.to_dict(), indent=2))
    return 0 if decision.proceed else 2


if __name__ == "__main__":
    raise SystemExit(main())
