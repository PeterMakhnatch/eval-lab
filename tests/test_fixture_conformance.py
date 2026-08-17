"""Every committed fixture that claims to be an ATIF document must be one.

The defect this file exists to prevent, in full: the explorer read trajectory
observations from ``step["observations"]`` — a key that appears in no Harbor
output and in no validator — while ``evallab.atif`` validates
``step["observation"]["results"]`` (``src/evallab/atif.py:296-306``). Every
committed ATIF fixture used the invented key, so the explorer's observation
rendering was only ever exercised against documents that could not exist. Zero
of the 58 observation results in ``research/evidence/runs/`` were rendered, and
the suite stayed green from the first commit until PR #66.

The measurement that named the class: run the ingest's own validation over
every tracked JSON file whose ``schema_version`` starts with ``ATIF-``. All ten
real documents (nine promoted Codex trajectories plus the Harbor 0.21 capture
under ``research/explorations/``) were valid. All seven committed fixtures were
invalid, each failing first on ``agent.name must be a string`` — they had no
top-level ``agent`` object at all. Validation was never the weak link; only the
test inputs were, and nothing compared the two populations.

So this guard compares them, pushing every ATIF-claiming fixture through the
same entry point ``atif.ingest_and_project`` uses and naming the fixture and
the offending field when one could not exist.

A fixture that is *meant* to be rejected declares that in the document::

    "evallab_fixture_expectation": {
        "validation_status": "invalid",
        "error_contains": "steps must be a non-empty array",
        "why": "exercises the refusal path for an empty trajectory"
    }

The declaration is checked in both directions: a document claiming to be
invalid must actually fail, and must fail with the error it names. That is what
separates this from the skip-list it deliberately is not — a declaration cannot
mute a fixture, only pin the exact refusal the fixture exists to prove. Extra
top-level keys are safe to carry: an ATIF document is a plain mapping to both
validators (``atif._validate_fallback`` and the installed
``harbor_atif2otel.validate``), and every promoted trajectory already carries
``evallab_redaction`` the same way.

Documents built inside a test at run time are out of scope on purpose. Several
exist to drive degradation paths — a step with no ``message``, a trajectory that
is not JSON at all — and belong to the test that reads them. The standing claim
this guard enforces is about files committed to the repository, which is where
drift survives long enough to hide a defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.atif import _document_validation

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PROMOTED_RUNS = ROOT / "research" / "evidence" / "runs"

#: Set by a fixture that exists to be refused. See the module docstring.
EXPECTATION_KEY = "evallab_fixture_expectation"

#: Field names carried by no ATIF document, each mapped to what really holds the
#: value. Validation does not reject these — it simply never reads them, which
#: is precisely how 58 observation results stayed invisible while every test
#: passed — so they are banned by name rather than left to a validator.
INVENTED_FIELDS = {
    "steps[].observations": "steps[].observation.results",
    "steps[].tool_calls[].function": "steps[].tool_calls[].function_name plus .arguments",
    "observation.results[].command_exit_code": (
        "observation.results[].extra.exit_code — command_exit_code is a derived "
        "projection column (atif.py:132, atif.py:744), not a document field"
    ),
}


def _claims_to_be_atif(payload: object) -> bool:
    """Whether a parsed file presents itself to the ingest as an ATIF document.

    This is the ingest's own test: ``atif._initial_candidates`` accepts a file
    under ``agent/`` when its name starts with ``trajectory`` *or* its
    ``schema_version`` starts with ``ATIF-``. Anything that answers yes here
    gets validated in production, so it is in scope here.
    """
    return isinstance(payload, dict) and str(payload.get("schema_version", "")).startswith("ATIF-")


def _atif_documents(root: Path) -> list[Path]:
    found = []
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # unparseable, therefore claiming nothing
        if _claims_to_be_atif(payload):
            found.append(path)
    return found


FIXTURE_DOCUMENTS = _atif_documents(FIXTURES)
PROMOTED_DOCUMENTS = _atif_documents(PROMOTED_RUNS)


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _invented_fields(payload: dict) -> list[str]:
    found: list[str] = []
    for index, step in enumerate(payload.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if "observations" in step:
            found.append(f"steps[{index}].observations")
        for call_index, call in enumerate(step.get("tool_calls") or []):
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                found.append(f"steps[{index}].tool_calls[{call_index}].function")
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else []
        for result_index, result in enumerate(results or []):
            if isinstance(result, dict) and "command_exit_code" in result:
                found.append(
                    f"steps[{index}].observation.results[{result_index}].command_exit_code"
                )
    return found


def _expected_field(name: str) -> str:
    if ".observations" in name:
        return INVENTED_FIELDS["steps[].observations"]
    if name.endswith(".function"):
        return INVENTED_FIELDS["steps[].tool_calls[].function"]
    return INVENTED_FIELDS["observation.results[].command_exit_code"]


def conformance_failure(path: Path) -> str | None:
    """The guard's whole judgement of one document: a message, or None to pass.

    Validation runs through ``atif._document_validation``, the function
    ``atif._project_payload`` calls for every document it lands, so a verdict
    here is the verdict the catalog and the Parquet projection would record.
    The trial directory is the document's grandparent because ATIF lives at
    ``<trial>/agent/<file>.json``; it is what bounds reference resolution.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    status, _validator, error = _document_validation(payload, path, path.parent.parent)
    declared = payload.get(EXPECTATION_KEY)
    declared = declared if isinstance(declared, dict) else None

    invented = _invented_fields(payload)
    if invented:
        return "\n".join(
            f"{_rel(path)}: {name} is a field no ATIF document has; the real one is "
            f"{_expected_field(name)}"
            for name in invented
        )

    if declared is None:
        if status == "valid":
            return None
        return (
            f"{_rel(path)}: claims to be {payload.get('schema_version')} but the ingest "
            f"reports {status} — {error}. Bring the fixture to the shape "
            f"src/evallab/atif.py validates, or, if it exists to prove a refusal, declare "
            f'that with "{EXPECTATION_KEY}": '
            f'{{"validation_status": "{status}", "error_contains": ..., "why": ...}}.'
        )

    expected_status = declared.get("validation_status")
    if expected_status not in {"invalid", "unsupported"}:
        return (
            f"{_rel(path)}: declares {EXPECTATION_KEY}.validation_status "
            f"{expected_status!r}; only a deliberately refused document needs a declaration."
        )
    if status != expected_status:
        return (
            f"{_rel(path)}: declares it is {expected_status} on purpose, but the ingest "
            f"reports {status} ({error}). A fixture that no longer proves its refusal is "
            f"stale — fix the declaration or delete the fixture."
        )
    fragment = declared.get("error_contains")
    if not isinstance(fragment, str) or not fragment:
        return (
            f"{_rel(path)}: must name the error it expects in "
            f"{EXPECTATION_KEY}.error_contains, so a declaration cannot mute an "
            f"unrelated defect."
        )
    if fragment not in (error or ""):
        return (
            f"{_rel(path)}: expects to be refused for {fragment!r} but the ingest refused "
            f"it for {error!r}. The fixture now tests a different refusal than it claims."
        )
    return None


def test_fixture_tree_contains_atif_documents_to_check():
    """A guard that silently matches nothing is worse than no guard."""
    assert FIXTURE_DOCUMENTS, f"no ATIF-claiming fixture found under {_rel(FIXTURES)}"


@pytest.mark.parametrize("path", FIXTURE_DOCUMENTS, ids=_rel)
def test_every_atif_fixture_is_a_document_that_could_exist(path: Path):
    """The guard. A fixture the ingest would reject fails the suite here."""
    assert conformance_failure(path) is None, conformance_failure(path)


@pytest.mark.parametrize("path", PROMOTED_DOCUMENTS, ids=_rel)
def test_promoted_evidence_passes_the_same_validation_as_the_fixtures(path: Path):
    """The other half of the comparison, and the reason the guard is fair.

    Fixtures are held to the standard real evidence already meets. If a promoted
    trajectory ever failed this, the validator — not the fixture — would be the
    thing to fix, and the two tests would disagree loudly instead of the suite
    quietly grading fiction.
    """
    assert conformance_failure(path) is None, conformance_failure(path)


# ---- the guard's own behaviour, proven on documents written here -------------


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return path


CONFORMANT = {
    "schema_version": "ATIF-v1.7",
    "session_id": "01a00420-0d94-7d50-8e01-00000000000f",
    "agent": {"name": "codex", "version": "0.147.0"},
    "steps": [
        {
            "step_id": 1,
            "source": "agent",
            "message": "m",
            "tool_calls": [
                {"tool_call_id": "d0", "function_name": "run_bash", "arguments": {"cmd": "make"}}
            ],
            "observation": {
                "results": [{"source_call_id": "d0", "content": "ok", "extra": {"exit_code": 0}}]
            },
        }
    ],
}

#: The exact shape every committed fixture used before 2026-08-16.
DRIFTED = {
    "schema_version": "ATIF-v1.6",
    "session_id": "s9",
    "steps": [
        {
            "step_id": 1,
            "source": "agent",
            "message": "m",
            "tool_calls": [
                {"tool_call_id": "d0", "function": {"name": "run_bash", "arguments": {}}}
            ],
            "observations": [{"source_call_id": "d0", "command_exit_code": 2}],
        }
    ],
}


def test_a_conformant_document_passes(tmp_path: Path):
    assert conformance_failure(_write(tmp_path / "t/agent/trajectory.json", CONFORMANT)) is None


def test_the_historical_drift_is_refused_naming_the_fixture_and_the_field(tmp_path: Path):
    """Both halves of the failure message the mission asked for."""
    failure = conformance_failure(_write(tmp_path / "t/agent/trajectory.json", DRIFTED))
    assert failure is not None
    assert "t/agent/trajectory.json" in failure                      # the fixture
    assert "steps[0].observations" in failure                        # the field
    assert "steps[].observation.results" in failure                  # what it should be
    assert "steps[0].tool_calls[0].function" in failure
    assert "function_name" in failure


def test_a_document_missing_its_agent_is_refused_with_the_validator_error(tmp_path: Path):
    """The error all seven fixtures hit first, reported verbatim from `atif`."""
    without_agent = {key: value for key, value in CONFORMANT.items() if key != "agent"}
    failure = conformance_failure(_write(tmp_path / "t/agent/trajectory.json", without_agent))
    assert failure is not None and "agent.name must be a string" in failure
    assert EXPECTATION_KEY in failure  # and it says how to declare a real refusal


def test_a_declared_refusal_is_accepted_when_it_still_happens(tmp_path: Path):
    empty = {**CONFORMANT, "steps": []}
    empty[EXPECTATION_KEY] = {
        "validation_status": "invalid",
        "error_contains": "steps must be a non-empty array",
        "why": "exercises the refusal path for a trajectory with no steps",
    }
    assert conformance_failure(_write(tmp_path / "t/agent/trajectory.json", empty)) is None


def test_a_declaration_naming_the_wrong_error_is_refused(tmp_path: Path):
    """A declaration pins one refusal; it can never mute a different defect."""
    empty = {**CONFORMANT, "steps": []}
    empty[EXPECTATION_KEY] = {
        "validation_status": "invalid",
        "error_contains": "agent.name must be a string",
        "why": "stale claim",
    }
    failure = conformance_failure(_write(tmp_path / "t/agent/trajectory.json", empty))
    assert failure is not None and "different refusal than it claims" in failure


def test_a_declaration_on_a_document_that_now_validates_is_refused(tmp_path: Path):
    """A fixture that stopped proving its refusal is stale, not passing."""
    stale = dict(CONFORMANT)
    stale[EXPECTATION_KEY] = {
        "validation_status": "invalid",
        "error_contains": "steps must be a non-empty array",
        "why": "no longer refused",
    }
    failure = conformance_failure(_write(tmp_path / "t/agent/trajectory.json", stale))
    assert failure is not None and "no longer proves its refusal is stale" in failure


def test_a_declaration_cannot_hide_an_invented_field(tmp_path: Path):
    """Precedence: banned field names are reported before any declaration."""
    declared_drift = dict(DRIFTED)
    declared_drift[EXPECTATION_KEY] = {
        "validation_status": "invalid",
        "error_contains": "agent.name must be a string",
        "why": "attempting to launder drift as a deliberate refusal",
    }
    failure = conformance_failure(_write(tmp_path / "t/agent/trajectory.json", declared_drift))
    assert failure is not None and "no ATIF document has" in failure
