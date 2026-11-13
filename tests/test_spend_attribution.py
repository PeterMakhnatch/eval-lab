"""Explicit card attribution for spend ledgers (HAR-145).

Consumer-visible behaviour (never wiring):
- an explicit ``linear_card`` wins over the job-name prefix when they
  agree, fails closed on conflict or malformed values, and is never
  guessed from task/model/app names;
- Modal billed totals split by app conserve the per-object
  ``max(daily, hourly)`` total, keep an unattributed residual, and bind
  a card only through an explicit app binding;
- Daytona/model rows attribute via the catalog ``lab_metadata``
  explicit card, leaving pre-explicit jobs unattributed with a note;
- publish-time provenance prefers the explicit card fail-closed;
- an empty day renders a fixed $0 ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.schemas import normalize_linear_card

# ---------------------------------------------------------------------------
# 1. Explicit card normalization and resolver precedence
# ---------------------------------------------------------------------------


def test_normalize_linear_card_variants() -> None:
    assert normalize_linear_card("har126") == "HAR-126"
    assert normalize_linear_card("HAR-126") == "HAR-126"
    assert normalize_linear_card("har-0126") == "HAR-126"
    assert normalize_linear_card(None) is None
    assert normalize_linear_card("   ") is None
    assert normalize_linear_card("") is None
    for bad in ("G5", "HAR", "HAR-12x", "mimo-v2.6-rl", "evallab-mimo-v26-9b", 126):
        with pytest.raises(ValueError):
            normalize_linear_card(bad)  # type: ignore[arg-type]







# ---------------------------------------------------------------------------
# 2. Modal per-app split conserves the billed total (fake catalog)
# ---------------------------------------------------------------------------


















# ---------------------------------------------------------------------------
# 3. Daytona rows attribute via the catalog explicit card (fake catalog)
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# 5. Publish-time provenance prefers the explicit card, fail-closed
# ---------------------------------------------------------------------------


def _provenance_job(
    root: Path, name: str, *, experiment_card: str | None, spec_card: str | None
) -> Path:
    job = root / name
    job.mkdir(parents=True)
    metadata: dict[str, Any] = {"started_at": "2026-10-01T10:00:00+00:00"}
    if experiment_card is not None:
        metadata["experiment"] = {"linear_card": experiment_card}
    (job / "lab-metadata.json").write_text(json.dumps(metadata))
    if spec_card is not None:
        (job / "experiment-spec.json").write_text(
            json.dumps({"linear_card": spec_card})
        )
    return job


def test_provenance_explicit_card_binds_unprefixed_job(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "ovn-g5-000169-stock", experiment_card="har126", spec_card=None
    )
    provenance = build_provenance(job, pr_lookup=lambda _: None)
    assert provenance["card"] == "HAR-126"
    assert provenance["card_assignment"] == {
        "source": "experiment_linear_card",
        "card": "HAR-126",
    }


def test_provenance_explicit_conflict_fails_closed(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "har120-000001-a1", experiment_card="HAR-126", spec_card=None
    )
    with pytest.raises(ValueError, match="conflicts"):
        build_provenance(job, pr_lookup=lambda _: None)
    disagreeing = _provenance_job(
        tmp_path, "ovn-g5-x", experiment_card="HAR-126", spec_card="HAR-131"
    )
    with pytest.raises(ValueError, match="disagree"):
        build_provenance(disagreeing, pr_lookup=lambda _: None)
    malformed = _provenance_job(
        tmp_path, "ovn-g5-y", experiment_card="G5", spec_card=None
    )
    with pytest.raises(ValueError, match="HAR issue identifier"):
        build_provenance(malformed, pr_lookup=lambda _: None)


def test_provenance_recorded_card_unchanged_without_explicit(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "har120-000001-a1", experiment_card=None, spec_card=None
    )
    provenance = build_provenance(job, pr_lookup=lambda _: None)
    assert provenance["card"] == "HAR-120"
    assert "card_assignment" not in provenance


# ---------------------------------------------------------------------------
# 6. Empty day renders a fixed $0 ledger (isolated fixture)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. Declared exact-name bindings (policy/spend-attribution.yaml)
# ---------------------------------------------------------------------------

_MINI_POLICY = """\
schema: evallab.spend_attribution/v1
source:
  path: research/experiments/ovn-sft-v0/G5-RUN.md
  card_evidence: "# G5 run record (HAR-126)"
  recorded: "2026-10-01"
job_cards:
  ovn-g5-001695-stock: har126
app_cards: {}
"""














