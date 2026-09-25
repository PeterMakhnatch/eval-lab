"""Known-effect control isolation for the HAR-72 calibration driver.

The known-effect condition measures gate power by restoring the original tutorial
``answer-style`` skill over a deliberately degraded current one. These tests pin the
consumer-visible isolation edges that keep that contrast valid, without starting Reef,
Ollama, or any service:

* every trial runs in its own scenario, and all scenarios are named before the first
  trial publishes (a Reef scenario created later forks the shared head -- which earlier
  publishes advance -- instead of the degraded seed);
* each trial's proposal restores the seed's original skill text and carries no oracle
  answers;
* the degraded seed and the restored candidate share no mutable state.
"""

from __future__ import annotations

import copy
import json

from evallab_reef_gate import calibrate

MODEL = "qwen2.5:7b"
TRIALS = 5

TUTORIAL_SKILL_TEXT = (
    "# answer-style\n"
    "\n"
    "Starter skill. The evolution loop replaces this placeholder with\n"
    "concrete guidance learned from failing tasks.\n"
)


def seed_entries(skill_text: str = TUTORIAL_SKILL_TEXT) -> list:
    """A tutorial-shaped seed: one dotted tool reference plus the answer-style skill."""
    return [
        "reef.harness.runners.native.seed:SEED_NODES",
        {
            "id": "answer-style",
            "name": "skill",
            "config": {"name": "answer-style", "text": skill_text},
        },
    ]


def answer_style(entries: list) -> dict:
    return next(entry for entry in entries if isinstance(entry, dict) and entry.get("id") == "answer-style")


class _FakeReef:
    """The driver-visible slice of ReefClient: scenario naming, proposals, settled rows."""

    def __init__(self) -> None:
        self.events: list = []
        self.proposals: dict[str, dict] = {}
        self._rows: dict[str, list[dict]] = {}

    def inference_with_record(self, scenario: str, path: str, payload: dict):
        self.events.append(("inference", scenario))
        return ({"ok": True}, f"receipt-{scenario}-{len(self.events)}")


    def report(self, scenario: str, payload: dict, references=None) -> None:
        self.events.append(("report", scenario))

    def get(self, path: str, extra_headers=None):
        scenario = (extra_headers or {}).get("x-reef-scenario")
        if path == "/reef/harness":
            return {"release_id": f"head-{scenario}", "content_id": "content", "files": {"tree": "v1"}}
        if path == "/reef/harness/releases":
            return {"releases": list(self._rows.get(scenario, []))}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, scenario: str, payload: dict):
        assert path == "/reef/harness/proposals"
        self.events.append(("propose", scenario))
        self.proposals[scenario] = payload
        published = scenario.endswith("-01")
        self._rows.setdefault(scenario, []).append(
            {
                "release_id": f"row-{scenario}",
                "operation": "training",
                "metrics": {
                    "published": published,
                    "skipped": None,
                    "wins": 8 if published else 2,
                    "losses": 0 if published else 3,
                    "ties": 7 if published else 10,
                },
            }
        )
        return ({"admitted": True, "proposal_id": f"proposal-{scenario}"}, None)


def test_trial_scenarios_are_distinct_per_trial_and_shared_for_aa() -> None:
    scenarios = [calibrate.trial_scenario("known-effect", index) for index in range(1, TRIALS + 1)]
    assert len(set(scenarios)) == TRIALS
    assert all(scenario.startswith(calibrate.KNOWN_EFFECT_SCENARIO_PREFIX + "-") for scenario in scenarios)
    assert [calibrate.trial_scenario("aa", index) for index in range(1, TRIALS + 1)] == [
        calibrate.SCENARIO_AA
    ] * TRIALS


def test_campaign_names_every_scenario_before_first_proposal() -> None:
    """The fixed campaign order: all trial scenarios exist before any trial can publish.

    Trial 1 publishes here, so any scenario named after it would fork the published
    (restored) tree instead of the degraded seed. The driver runs the same two calls
    ``main`` runs -- name everything, then run each trial in its own scenario.
    """
    degraded, original = calibrate.degrade_seed_entries(seed_entries())
    current = answer_style(degraded)
    client = _FakeReef()
    scenarios = calibrate.name_known_effect_scenarios(client, MODEL, TRIALS)
    assert scenarios == [calibrate.trial_scenario("known-effect", index) for index in range(1, TRIALS + 1)]
    for index in range(1, TRIALS + 1):
        result = calibrate.run_trial(
            client,
            model=MODEL,
            scenario=scenarios[index - 1],
            index=index,
            condition="known-effect",
            current_entry=current,
            candidate_entry=original,
        )
        assert result["status"] == "settled"
    inferences = [(position, event[1]) for position, event in enumerate(client.events) if event[0] == "inference"]
    proposes = [position for position, event in enumerate(client.events) if event[0] == "propose"]
    assert len(proposes) == TRIALS
    # The first inferences are the pre-creation namings -- one per scenario, in trial order --
    # and they all precede the first proposal (later inferences are per-trial triggers).
    assert [scenario for _, scenario in inferences[:TRIALS]] == scenarios
    assert max(position for position, _ in inferences[:TRIALS]) < min(proposes)
    assert [event[1] for event in client.events if event[0] == "propose"] == scenarios


def test_proposal_restores_original_skill_without_oracle_answers() -> None:
    degraded, original = calibrate.degrade_seed_entries(seed_entries())
    current = answer_style(degraded)
    assert current["config"]["text"] == calibrate.DEGRADED_ANSWER_STYLE_TEXT
    assert original["config"]["text"] == TUTORIAL_SKILL_TEXT
    client = _FakeReef()
    calibrate.name_known_effect_scenarios(client, MODEL, 1)
    result = calibrate.run_trial(
        client,
        model=MODEL,
        scenario=calibrate.trial_scenario("known-effect", 1),
        index=1,
        condition="known-effect",
        current_entry=current,
        candidate_entry=original,
    )
    assert result["status"] == "settled" and result["published"] is True
    (mutation,) = client.proposals[calibrate.trial_scenario("known-effect", 1)]["mutations"]
    assert mutation["id"] == "answer-style"
    assert mutation["options"]["config"]["text"] == TUTORIAL_SKILL_TEXT
    assert mutation["options"]["config"]["text"] != current["config"]["text"]
    blob = json.dumps(client.proposals)
    assert not any(answer in blob for answer in calibrate.ANSWERS.values())


def test_degraded_seed_shares_no_mutable_state_with_candidate() -> None:
    entries = seed_entries()
    snapshot = copy.deepcopy(entries)
    degraded, original = calibrate.degrade_seed_entries(entries)
    assert entries == snapshot
    skill = answer_style(degraded)
    assert skill is not original and skill["config"] is not original["config"]
    skill["config"]["text"] = "mutated"
    skill["config"]["note"] = ["mutable"]
    assert original["config"]["text"] == TUTORIAL_SKILL_TEXT
    assert "note" not in original["config"]
    original["config"]["text"] = "mutated back"
    assert skill["config"]["text"] == "mutated"
