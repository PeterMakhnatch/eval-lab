"""Per-trial posture for the HAR-178 trials view (legit census).

Deterministic small-metadata readers over landed trial/job files: no
trajectory parsing, no projection writes, no network, no database. Every
reader fails closed (unknown instead of a guessed value) so unfinished,
infra, and legacy trials stay census rows.

Sources per key (all recorded, never reconstructed):

- ``campaign``: explicit ``linear_card`` from ``lab-metadata.json``
  ``experiment`` plus the frozen ``experiment-spec.json``, under the
  existing :func:`evallab.results_home.card_from` rules. Disagreement or
  malformed values yield unattributed (``None``), never a guessed card.
- ``date``: full recorded ``started_at`` timestamp from the trial
  ``result.json``, else the job ``result.json`` (the view casts to
  TIMESTAMPTZ). Never filesystem mtime.
- ``model``: observed ``agent_info.model_info`` id first, then landed
  ``result.config``, then the trial ``config.json`` model, normalized
  only for the canonical native id and strict ``selfhosted/`` selectors
  (adapter variants stay distinct).
- ``harness``: recorded agent import path or name, one
  precedence-ordered agent record at a time (landed ``result.config``
  before the config file); the ``mimoagent`` alias maps to the known
  native class, nothing else is heuristically mapped.
- ``egress_lock``: actual applied lock from ``trial/egress-lock.json``
  (``applied is True``). Intent alone is not evidence.
- ``reference_profile`` / ``reference_profile_match`` /
  ``reference_profile_diffs``: HAR-149 exact match of the observed
  setup fingerprint against the named reference profile. A present
  trial ``setup-fingerprint.json`` decides alone (corrupt stays
  nonmatching, never replaced by intention); only an absent trial file
  falls back to the job fingerprint or the lock-covered
  ``EVALLAB_SETUP_FINGERPRINT`` agent-env record, each through
  :func:`trial_fingerprint` (observed lock override). Profile names
  with path components are rejected. Declared deviations never erase a
  difference; unsourced reference fields never disqualify; explicitly
  unbounded (``None``) budget ceilings are not a restriction; anything
  else unobserved fails closed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["cut_short_by_our_limits", "trial_posture"]


def trial_posture(
    *,
    repo_root: Path,
    job_dir: Path,
    trial_dir: Path,
    result: dict,
) -> dict:
    """One posture row for a single landed trial.

    ``result`` is the trial's parsed ``result.json`` (``{}`` when the
    trial has none yet: active/unsealed trials stay census rows with
    unknown posture, never an exception). Never raises on missing or
    malformed files; every unresolvable key fails closed.
    """
    job_path = Path(job_dir)
    trial_path = Path(trial_dir)
    record = result if isinstance(result, dict) else {}
    config = _read_dict(trial_path / "config.json")
    fingerprint = _resolve_fingerprint(job_path, trial_path, record, config)
    spec = _read_dict(job_path / "experiment-spec.json")
    profile_name, profile_state = _reference_claim(spec, fingerprint)
    reference_match, diffs_json = _reference_match(
        repo_root, profile_name, profile_state, fingerprint
    )

    return {
        "campaign": _campaign(job_path, spec),
        "date": _recorded_date(record, job_path),
        "model": _model(record, config),
        "harness": _harness(record, config),
        "egress_lock": _observed_egress_lock(trial_path),
        "reference_profile": profile_name,
        "reference_profile_match": reference_match,
        "reference_profile_diffs": diffs_json,
        "infra": _infra(record),
        "infra_exception": _infra_exception(record),
        "laminar_trace_id": _laminar_trace_id(trial_path),
    }


def cut_short_by_our_limits(stop_reason: str | None) -> bool | None:
    """Whether HAR-156 attributes this stop to our own limits.

    Exactly ``our_limit`` reads ``True``, ``unknown`` reads ``None``,
    every other known category (native harness cap, task timeout,
    model end, error) reads ``False``.
    """
    from evallab.step_layers import stop_category

    category = stop_category(stop_reason)
    if category == "our_limit":
        return True
    if category == "unknown":
        return None
    return False


def _read_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sound_fingerprint(payload: Any) -> bool:
    from evallab.setup_fingerprint import FINGERPRINT_SCHEMA

    return (
        isinstance(payload, dict) and payload.get("schema") == FINGERPRINT_SCHEMA
    )


def _resolve_fingerprint(
    job_dir: Path,
    trial_dir: Path,
    record: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Best recorded observed fingerprint, never reconstructed.

    A present trial ``setup-fingerprint.json`` decides alone: sound wins,
    corrupt or schema-invalid stays nonmatching with no fallback to
    intention. Only an absent trial file falls back to the job
    fingerprint or the lock-covered ``EVALLAB_SETUP_FINGERPRINT``
    agent-env record, each through :func:`trial_fingerprint` (observed
    lock override). Present-day serving defaults are never consulted.
    """
    from evallab.setup_fingerprint import FINGERPRINT_FILENAME, trial_fingerprint

    if (trial_dir / FINGERPRINT_FILENAME).is_file():
        trial_fp = _read_dict(trial_dir / FINGERPRINT_FILENAME)
        return trial_fp if _sound_fingerprint(trial_fp) else None
    job_fp = _read_dict(job_dir / FINGERPRINT_FILENAME)
    if _sound_fingerprint(job_fp):
        try:
            return trial_fingerprint(job_fp, trial_dir)
        except Exception:  # noqa: BLE001 -- corrupt record fails closed below
            pass
    env_fp = _lock_covered_fingerprint(record, config, job_dir, trial_dir)
    if env_fp is None or not _sound_fingerprint(env_fp):
        return None
    try:
        return trial_fingerprint(env_fp, trial_dir)
    except Exception:  # noqa: BLE001 -- corrupt record fails closed
        return None


def _agent_env_sources(
    record: dict[str, Any], config: dict[str, Any], trial_dir: Path
) -> list[dict[str, Any]]:
    """Recorded agent-env mappings that may carry the lock-covered fingerprint."""
    sources: list[dict[str, Any]] = []
    landed = record.get("config")
    if isinstance(landed, dict):
        agent = landed.get("agent")
        if isinstance(agent, dict) and isinstance(agent.get("env"), dict):
            sources.append(agent["env"])
    lock_agent = _read_dict(trial_dir / "lock.json").get("agent")
    if isinstance(lock_agent, dict) and isinstance(lock_agent.get("env"), dict):
        sources.append(lock_agent["env"])
    file_agent = config.get("agent")
    if isinstance(file_agent, dict) and isinstance(file_agent.get("env"), dict):
        sources.append(file_agent["env"])
    return sources


def _lock_covered_fingerprint(
    record: dict[str, Any],
    config: dict[str, Any],
    job_dir: Path,
    trial_dir: Path,
) -> dict[str, Any] | None:
    """Recorded ``EVALLAB_SETUP_FINGERPRINT`` for old records, env first."""
    candidates: list[str] = []
    for source in _agent_env_sources(record, config, trial_dir):
        value = source.get("EVALLAB_SETUP_FINGERPRINT")
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    metadata = _read_dict(job_dir / "lab-metadata.json")
    command = metadata.get("command")
    if isinstance(command, list):
        for entry in command:
            if (
                isinstance(entry, str)
                and entry.startswith("EVALLAB_SETUP_FINGERPRINT=")
            ):
                candidates.append(entry[len("EVALLAB_SETUP_FINGERPRINT="):])
    for encoded in candidates:
        try:
            payload = json.loads(encoded)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None

def _clean_name(value: Any) -> tuple[str | None, bool]:
    """(cleaned, malformed): plain names clean, else malformed iff set.

    Names with path components never reach profile loading: no arbitrary
    profile traversal from a recorded string.
    """
    if value is None:
        return None, False
    if isinstance(value, str) and value.strip():
        name = value.strip()
        if "/" in name or "\\" in name or name in (".", ".."):
            return None, True
        return name, False
    return None, True


def _reference_claim(
    spec: dict[str, Any], fingerprint: dict[str, Any] | None
) -> tuple[str | None, str]:
    """Agreed reference-profile name and comparison state.

    ``"absent"`` (nothing named anywhere), ``"agreed"`` (one name), or
    ``"conflict"`` (spec and fingerprint disagree, or a malformed
    name): no arbitrary profile traversal, ever.
    """
    spec_name, spec_bad = _clean_name(spec.get("reference_profile"))
    fp_source = fingerprint if isinstance(fingerprint, dict) else {}
    fp_name, fp_bad = _clean_name(fp_source.get("reference_profile"))
    if spec_bad or fp_bad:
        return None, "conflict"
    if spec_name is not None and fp_name is not None:
        if spec_name != fp_name:
            return None, "conflict"
        return spec_name, "agreed"
    if spec_name is not None:
        return spec_name, "agreed"
    if fp_name is not None:
        return fp_name, "agreed"
    return None, "absent"


def _reference_match(
    repo_root: Path,
    profile_name: str | None,
    state: str,
    fingerprint: dict[str, Any] | None,
) -> tuple[bool | None, str]:
    """(match, diffs JSON) for the agreed reference, fail-closed.

    ``None`` match means no reference was ever named (nothing to compare);
    ``False`` means compared-and-not-matching or not comparable. Declared
    deviations never erase a difference; missing sourced evidence never
    becomes a match.
    """
    if state == "absent":
        return None, "[]"
    if state == "conflict" or profile_name is None or fingerprint is None:
        return False, "[]"
    from evallab.setup_fingerprint import compare_fingerprint, load_reference_profile

    try:
        profile = load_reference_profile(Path(repo_root), profile_name)
    except (ValueError, OSError):
        return False, "[]"
    try:
        diffs = compare_fingerprint(fingerprint, profile)
    except Exception:  # noqa: BLE001 -- malformed record fails closed
        return False, "[]"
    if diffs:
        return False, json.dumps(diffs, sort_keys=True, default=str)
    if not _observations_known(fingerprint, profile):
        return False, "[]"
    return True, "[]"


def _observations_known(fingerprint: dict[str, Any], profile: dict[str, Any]) -> bool:
    """Every sourced compared field has its required observation.

    Unsourced reference fields never disqualify. An explicitly ``None``
    budget ceiling is unbounded (no ceiling to violate); any other
    absent or ``None`` observation fails closed instead of matching.
    """
    from evallab.setup_fingerprint import (
        CEILING_REFERENCES,
        COMPARED_FIELDS,
        _reference_value,
    )

    for field in COMPARED_FIELDS:
        reference_field = CEILING_REFERENCES.get(field, field)
        _, sourced, _ = _reference_value(profile, reference_field)
        if not sourced:
            continue
        section, _, key = field.partition(".")
        node = fingerprint.get(section)
        if not isinstance(node, dict) or key not in node:
            return False
        if node[key] is None and field not in CEILING_REFERENCES:
            return False
    return True


def _campaign(job_dir: Path, spec: dict[str, Any]) -> str | None:
    """Recorded card under existing attribution rules, else unattributed."""
    from evallab.results_home import _explicit_linear_card, card_from

    metadata = _read_dict(job_dir / "lab-metadata.json")
    try:
        explicit = _explicit_linear_card(metadata, spec)
    except ValueError:
        return None
    question_ref = spec.get("question_ref")
    name_card = card_from(
        Path(job_dir).name,
        question_ref if isinstance(question_ref, str) else None,
    )
    if explicit is not None:
        if name_card is not None and name_card != explicit:
            return None
        return explicit
    return name_card


def _iso_timestamp(value: Any) -> str | None:
    """Full recorded ISO timestamp, else None. The view casts to TIMESTAMPTZ."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.strip()


def _recorded_date(record: dict[str, Any], job_dir: Path) -> str | None:
    for value in (
        record.get("started_at"),
        _read_dict(job_dir / "result.json").get("started_at"),
    ):
        stamp = _iso_timestamp(value)
        if stamp is not None:
            return stamp
    return None


def _agent_records(config: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Agent records, landed ``result.config`` before the config file."""
    records: list[dict[str, Any]] = []
    landed = record.get("config")
    if isinstance(landed, dict):
        landed_agent = landed.get("agent")
        if isinstance(landed_agent, dict):
            records.append(landed_agent)
    agent = config.get("agent")
    if isinstance(agent, dict):
        records.append(agent)
    return records


def _first_text(records: list[dict[str, Any]], key: str) -> str | None:
    for entry in records:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _observed_model_info(record: dict[str, Any]) -> dict[str, Any]:
    """Observed ``agent_info.model_info`` mapping, else empty (trial_facts order)."""
    info = record.get("agent_info")
    info = info if isinstance(info, dict) else {}
    model_info = info.get("model_info")
    return model_info if isinstance(model_info, dict) else {}


def _normalize_model(raw: str | None) -> str | None:
    """Canonical native or strict selfhosted selector only; adapters stay distinct."""
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_MODEL_PREFIX,
        MIMO_SELFHOSTED_NATIVE_MODELS,
        parse_mimo_selfhosted_model,
    )

    if raw is None:
        return None
    if raw in MIMO_SELFHOSTED_NATIVE_MODELS:
        return raw
    if raw.startswith(MIMO_SELFHOSTED_MODEL_PREFIX):
        try:
            return parse_mimo_selfhosted_model(raw)
        except ValueError:
            return None
    return raw


def _model(record: dict[str, Any], config: dict[str, Any]) -> str | None:
    """Observed model id first, then landed config, then the config file."""
    observed = _observed_model_info(record)
    raw = _first_text([observed], "name") or _first_text([observed], "model_name")
    if raw is None:
        raw = _first_text(_agent_records(config, record), "model_name")
    return _normalize_model(raw)


def _harness(record: dict[str, Any], config: dict[str, Any]) -> str | None:
    """Actual recorded agent import path (``mimoagent`` alias resolved).

    Each precedence-ordered agent record contributes its import path or
    its name before the next record is consulted, so a stale
    lower-priority config file cannot override the landed record.
    """
    from evallab.execution_contracts import MIMO_AGENT_IMPORT_PATH

    raw: str | None = None
    for entry in _agent_records(config, record):
        raw = _first_text([entry], "import_path") or _first_text([entry], "name")
        if raw is not None:
            break
    if raw is None:
        info = record.get("agent_info")
        if isinstance(info, dict):
            name = info.get("name")
            raw = name.strip() if isinstance(name, str) and name.strip() else None
    if raw is None:
        return None
    if raw == "mimoagent":
        return MIMO_AGENT_IMPORT_PATH
    return raw


def _observed_egress_lock(trial_dir: Path) -> bool:
    """True only when the trial record shows the lock actually applied."""
    try:
        payload = json.loads((Path(trial_dir) / "egress-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("applied") is True


def _infra(record: dict[str, Any]) -> bool:
    """Canonical counts infra reason only: never copy/exclusion status."""
    from evallab.campaign_approval import trial_is_infra_excluded

    verifier = record.get("verifier_result")
    verifier = verifier if isinstance(verifier, dict) else {}
    rewards = verifier.get("rewards")
    rewards = rewards if isinstance(rewards, dict) else {}
    return bool(trial_is_infra_excluded(record, rewards))


def _infra_exception(record: dict[str, Any]) -> str | None:
    """Native exception class when the trial recorded one, else None."""
    exception = record.get("exception_info")
    if not isinstance(exception, dict):
        return None
    value = exception.get("exception_type") or exception.get("type")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _laminar_trace_id(trial_dir: Path) -> str | None:
    """Trace id from the ``trial/laminar-trace.json`` SDK marker only.

    No marker (or an invalid one) reads ``None``: never the deterministic
    cloud-trace fallback, never a trace URL from unrelated runtime.
    """
    from evallab.laminar import _sdk_trace_reference

    reference = _sdk_trace_reference(Path(trial_dir))
    if reference is None:
        return None
    trace_id, _ = reference
    return trace_id or None
