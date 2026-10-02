"""Setup fingerprints and reference-profile gates for MiMo runs (HAR-149).

On 2026-10-01 every MiMo trial ran with the sandbox network open and nobody
noticed: nothing compared a run's setup with the intended setup. This module
makes that comparison a software check.

A setup fingerprint records, per trial and per batch:

- harness id and version;
- server: model revision, SGLang image digest, tool-call parser, reasoning
  parser, context length;
- the sampling actually sent: temperature, top_p, top_k;
- the lock mode as applied, from ``egress-lock.json``;
- task bytes and digest, and the ledger status at launch;
- step and token budgets.

A reference profile per measured setup lives in
``research/setup-profiles/`` (see its README). Dispatch refuses a MiMo batch
whose fingerprint differs from the named reference unless the deviation is
explicitly listed in the spec. ``evallab preflight --spec`` runs the same
comparison before anything is approved, at $0, without Daytona or Modal.
"""

from __future__ import annotations

import ast
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

FINGERPRINT_SCHEMA = "evallab.setup_fingerprint/v1"
FINGERPRINT_FILENAME = "setup-fingerprint.json"
SETUP_PROFILES_DIRNAME = "research/setup-profiles"
SERVE_CONFIG_RELATIVE = "tools/modal-mimo-serve/serve.py"
PARSER_SOURCE_RELATIVE = "src/evallab/mimo_tool_calls.py"
TASK_LEDGER_RELATIVE = "research/experiments/python-task-ledger/ledger.csv"
TASK_VARIANTS_RELATIVE = "library/task-variants"

#: Fingerprint fields compared against the reference. Deployment identity
#: (server.model_revision, server.sglang_image) is recorded but never
#: compared: the reference pins behaviour, not which container serves it.
#: lock.mode and the task ledger binding are hard gates, never deviations.
COMPARED_FIELDS = (
    "harness.id",
    "server.tool_call_parser",
    "server.reasoning_parser",
    "server.context_length",
    "sampling.temperature",
    "sampling.top_p",
    "sampling.top_k",
    "lock.mode",
    "budgets.step_limit",
)

#: Compared fields a spec deviation may cover. The lock and the ledger
#: binding cannot be waived by declaration.
DEVIATION_ELIGIBLE_FIELDS = frozenset(name for name in COMPARED_FIELDS if name != "lock.mode")


def package_repo_root() -> Path:
    """The checkout running this code (``src/evallab`` two levels up)."""
    return Path(__file__).resolve().parents[2]


def discover_repo_root(start: Path) -> Path | None:
    """Walk up from *start* to the checkout holding the fingerprint assets."""
    current = start.resolve() if start.is_file() else start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / SERVE_CONFIG_RELATIVE).is_file() and (
            candidate / SETUP_PROFILES_DIRNAME
        ).is_dir():
            return candidate
    return None


def resolve_repo_root(explicit: Path | None, start: Path) -> Path:
    """Explicit root, else discovery from *start*, else the running checkout."""
    if explicit is not None:
        return explicit.resolve()
    discovered = discover_repo_root(start)
    return discovered if discovered is not None else package_repo_root()


def read_serve_config(repo_root: Path) -> dict[str, Any]:
    """Read server settings from the repo's serve config without importing it.

    ``serve.py`` imports Modal, so it is parsed as an AST: $0, no server or
    credentials needed. Missing constants read as ``None`` with the reason.
    """
    path = repo_root / SERVE_CONFIG_RELATIVE
    fields: dict[str, Any] = {
        "model_id": None,
        "model_revision": None,
        "sglang_image": None,
        "context_length": None,
        "reasoning_parser": None,
    }
    if not path.is_file():
        return {"path": path.as_posix(), "missing": "serve config not found", **fields}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return {"path": path.as_posix(), "missing": f"serve config unreadable: {exc}", **fields}
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    constants[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                    continue
    # SGLANG_IMAGE pins a manifest-list digest (``repo@sha256:...``).
    image = constants.get("SGLANG_IMAGE")
    if isinstance(image, str):
        # A parenthesized implicit-concat assignment is an Expr, not Assign,
        # so fall back to a textual read for the digest line.
        fields["sglang_image"] = image
    if fields["sglang_image"] is None:
        digest = _sglang_digest_text(path)
        if digest is not None:
            fields["sglang_image"] = digest
    model_id = constants.get("MODEL_ID")
    if isinstance(model_id, str):
        fields["model_id"] = model_id
    revision = constants.get("MODEL_REVISION")
    if isinstance(revision, str):
        fields["model_revision"] = revision
    context = constants.get("CONTEXT_LENGTH")
    if isinstance(context, int) and not isinstance(context, bool):
        fields["context_length"] = context
    parser = _sglang_flag_text(path, "--reasoning-parser")
    if parser is not None:
        fields["reasoning_parser"] = parser
    fields["path"] = path.as_posix()
    return fields


def _sglang_digest_text(path: Path) -> str | None:
    """The ``lmsysorg/sglang@sha256:...`` pin from the raw config text."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip().strip("\"'")
        if stripped.startswith("lmsysorg/sglang@sha256:"):
            return stripped.rstrip('",')
    return None


def _sglang_flag_text(path: Path, flag: str) -> str | None:
    """The value following an SGLang launch flag in ``sglang_command``."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for index, line in enumerate(lines):
        if f'"{flag}"' in line and index + 1 < len(lines):
            value = lines[index + 1].strip().strip('",')
            return value or None
    return None


def tool_call_parser_name(repo_root: Path) -> tuple[str, str]:
    """(parser name, source) for the MiMo route normalizer.

    ``none`` when the normalizer is absent: without it nothing rewrites the
    native ``<tool_call><function=...>`` markup into Terminus JSON, so a
    reference that requires a parser refuses.
    """
    source = (repo_root / PARSER_SOURCE_RELATIVE).as_posix()
    if (repo_root / PARSER_SOURCE_RELATIVE).is_file():
        return "mimo-native", source
    return "none", f"{source} (absent)"


def sampling_sent(model: str | None) -> dict[str, Any]:
    """The sampling actually sent on the MiMo route.

    The proxy enforces the generation config on every call
    (``containers/zai_openapi_secret_proxy.py`` forces temperature/top_p/top_k
    and strips reasoning_effort), so the harness-requested values do not
    decide. Values mirror ``MIMO_SELFHOSTED_*`` in execution contracts.
    """
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_TEMPERATURE,
        MIMO_SELFHOSTED_TOP_K,
        MIMO_SELFHOSTED_TOP_P,
        is_mimo_selfhosted_model,
    )

    if is_mimo_selfhosted_model(model):
        return {
            "temperature": float(MIMO_SELFHOSTED_TEMPERATURE),
            "top_p": float(MIMO_SELFHOSTED_TOP_P),
            "top_k": int(MIMO_SELFHOSTED_TOP_K),
            "source": (
                "proxy-enforced generation_config "
                "(execution_contracts MIMO_SELFHOSTED_TEMPERATURE/TOP_P/TOP_K, "
                "mirrored in containers/zai_openapi_secret_proxy.py)"
            ),
        }
    return {
        "temperature": None,
        "top_p": None,
        "top_k": None,
        "source": "unsent: non-selfhosted MiMo route pins no sampling here",
    }


def lock_posture(declared: bool | None, effective: bool, *, source: str) -> dict[str, Any]:
    """Declared setup posture plus the resolved intent.

    Only an explicit ``egress_lock: true`` counts as ``locked``: the
    2026-10-01 run was open because the lock was implicit, never pinned.
    The resolved intent (code default) is reported alongside so preflight
    shows what the trial fingerprint will later observe.
    """
    return {
        "mode": "locked" if declared is True else "open",
        "declared": declared,
        "effective": "locked" if effective else "open",
        "source": source,
    }


def task_bytes_and_digest(task_dir: Path) -> tuple[int, str]:
    """(total bytes, package digest) of the task directory.

    The digest reuses ``registry.compute_task_digests`` — the same package
    digest the runner stages — so there is exactly one task identity.
    """
    from evallab.registry import compute_task_digests

    total = sum(
        candidate.stat().st_size for candidate in task_dir.rglob("*") if candidate.is_file()
    )
    return total, compute_task_digests(task_dir).package


def ledger_binding(
    repo_root: Path, task_id: str | None, package_digest: str | None
) -> dict[str, Any]:
    """Ledger status at launch, via the canonical task index."""
    from evallab.counts import task_index_for

    if not task_id:
        return {"status": None, "matched": False, "reason": "no task id"}
    try:
        status = task_index_for(repo_root).status_for(task_id, package_digest)
    except Exception as exc:  # noqa: BLE001 — unreadable ledger refuses closed
        return {
            "status": None,
            "matched": False,
            "reason": f"task ledger unreadable: {type(exc).__name__}: {exc}",
        }
    return {
        "status": status.get("status"),
        "ledger_status": status.get("ledger_status"),
        "digest_match": status.get("digest_match"),
        "matched": bool(status.get("digest_match")),
        "reason": status.get("reason"),
        "source": f"{TASK_LEDGER_RELATIVE}#{task_id}",
    }


def build_intended_fingerprint(
    *,
    spec: Any,
    task_dir: Path,
    model: str | None,
    agent: str,
    environment: str = "docker",
    repo_root: Path,
) -> dict[str, Any]:
    """The setup a dispatch is about to run, from the real $0 path."""
    from evallab.execution_contracts import resolve_egress_lock

    serve = read_serve_config(repo_root)
    parser, parser_source = tool_call_parser_name(repo_root)
    sampling = sampling_sent(model)
    task_id = spec.task_id or task_dir.name if spec is not None else task_dir.name
    try:
        task_bytes, task_digest = task_bytes_and_digest(task_dir)
        task_error: str | None = None
    except (OSError, ValueError) as exc:
        task_bytes, task_digest = 0, ""
        task_error = f"{type(exc).__name__}: {exc}"
    ledger = ledger_binding(repo_root, task_id, task_digest or None)
    declared = spec.egress_lock if spec is not None else None
    effective = bool(
        resolve_egress_lock(_egress_request(task_dir, agent, model, declared, environment))
    )
    deviations = [
        {"field": item.field, "value": item.value, "reason": item.reason}
        if hasattr(item, "field")
        else dict(item)
        for item in ((spec.deviations or ()) if spec is not None else ())
    ]
    return {
        "schema": FINGERPRINT_SCHEMA,
        "subject": {
            "spec": spec.name if spec is not None else None,
            "task": task_id,
            "model": model,
        },
        "reference_profile": spec.reference_profile if spec is not None else None,
        "harness": {
            "id": agent,
            "version": spec.harness_tree_sha256 if spec is not None else None,
            "version_source": (
                "spec harness_tree_sha256"
                if spec is not None and spec.harness_tree_sha256
                else "stock harbor agent (no harness tree pinned)"
            ),
        },
        "server": {
            "model_revision": serve.get("model_revision"),
            "sglang_image": serve.get("sglang_image"),
            "tool_call_parser": parser,
            "reasoning_parser": serve.get("reasoning_parser"),
            "context_length": serve.get("context_length"),
            "sources": {
                "model_revision": f"{SERVE_CONFIG_RELATIVE} MODEL_REVISION",
                "sglang_image": f"{SERVE_CONFIG_RELATIVE} SGLANG_IMAGE",
                "tool_call_parser": parser_source,
                "reasoning_parser": f"{SERVE_CONFIG_RELATIVE} sglang_command --reasoning-parser",
                "context_length": f"{SERVE_CONFIG_RELATIVE} CONTEXT_LENGTH",
            },
        },
        "sampling": sampling,
        "lock": lock_posture(
            declared,
            effective,
            source=(
                "spec egress_lock (declared); execution_contracts "
                "resolve_egress_lock (effective intent)"
            ),
        ),
        "task": {
            "bytes": task_bytes,
            "digest": task_digest,
            "declared_digest": spec.task_package_digest if spec is not None else None,
            "ledger_status": ledger.get("status"),
            "ledger_match": ledger.get("matched"),
            "ledger_reason": ledger.get("reason"),
            "error": task_error,
            "source": "registry.compute_task_digests(task).package; ledger via counts.task_index_for",
        },
        "budgets": {
            "timeout_seconds": spec.timeout_seconds if spec is not None else None,
            "max_requests": spec.max_requests if spec is not None else None,
            "max_input_tokens": spec.max_input_tokens if spec is not None else None,
            "max_output_tokens": spec.max_output_tokens if spec is not None else None,
            "max_total_tokens": spec.max_total_tokens if spec is not None else None,
            "cost_limit_usd": spec.cost_limit_usd if spec is not None else None,
            "step_limit": None,
            "step_limit_source": "terminus harness step cap is not pinned in the spec",
        },
        "deviations": deviations,
    }


def _egress_request(
    task_dir: Path, agent: str, model: str | None, declared: bool | None, environment: str
) -> Any:
    """Minimal request shape for the egress-lock resolution."""
    from evallab.execution_contracts import RunRequest

    return RunRequest(
        task=task_dir,
        agent=agent,
        name="setup-fingerprint-probe",
        jobs_dir=task_dir,
        environment=environment,
        model=model,
        egress_lock=declared,
    )


def trial_fingerprint(intended: dict[str, Any], trial_dir: Path) -> dict[str, Any]:
    """The observed setup of one landed trial: lock as applied.

    Reads ``egress-lock.json`` written by ``harbor_daytona``: an absent file
    reads ``open`` (the pre-HAR-140 default was off and no run used the
    opt-in), matching ``trial_treatment``.
    """
    observed = dict(intended)
    lock = dict(intended.get("lock") or {})
    record_path = trial_dir / "egress-lock.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        record = None
    applied = bool(record.get("applied")) if isinstance(record, dict) else False
    lock["mode"] = "locked" if applied else "open"
    lock["observed_applied"] = applied
    lock["observed_source"] = (
        "trial egress-lock.json applied"
        if record is not None
        else "trial egress-lock.json absent (pre-HAR-140 default unlocked)"
    )
    observed["lock"] = lock
    observed["trial"] = trial_dir.name
    return observed


def load_reference_profile(repo_root: Path, name: str) -> dict[str, Any]:
    """Load one checked-in reference profile by name."""
    path = repo_root / SETUP_PROFILES_DIRNAME / f"{name}.yaml"
    if not path.is_file():
        known = sorted(
            candidate.stem
            for candidate in (repo_root / SETUP_PROFILES_DIRNAME).glob("*.yaml")
            if candidate.name != "README.md"
        )
        raise ValueError(
            f"unknown reference_profile {name!r} (expected one of {known}; "
            f"see {SETUP_PROFILES_DIRNAME}/README.md)"
        )
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"reference profile {name!r} unreadable: {exc}") from exc
    if not isinstance(profile, dict) or profile.get("name") != name:
        raise ValueError(f"reference profile {path} names a different setup")
    return profile


def default_profile_name(repo_root: Path) -> str | None:
    """The single checked-in profile, or None when the choice is ambiguous."""
    profiles_dir = repo_root / SETUP_PROFILES_DIRNAME
    if not profiles_dir.is_dir():
        return None
    names = sorted(
        candidate.stem for candidate in profiles_dir.glob("*.yaml") if candidate.is_file()
    )
    return names[0] if len(names) == 1 else None


def _reference_value(profile: dict[str, Any], field: str) -> tuple[Any, bool, str]:
    """(value, sourced, source-or-reason) for one compared field."""
    section, _, key = field.partition(".")
    node = profile.get(section)
    if not isinstance(node, dict):
        return None, False, f"profile has no {section!r} section"
    entry = node.get(key)
    if not isinstance(entry, dict):
        if section == "harness" and key == "id":
            return entry, True, "profile harness.id"
        return None, False, f"profile has no {field!r} entry"
    if entry.get("unsourced"):
        return None, False, str(entry.get("reason") or "unsourced in profile")
    return entry.get("value"), True, str(entry.get("source") or "profile")


def _fingerprint_value(fingerprint: dict[str, Any], field: str) -> Any:
    section, _, key = field.partition(".")
    node = fingerprint.get(section)
    return node.get(key) if isinstance(node, dict) else None


def compare_fingerprint(
    fingerprint: dict[str, Any], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every compared field where the setup differs from the reference.

    Unsourced reference fields and unknown setup values never diff: nothing
    can differ from an unknown value.
    """
    diffs: list[dict[str, Any]] = []
    for field in COMPARED_FIELDS:
        expected, sourced, source = _reference_value(profile, field)
        if not sourced:
            continue
        actual = _fingerprint_value(fingerprint, field)
        if actual is None:
            continue
        if actual != expected:
            diffs.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                    "source": source,
                }
            )
    return diffs


def check_deviations(
    diffs: list[dict[str, Any]], deviations: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """(uncovered diffs, stale-deviation reasons) for the listed deviations."""
    uncovered: list[dict[str, Any]] = []
    stale: list[str] = []
    by_field: dict[str, list[dict[str, Any]]] = {}
    for item in deviations:
        by_field.setdefault(str(item.get("field")), []).append(item)
    for diff in diffs:
        covering = [
            item
            for item in by_field.get(diff["field"], [])
            if item.get("value") == diff["actual"] and str(item.get("reason") or "").strip()
        ]
        if not covering:
            uncovered.append(diff)
    for field, items in by_field.items():
        if field not in COMPARED_FIELDS:
            stale.append(f"deviation for {field!r}: not a compared setup field")
            continue
        if field not in DEVIATION_ELIGIBLE_FIELDS:
            stale.append(f"deviation for {field!r}: this gate cannot be waived by declaration")
            continue
        for item in items:
            if not str(item.get("reason") or "").strip():
                stale.append(f"deviation for {field!r}: reason is empty")
    return uncovered, stale


def registered_variant(repo_root: Path, task_id: str | None, digest: str | None) -> str | None:
    """Record path of a ``library/task-variants`` variant of ``task_id`` with this digest."""
    if not task_id or not digest:
        return None
    for path in sorted((repo_root / TASK_VARIANTS_RELATIVE).glob("*/*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            record.get("variant_digest") == digest
            and str(record.get("task_name") or "").split("/", 1)[-1] == task_id
        ):
            return str(path.relative_to(repo_root))
    return None


def _validate_modelfree_setup(fingerprint: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Gates for model-free (nop/oracle) MiMo runs: lock resolution and ledger.

    No reference profile, no harness/server/sampling comparison: there is no
    model setup to compare. The lock passes on the effective resolution (the
    default locks MiMo tasks on Daytona; an explicit opt-out is already
    refused by the egress-lock validation), so census specs pass unchanged.
    A registered task variant of the named task (``library/task-variants``,
    any status) also passes: its nop is how a repair gets validated before
    the ledger can run it.
    """
    reasons: list[str] = []
    lock = fingerprint["lock"]
    if lock["effective"] != "locked":
        reasons.append(
            f"lock: effective resolution is {lock['effective']} "
            f"(declared={lock['declared']!r}); model-free MiMo runs require "
            "the sandbox locked"
        )
    task = fingerprint["task"]
    if task.get("error"):
        reasons.append(f"task: unreadable: {task['error']}")
    elif not task.get("ledger_match") and not registered_variant(
        repo_root, fingerprint["subject"]["task"], task.get("digest")
    ):
        reasons.append(
            f"task {fingerprint['subject']['task']}: outside the ledger "
            f"(status={task.get('ledger_status')}; {task.get('ledger_reason')})"
        )
    if reasons:
        raise ValueError("MiMo model-free setup refused: " + "; ".join(reasons))
    return fingerprint


def validate_mimo_setup(request: Any, repo_root: Path) -> dict[str, Any]:
    """Refuse a MiMo batch whose setup differs from its named reference.

    Lives on the real dispatch validate path: ``validate_request`` calls it
    for every spec-driven MiMo run, before anything is approved or spent.
    Returns the intended fingerprint for the caller to persist.

    Model-free agents (nop/oracle) have no model, sampling, server or harness
    setup, so they skip the reference requirement and those comparisons. They
    keep the gates that matter: the effective egress-lock resolution and the
    ledger binding.
    """
    from evallab.execution_contracts import CONTROL_AGENTS, is_mimo_run

    spec = request.experiment_spec
    if spec is None or not is_mimo_run(request.task, request.model):
        raise ValueError("validate_mimo_setup requires a spec-driven MiMo request")
    task_dir = Path(request.task)
    fingerprint = build_intended_fingerprint(
        spec=spec,
        task_dir=task_dir,
        model=request.model,
        agent=request.agent,
        environment=request.environment,
        repo_root=repo_root,
    )
    if request.agent in CONTROL_AGENTS:
        return _validate_modelfree_setup(fingerprint, repo_root)
    name = spec.reference_profile
    if not name:
        raise ValueError(
            "MiMo run refuses without a named reference_profile: set "
            "reference_profile (e.g. 'xiaomi-mimo-rl') plus deviations "
            "[{field, value, reason}] for every intended difference "
            f"(HAR-149; setup is {FINGERPRINT_SCHEMA})"
        )
    profile = load_reference_profile(repo_root, name)
    reasons: list[str] = []
    lock = fingerprint["lock"]
    if lock["mode"] != "locked":
        reasons.append(
            "lock.mode: reference requires locked; spec leaves egress_lock "
            f"{lock['declared']!r} (resolves {lock['effective']} by code default, "
            "not pinned). Set egress_lock: true explicitly: implicit defaults "
            "caused the 2026-10-01 open-network run."
        )
    task = fingerprint["task"]
    if task.get("error"):
        reasons.append(f"task: unreadable: {task['error']}")
    elif not task.get("ledger_match"):
        reasons.append(
            f"task {fingerprint['subject']['task']}: outside the ledger "
            f"(status={task.get('ledger_status')}; {task.get('ledger_reason')})"
        )
    if fingerprint["server"]["tool_call_parser"] == "none":
        reasons.append(
            "server.tool_call_parser: reference requires a parser for the "
            f"<tool_call><function=...> format; {PARSER_SOURCE_RELATIVE} is absent"
        )
    diffs = compare_fingerprint(fingerprint, profile)
    deviations = fingerprint["deviations"]
    uncovered, stale = check_deviations(diffs, deviations)
    for diff in uncovered:
        if diff["field"] == "lock.mode":
            continue
        reasons.append(
            f"{diff['field']}: reference {diff['expected']!r} vs setup "
            f"{diff['actual']!r} (source: {diff['source']}); list it under "
            "deviations [{field, value, reason}] to declare the difference"
        )
    reasons.extend(stale)
    if reasons:
        raise ValueError(f"MiMo setup fingerprint differs from {name!r}: " + "; ".join(reasons))
    return fingerprint


def _render_modelfree_preflight(lines: list[str], fingerprint: dict[str, Any]) -> tuple[str, bool]:
    """Preflight text for a model-free MiMo spec: lock resolution and ledger only."""
    lines = [
        *lines,
        "",
        "model-free run (nop/oracle): no reference profile, no harness/server/"
        "sampling comparison; gates are the lock resolution and the ledger binding.",
    ]
    problems: list[str] = []
    if fingerprint["lock"]["effective"] != "locked":
        problems.append(
            f"lock: effective resolution is {fingerprint['lock']['effective']} "
            f"(declared={fingerprint['lock']['declared']!r})"
        )
    if not fingerprint["task"]["ledger_match"]:
        problems.append(
            f"task: outside the ledger (status={fingerprint['task']['ledger_status']}; "
            f"{fingerprint['task']['ledger_reason']})"
        )
    if problems:
        lines += ["", "REFUSED:", *(f"  - {problem}" for problem in problems)]
        return ("\n".join(lines) + "\n", False)
    lines += ["", "OK: model-free setup passes (lock resolves locked; ledger matches)."]
    return ("\n".join(lines) + "\n", True)


def render_spec_preflight(spec_path: Path, repo_root: Path) -> tuple[str, bool]:
    """Dry-run one spec file against its reference at $0. Returns (text, ok)."""
    from evallab.execution_contracts import CONTROL_AGENTS, is_mimo_run
    from evallab.schemas import ExperimentSpec

    try:
        spec = ExperimentSpec.model_validate(json.loads(spec_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return (f"spec {spec_path}: unreadable: {exc}\n", False)
    task_rel = spec.task_path or spec.task
    task_dir = repo_root / task_rel
    fingerprint = build_intended_fingerprint(
        spec=spec,
        task_dir=task_dir,
        model=spec.model,
        agent=spec.agent,
        environment=spec.environment,
        repo_root=repo_root,
    )
    lines = [
        f"spec: {spec.name} ({spec_path})",
        f"reference: {spec.reference_profile or 'none named'}",
        f"model: {spec.model}; task: {task_rel}",
        "",
        "fingerprint:",
        f"  harness: {fingerprint['harness']['id']} (version={fingerprint['harness']['version']})",
        f"  server: revision={fingerprint['server']['model_revision']} "
        f"image={_short(fingerprint['server']['sglang_image'])} "
        f"parser={fingerprint['server']['tool_call_parser']} "
        f"reasoning={fingerprint['server']['reasoning_parser']} "
        f"context={fingerprint['server']['context_length']}",
        f"  sampling: temperature={fingerprint['sampling']['temperature']} "
        f"top_p={fingerprint['sampling']['top_p']} "
        f"top_k={fingerprint['sampling']['top_k']}",
        f"  lock: mode={fingerprint['lock']['mode']} "
        f"(declared={fingerprint['lock']['declared']}, "
        f"resolved intent={fingerprint['lock']['effective']})",
        f"  task: bytes={fingerprint['task']['bytes']} "
        f"digest={_short(fingerprint['task']['digest'])} "
        f"ledger={fingerprint['task']['ledger_status']} "
        f"(match={fingerprint['task']['ledger_match']})",
        f"  budgets: timeout={fingerprint['budgets']['timeout_seconds']} "
        f"requests={fingerprint['budgets']['max_requests']} "
        f"tokens={fingerprint['budgets']['max_input_tokens']}/"
        f"{fingerprint['budgets']['max_output_tokens']}/"
        f"{fingerprint['budgets']['max_total_tokens']} "
        f"cost={fingerprint['budgets']['cost_limit_usd']}",
    ]
    if not is_mimo_run(task_rel, spec.model):
        lines += ["", "not a MiMo run: reference gate does not apply."]
        return ("\n".join(lines) + "\n", True)
    if spec.agent in CONTROL_AGENTS:
        return _render_modelfree_preflight(lines, fingerprint)
    profile_name = spec.reference_profile
    if profile_name is None:
        profile_name = default_profile_name(repo_root) or default_profile_name(package_repo_root())
    if not spec.reference_profile:
        lines.append("")
        if profile_name is not None:
            lines.append(
                f"no reference named: showing differences from {profile_name!r} "
                "(the only checked-in profile); dispatch still refuses until "
                "the spec names it explicitly."
            )
        else:
            lines += [
                "",
                "REFUSED: MiMo run names no reference_profile and no single "
                "default profile exists: set reference_profile plus deviations "
                "[{field, value, reason}] for every intended difference.",
            ]
            return ("\n".join(lines) + "\n", False)
    assert profile_name is not None
    profile_path = repo_root / SETUP_PROFILES_DIRNAME / f"{profile_name}.yaml"
    profile_root = repo_root if profile_path.is_file() else package_repo_root()
    try:
        profile = load_reference_profile(profile_root, profile_name)
    except ValueError as exc:
        return ("\n".join(lines) + f"\n\nREFUSED: {exc}\n", False)
    diffs = compare_fingerprint(fingerprint, profile)
    uncovered, stale = check_deviations(diffs, fingerprint["deviations"])
    hard: list[str] = []
    if fingerprint["lock"]["mode"] != "locked":
        hard.append(
            f"lock.mode: reference locked vs setup open "
            f"(declared={fingerprint['lock']['declared']}, "
            f"resolved intent={fingerprint['lock']['effective']})"
        )
    if not fingerprint["task"]["ledger_match"]:
        hard.append(
            f"task: outside the ledger (status="
            f"{fingerprint['task']['ledger_status']}; "
            f"{fingerprint['task']['ledger_reason']})"
        )
    if fingerprint["server"]["tool_call_parser"] == "none":
        hard.append("server.tool_call_parser: missing (reference requires a parser)")
    lines.append("")
    if diffs:
        lines.append("differences from reference:")
        for diff in diffs:
            covered = diff not in uncovered
            lines.append(
                f"  - {diff['field']}: reference {diff['expected']!r} vs setup "
                f"{diff['actual']!r}" + (" (deviation listed)" if covered else "")
            )
    else:
        lines.append("differences from reference: none")
    problems = (
        hard
        + [
            f"{diff['field']}: reference {diff['expected']!r} vs setup {diff['actual']!r}"
            for diff in uncovered
            if diff["field"] != "lock.mode"
        ]
        + stale
    )
    if not spec.reference_profile:
        problems.append(
            "no reference_profile named: set reference_profile "
            f"{profile_name!r} plus deviations [{{field, value, reason}}] "
            "for every intended difference"
        )
    if problems:
        lines.append("")
        lines.append("REFUSED:")
        lines.extend(f"  - {problem}" for problem in problems)
        return ("\n".join(lines) + "\n", False)
    if fingerprint["deviations"]:
        lines.append("")
        lines.append("declared deviations:")
        for item in fingerprint["deviations"]:
            lines.append(f"  - {item.get('field')}={item.get('value')!r} ({item.get('reason')})")
    lines.append("")
    lines.append("OK: setup matches reference (deviations declared).")
    return ("\n".join(lines) + "\n", True)


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 24:
        return value[:18] + "…" + value[-6:]
    return value


def write_batch_fingerprint(
    job_dir: Path, fingerprint: dict[str, Any], trial_dirs: list[Path]
) -> None:
    """Persist the batch fingerprint and one observed fingerprint per trial."""
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / FINGERPRINT_FILENAME).write_text(
        json.dumps(fingerprint, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for trial_dir in trial_dirs:
        if not trial_dir.is_dir():
            continue
        (trial_dir / FINGERPRINT_FILENAME).write_text(
            json.dumps(trial_fingerprint(fingerprint, trial_dir), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def maybe_write_fingerprints(job_dir: Path, fingerprint: dict[str, Any]) -> str | None:
    """Best-effort fingerprint persistence; never fails the run. Returns error or None."""
    try:
        trial_dirs = [
            candidate
            for candidate in job_dir.iterdir()
            if candidate.is_dir() and "__" in candidate.name
        ]
        write_batch_fingerprint(job_dir, fingerprint, trial_dirs)
    except Exception as exc:  # noqa: BLE001 — evidence write, not dispatch gate
        with suppress(OSError):
            (job_dir / "setup-fingerprint-error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
            )
        return f"{type(exc).__name__}: {exc}"
    return None


def task_digest_for(task_dir: Path) -> str:
    """Package digest of a task directory (single task-identity helper)."""
    _, digest = task_bytes_and_digest(task_dir)
    return digest
