from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.production.live_supervisor import Decision, Observation, Subject
from evallab.production.live_supervisor_act import (
    LabActuator,
    RetryAdmission,
    register_app,
)
from evallab.production.live_supervisor_gate import (
    GATE_DIGEST_FILE,
    GATE_EVIDENCE_FILE,
    GATE_SCHEMA,
    MIN_SAMPLES,
    DeniedGate,
    EvidenceGate,
    GateEvidenceError,
    Scope,
    adjudication_scopes,
    gate_for_root,
    gate_from_adjudication,
    parse_evidence,
    reason_trigger,
    write_evidence,
)

T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
KILL = Decision("kill_and_retry", "loop", "repetition: same call 12x")


def _app(app_id: str = "ap-1") -> Observation:
    return Observation(Subject("modal_app", app_id, "lab", True, label="sb1-seeker"), T0, (), {})


def _scope(**kw) -> Scope:
    base = dict(action="kill_and_retry", subject_kind="modal_app",
                detector_version="live-supervisor/1", reader_model="opus-4", trigger="repetition",
                true_positives=38, false_positives=4, samples=42)
    return Scope(**(base | kw))


def _decision(**kw) -> Decision:
    base = dict(action="kill_and_retry", kind="loop", reason="repetition: same call 12x",
                reader_model="opus-4")
    return Decision(**(base | kw))


def _gate(tmp_path: Path, *scopes: Scope, **kw) -> tuple[EvidenceGate, Path]:
    path = tmp_path / GATE_EVIDENCE_FILE
    digest = write_evidence(path, scopes or (_scope(),))
    return EvidenceGate(path, expected_sha256=digest, **kw), path


# --------------------------------------------------------------------------- precision and samples


def test_precision_above_the_bar_allows_the_exact_scope(tmp_path: Path) -> None:
    gate, _ = _gate(tmp_path)
    assert gate.refusal(_app(), _decision()) is None
    assert gate(_app(), _decision()) is True


def test_precision_below_nine_tenths_is_refused(tmp_path: Path) -> None:
    gate, _ = _gate(tmp_path, _scope(true_positives=34, false_positives=8, samples=42))
    why = gate.refusal(_app(), _decision())
    assert why is not None and why.startswith("gate precision low: 0.810 < 0.90")


def test_thin_samples_are_refused_even_at_perfect_precision(tmp_path: Path) -> None:
    gate, _ = _gate(tmp_path, _scope(true_positives=5, false_positives=0, samples=5))
    why = gate.refusal(_app(), _decision())
    assert why is not None and why.startswith("gate samples thin: 5 < 20")
    assert EvidenceGate(gate.path, expected_sha256=gate.expected_sha256, min_samples=5)(
        _app(), _decision()) is True


def test_inconsistent_counts_are_off_schema() -> None:
    with pytest.raises(GateEvidenceError, match="must equal samples"):
        _scope(true_positives=10, false_positives=1, samples=20)
    with pytest.raises(GateEvidenceError, match="action"):
        _scope(action="kill_everything")
    with pytest.raises(GateEvidenceError, match="trigger"):
        _scope(trigger="vibes")
    with pytest.raises(GateEvidenceError, match="scopes"):
        parse_evidence({"schema": GATE_SCHEMA, "scopes": []})
    with pytest.raises(GateEvidenceError, match="schema"):
        parse_evidence({"schema": "other/v1", "scopes": [{}]})


# --------------------------------------------------------------------------- scoping


@pytest.mark.parametrize(("decision", "observation", "fragment"), [
    (_decision(reader_model="gpt-5"), _app(), "by gpt-5"),
    (_decision(reader_model=None), _app(), "by rules"),
    (_decision(action="kill_and_mark_infra"), _app(), "kill_and_mark_infra"),
    (_decision(), Observation(Subject("harbor_trial", "job-a/t1", "lab", True), T0, (), {}),
     "on harbor_trial"),
    (_decision(reason="wall_time: over budget"), _app(), "by opus-4"),
])
def test_scope_mismatch_is_never_pooled(tmp_path: Path, decision, observation, fragment) -> None:
    gate, _ = _gate(tmp_path)
    why = gate.refusal(observation, decision)
    assert why is not None and "gate scope not measured" in why and fragment in why


def test_trigger_wildcard_scope_covers_every_trigger(tmp_path: Path) -> None:
    gate, _ = _gate(tmp_path, _scope(trigger=None))
    assert gate.refusal(_app(), _decision(reason="wall_time: 2x budget")) is None
    assert gate.refusal(_app(), _decision(reason="no trigger prefix")) is None


def test_reason_trigger_reads_rule_table_prefixes() -> None:
    assert reason_trigger("repetition: same call 12x") == "repetition"
    assert reason_trigger("model reader rewrote this") is None
    assert reason_trigger("repetition without colon") is None


# --------------------------------------------------------------------------- digest pinning


def test_evidence_change_after_pinning_fails_closed(tmp_path: Path) -> None:
    gate, path = _gate(tmp_path)
    assert gate.refusal(_app(), _decision()) is None
    path.write_text(path.read_text().replace('"false_positives": 4', '"false_positives": 0'))
    why = gate.refusal(_app(), _decision())
    assert why is not None and why.startswith("gate evidence changed:")


def test_missing_evidence_fails_closed(tmp_path: Path) -> None:
    gate = EvidenceGate(tmp_path / "absent.json", expected_sha256="0" * 64)
    assert gate.refusal(_app(), _decision()) == f"gate evidence missing: {tmp_path / 'absent.json'}"
    assert gate(_app(), _decision()) is False


def test_gate_for_root_denies_until_a_digest_is_pinned(tmp_path: Path) -> None:
    scopes = (_scope(),)
    digest = write_evidence(tmp_path / GATE_EVIDENCE_FILE, scopes)
    unpinned = gate_for_root(tmp_path)
    assert isinstance(unpinned, DeniedGate)
    assert "digest not pinned" in (unpinned.refusal(_app(), _decision()) or "")
    (tmp_path / GATE_DIGEST_FILE).write_text(f"{digest}  gate-evidence-v1.json\n")
    pinned = gate_for_root(tmp_path)
    assert isinstance(pinned, EvidenceGate)
    assert pinned.refusal(_app(), _decision()) is None


# --------------------------------------------------------------------------- actuator integration


def test_actuator_kills_only_with_registration_and_measurable_evidence(tmp_path: Path) -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def __call__(self, argv) -> int:
            self.calls.append(tuple(argv))
            return 0

    runner = Runner()
    root = tmp_path / "ls"
    digest = write_evidence(root / GATE_EVIDENCE_FILE, (_scope(reader_model=None),))
    (root / GATE_DIGEST_FILE).write_text(digest)
    act = LabActuator(root=root, dry_run=False, runner=runner, gate=gate_for_root(root),
                      retry_admitter=lambda obs, d: RetryAdmission(True, "queued", "rq-1"),
                      act_actions=frozenset({"kill_and_retry"}),
                      queue_path=tmp_path / "q.jsonl", alerts_path=tmp_path / "a.md",
                      clock=lambda: T0)
    rules = _decision(reader_model=None)
    assert act.act(_app("ap-1"), rules) is False  # not registered yet
    assert "unregistered" in json.loads((root / "actions-v1.jsonl").read_text().splitlines()[-1])["detail"]
    register_app(root, "ap-1", registered_by="test")
    assert act.act(_app("ap-1"), rules) is True
    assert runner.calls == [("keys", "run", "--", "modal", "app", "stop", "ap-1")]
    assert act.act(_app("ap-2"), rules) is False  # registration is per immutable app_id


# --------------------------------------------------------------------------- LS-3 adjudication


def _headline(replay: dict, seeker: dict | None = None) -> dict:
    return {"gate": {"threshold": 0.9},
            "replay": {"model": {"per_action": replay.get("model", {})},
                       "rules": {"per_action": replay.get("rules", {})}},
            "seeker_pilot": {"model": {"per_action": (seeker or {}).get("model", {})},
                             "rules": {"per_action": (seeker or {}).get("rules", {})}}}


def _stats(tp: int, fp: int) -> dict:
    return {"tp": tp, "fp": fp, "n_decided": tp + fp, "precision": None, "recall": None}


def test_adjudication_adapter_keeps_the_worst_measured_source() -> None:
    headline = _headline(
        replay={"model": {"kill_and_retry": _stats(6, 1)}, "rules": {"kill_and_mark_infra": _stats(40, 0)}},
        seeker={"model": {"kill_and_retry": _stats(1, 0)}, "rules": {"kill_and_mark_infra": _stats(1, 0)}})
    scopes = adjudication_scopes(headline)
    by_key = {s.key(): s for s in scopes}
    retry = by_key["kill_and_retry|live-supervisor/1|*|*|*"]
    assert (retry.true_positives, retry.false_positives, retry.samples) == (6, 1, 7)
    assert retry.source == "replay/model" and retry.precision == pytest.approx(6 / 7)
    infra = by_key["kill_and_mark_infra|live-supervisor/1|*|rules|*"]
    assert infra.samples == 1 and infra.source == "seeker_pilot/rules"  # thin, worst source wins
    assert "continue" not in {s.action for s in scopes}


def test_adjudication_evidence_refuses_an_action_below_the_bar(tmp_path: Path) -> None:
    headline = _headline(replay={"model": {"kill_and_retry": _stats(6, 1)}},
                         seeker={"model": {"kill_and_retry": _stats(1, 0)}})
    path = tmp_path / "headline.json"
    path.write_text(json.dumps(headline))
    gate = gate_from_adjudication(tmp_path / "ls", path)
    model = _decision(reader_model="anthropic/claude-sonnet-5-5")  # any reader matches the arm
    why = gate.refusal(_app(), model)
    assert why is not None and why.startswith("gate samples thin: 7 < 20")  # LS-3's best n is 7
    assert (tmp_path / "ls" / GATE_DIGEST_FILE).read_text().strip() == gate.expected_sha256


def test_adjudication_evidence_refuses_low_precision_at_a_full_sample(tmp_path: Path) -> None:
    headline = _headline(replay={"rules": {"kill_and_retry": _stats(19, 6)}})  # 25 cases, 0.76
    path = tmp_path / "headline.json"
    path.write_text(json.dumps(headline))
    gate = gate_from_adjudication(tmp_path / "ls", path)
    why = gate.refusal(_app(), _decision(reader_model=None))
    assert why is not None and why.startswith("gate precision low: 0.760 < 0.90")


def test_adjudication_evidence_allows_a_source_that_measured_the_action_safely(tmp_path: Path) -> None:
    headline = _headline(
        replay={"rules": {"kill_and_mark_infra": _stats(30, 1)}, "model": {}},
        seeker={"rules": {"kill_and_mark_infra": _stats(24, 0)}})
    path = tmp_path / "headline.json"
    path.write_text(json.dumps(headline))
    gate = gate_from_adjudication(tmp_path / "ls", path)
    decision = _decision(action="kill_and_mark_infra", reason="error_signature: backend gone",
                         reader_model=None)
    assert gate.refusal(_app(), decision) is None
    assert gate.refusal(_app(), _decision(reader_model="anthropic/claude-sonnet-5-5")) is not None


def test_min_samples_is_twenty() -> None:
    assert MIN_SAMPLES == 20


def test_adjudication_rows_that_are_empty_or_inconsistent_are_skipped(tmp_path: Path) -> None:
    headline = _headline(replay={"rules": {
        "kill_and_retry": {"tp": 0, "fp": 0, "n_decided": 0},
        "kill_and_mark_infra": {"tp": 3, "fp": 1, "n_decided": 99},
        "alert_research": _stats(1, 0),
    }})
    scopes = adjudication_scopes(headline)
    assert [s.action for s in scopes] == ["alert_research"]
    assert scopes[0].samples == 1  # present but thin: the gate refuses it on samples
