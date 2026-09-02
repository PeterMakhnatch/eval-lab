#!/usr/bin/env python3
"""Fail-closed source and credential gates for the lean Tau lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SIMULATOR_PROVIDER = "openai"
DEFAULT_SIMULATOR_MODEL = "gpt-4o-mini-2024-07-18"
DEFAULT_SIMULATOR_CREDENTIAL_ENV = "TAU3_SIMULATOR_API_KEY"
DEFAULT_SIMULATOR_BASE_URL_ENV = "TAU3_SIMULATOR_BASE_URL"
ALLOWED_SIMULATOR_HOSTS = frozenset({"api.openai.com"})


@dataclass(frozen=True)
class Decision:
    phase: str
    proceed: bool
    reason_code: str | None = None
    detail: str = ""
    simulator_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase": self.phase,
            "status": "proceed" if self.proceed else "blocked",
            "proceed": self.proceed,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "created_trial": False,
        }
        if self.simulator_metadata is not None:
            # Metadata must be log-safe: only non-sensitive provider/model/env-name descriptors
            result["simulator"] = self.simulator_metadata
        return result


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
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tag = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"blocked:unreadable_source_checkout:{root}") from exc
    if commit != required["commit"]:
        raise RuntimeError(
            f"blocked:source_commit_mismatch:expected={required['commit']}:actual={commit}"
        )
    if tag != required["release_tag"]:
        raise RuntimeError(
            f"blocked:source_tag_mismatch:expected={required['release_tag']}:actual={tag}"
        )
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


def credential_preflight(
    phase: str,
    *,
    env: Mapping[str, str],
    simulator_provider: str = DEFAULT_SIMULATOR_PROVIDER,
    simulator_model: str = DEFAULT_SIMULATOR_MODEL,
    simulator_credential_env: str = DEFAULT_SIMULATOR_CREDENTIAL_ENV,
    simulator_base_url_env: str = DEFAULT_SIMULATOR_BASE_URL_ENV,
) -> Decision:
    """Require a distinct, explicit cloud route for the simulated-user runtime."""
    base_url = (env.get(simulator_base_url_env) or "").strip()
    sim_meta = {
        "provider": simulator_provider,
        "model": simulator_model,
        "credential_env": simulator_credential_env,
        "base_url_env": simulator_base_url_env,
        "base_url": base_url or None,
    }
    if phase in {"reference", "evaluation"}:
        key_val = (env.get(simulator_credential_env) or "").strip()
        if not key_val:
            return Decision(
                phase,
                False,
                f"blocked:missing_{simulator_credential_env.lower()}_for_simulated_user",
                f"Harness credential block: tau3 simulated-user runtime ({simulator_provider}/{simulator_model}) "
                f"requires {simulator_credential_env}; this is not a model or verifier failure.",
                simulator_metadata=sim_meta,
            )
        if not base_url:
            return Decision(
                phase,
                False,
                f"blocked:missing_{simulator_base_url_env.lower()}_for_simulated_user",
                f"Harness route block: tau3 simulated-user runtime requires explicit {simulator_base_url_env}.",
                simulator_metadata=sim_meta,
            )
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SIMULATOR_HOSTS:
            return Decision(
                phase,
                False,
                "blocked:unregistered_simulated_user_route",
                f"Harness route block: {base_url!r} is not the registered {simulator_provider} cloud route.",
                simulator_metadata=sim_meta,
            )
    return Decision(
        phase,
        True,
        detail="Credential and cloud-route preflight passed.",
        simulator_metadata=sim_meta,
    )


def preflight_tau_phase(
    phase: str,
    *,
    env: Mapping[str, str],
    source_root: Path | None = None,
    manifest: Mapping[str, Any] | None = None,
    simulator_provider: str = DEFAULT_SIMULATOR_PROVIDER,
    simulator_model: str = DEFAULT_SIMULATOR_MODEL,
    simulator_credential_env: str = DEFAULT_SIMULATOR_CREDENTIAL_ENV,
    simulator_base_url_env: str = DEFAULT_SIMULATOR_BASE_URL_ENV,
) -> Decision:
    if source_root is not None and manifest is not None:
        try:
            validate_source(source_root, manifest)
        except RuntimeError as exc:
            return Decision(
                phase,
                False,
                ":".join(str(exc).split(":")[:2]),
                str(exc),
                simulator_metadata={
                    "provider": simulator_provider,
                    "model": simulator_model,
                    "credential_env": simulator_credential_env,
                    "base_url_env": simulator_base_url_env,
                },
            )
    return credential_preflight(
        phase,
        env=env,
        simulator_provider=simulator_provider,
        simulator_model=simulator_model,
        simulator_credential_env=simulator_credential_env,
        simulator_base_url_env=simulator_base_url_env,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["oracle", "reference", "evaluation"], default="oracle")
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("library/benchmarks/tau-knowledge/cohort.manifest.json"),
    )
    parser.add_argument("--simulator-provider", default=DEFAULT_SIMULATOR_PROVIDER)
    parser.add_argument("--simulator-model", default=DEFAULT_SIMULATOR_MODEL)
    parser.add_argument("--simulator-credential-env", default=DEFAULT_SIMULATOR_CREDENTIAL_ENV)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = args.source or (
        Path(os.environ["TAU2_BENCH_ROOT"]) if os.environ.get("TAU2_BENCH_ROOT") else None
    )
    decision = preflight_tau_phase(
        args.phase,
        env=os.environ,
        source_root=source,
        manifest=manifest if source else None,
        simulator_provider=args.simulator_provider,
        simulator_model=args.simulator_model,
        simulator_credential_env=args.simulator_credential_env,
    )
    print(json.dumps(decision.to_dict(), indent=2))
    return 0 if decision.proceed else 2


if __name__ == "__main__":
    raise SystemExit(main())
