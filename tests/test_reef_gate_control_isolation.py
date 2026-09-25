"""Known-effect control isolation for the HAR-72 calibration driver.

The known-effect condition measures gate power by restoring the original tutorial
``answer-style`` skill over a deliberately degraded current one. That contrast is only
valid if every trial's current tree still carries the degraded seed when its step
settles. Reef creates a fresh scenario by forking the shared head, and every published
commit advances that head -- so a scenario created after an earlier trial published
starts from the restored tree instead of the degraded seed.

These tests run the real driver against a stateful simulated Reef that models exactly
those two rules (creation snapshots the shared head; publication advances the scenario
tree and the shared head): ``main()`` end to end, and the pre-fix per-trial creation
order through the real ``run_trial``. No services, models, or project-wide suites.
"""

from __future__ import annotations

import copy
import json
import sys
import types

import pytest
import yaml
from evallab_reef_gate import calibrate

MODEL = "qwen2.5:7b"
TRIALS = 5
REEF_COMMIT = "818997d76412f0eead7d0b4b343da701d6ce2c20"

TUTORIAL_SKILL_TEXT = (
    "# answer-style\n"
    "\n"
    "Starter skill. The evolution loop replaces this placeholder with\n"
    "concrete guidance learned from failing tasks.\n"
)

TASKS = [
    "[sieve] Write and mentally run a prime sieve: how many primes are below 100000?",
    "[fib] With fib(1) = 1 and fib(2) = 1, compute fib(90) exactly.",
    "[csv] Compute the median of the value column in this csv.",
]


def _seed() -> list:
    """A tutorial-shaped seed: one dotted tool reference plus the answer-style skill."""
    return [
        "reef.harness.runners.native.seed:SEED_NODES",
        {
            "id": "answer-style",
            "name": "skill",
            "config": {"name": "answer-style", "text": TUTORIAL_SKILL_TEXT},
        },
    ]


def _answer_style(entries: list) -> dict:
    return next(entry for entry in entries if isinstance(entry, dict) and entry.get("id") == "answer-style")


class _SimulatedReef:
    """Reef's scenario/tree/head rules, offline.

    Creating a scenario snapshots the shared head as its current tree; settling a step
    whose proposal restores the tutorial skill over a degraded current publishes, which
    advances that scenario's tree and the shared head. Reads never create scenarios.
    """

    def __init__(self) -> None:
        self.shared_head = calibrate.DEGRADED_ANSWER_STYLE_TEXT
        self.trees: dict[str, str] = {}
        self.pending: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}
        self.proposals: dict[str, dict] = {}
        self.settled_current: dict[str, str] = {}

    def inference_with_record(self, scenario: str, path: str, payload: dict):
        if scenario not in self.trees:
            self.trees[scenario] = self.shared_head
        return ({"ok": True}, f"receipt-{scenario}-{len(self.trees)}")

    def report(self, scenario: str, payload: dict, references=None) -> None:
        if scenario not in self.trees:
            self.trees[scenario] = self.shared_head

    def get(self, path: str, extra_headers=None):
        scenario = (extra_headers or {}).get("x-reef-scenario")
        assert scenario in self.trees, f"unknown scenario {scenario!r}: reads never create"
        if path == "/reef/harness":
            return {"release_id": f"head-{scenario}", "content_id": "content", "files": {"tree": "v1"}}
        if path == "/reef/harness/releases":
            self._settle(scenario)
            return {"releases": list(self.rows.get(scenario, []))}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, scenario: str, payload: dict):
        assert path == "/reef/harness/proposals"
        assert scenario in self.trees, "proposals never create scenarios"
        self.pending[scenario] = payload
        self.proposals[scenario] = payload
        return ({"admitted": True, "proposal_id": f"proposal-{scenario}"}, None)

    def _candidate_text(self, payload: dict) -> str:
        for mutation in payload.get("mutations", []):
            if mutation.get("id") == "answer-style":
                return mutation["options"]["config"]["text"]
        raise AssertionError("proposal carries no answer-style mutation")

    def _settle(self, scenario: str) -> None:
        if scenario not in self.pending or scenario in self.rows:
            return
        candidate = self._candidate_text(self.pending.pop(scenario))
        current = self.trees[scenario]
        self.settled_current[scenario] = current
        published = (
            candidate == TUTORIAL_SKILL_TEXT and current == calibrate.DEGRADED_ANSWER_STYLE_TEXT
        )
        pairs = TRIALS * len(TASKS)
        if published:
            self.trees[scenario] = candidate
            self.shared_head = candidate
            candidate_scores, current_scores, wlt = [1.0] * pairs, [0.0] * pairs, (12, 0, 3)
        else:
            candidate_scores, current_scores, wlt = [1.0] * pairs, [1.0] * pairs, (0, 0, 15)
        wins, losses, ties = wlt
        self.rows[scenario] = [
            {
                "release_id": f"row-{scenario}",
                "operation": "training",
                "metrics": {
                    "published": published,
                    "skipped": None,
                    "wins": wins,
                    "losses": losses,
                    "ties": ties,
                    "candidate_scores": candidate_scores,
                    "current_scores": current_scores,
                },
            }
        ]


class _DummyServer:
    def terminate(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return 0


def _gguf_entry() -> dict:
    return {
        "name": MODEL,
        "model": MODEL,
        "digest": "sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
        "size": 4_700_000_000,
        "details": {"format": "gguf"},
    }


def _tutorial_serve_config() -> dict:
    return {
        "schema-version": 2,
        "reef": {"host": "127.0.0.1", "port": 8900, "token": "reef-local", "run-dir": "work/stack"},
        "recipe": {
            "implementation": "reef.recipe.cordis:CordisRecipe",
            "config": {
                "evolution": {
                    "adapter": "native",
                    "propose": "tutorial:propose",
                    "evaluate": "tutorial:evaluate",
                    "tasks": list(TASKS),
                    "seed": _seed(),
                }
            },
        },
        "inference": {"upstream-url": "http://127.0.0.1:11461", "upstream-model": MODEL},
        "execution": {"evolution": {"workers": 1}},
        "executors": {},
    }


@pytest.fixture
def offline_reef_root(tmp_path, monkeypatch):
    """A fabricated Reef checkout: the tutorial serve config plus what preflight checks."""
    root = tmp_path / "reef"
    (root / "reef").mkdir(parents=True)
    config_path = root / calibrate.SOURCE_CONFIG_RELATIVE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(_tutorial_serve_config(), sort_keys=False))
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    monkeypatch.setattr(
        calibrate, "fetch_local_inventory", lambda *args, **kwargs: {"models": [_gguf_entry()]}
    )
    monkeypatch.setattr(calibrate, "reef_checkout_commit", lambda *args, **kwargs: REEF_COMMIT)
    monkeypatch.setattr(calibrate, "start_reef", lambda *args, **kwargs: _DummyServer())
    return root, python


def test_main_keeps_every_trial_current_degraded(tmp_path, monkeypatch, offline_reef_root) -> None:
    """The real ``main()`` campaign: all five trials settle published with a degraded current.

    Trial 1 publishes, which moves the shared head to the restored skill -- yet every
    later trial's step still settles against the degraded seed, and every proposal
    restores the tutorial text. Under the pre-fix order (a scenario created just before
    its trial) trials 2-5 would fork the published tree instead.
    """
    reef_root, python = offline_reef_root
    sim = _SimulatedReef()
    monkeypatch.setitem(
        sys.modules, "reef_client", types.SimpleNamespace(ReefClient=lambda *args, **kwargs: sim)
    )
    # End-of-run reporting is outside this isolation test's scope (it reads run-meta keys
    # owned by other calibrate.py areas); the campaign itself -- trials plus results.jsonl -- is real.
    monkeypatch.setattr(calibrate, "analyze", lambda work: {})
    monkeypatch.setattr(calibrate, "print_summary", lambda summary: None)
    work = tmp_path / "known-effect"
    assert calibrate.main(
        [
            "--work-dir", str(work),
            "--reef-root", str(reef_root),
            "--python", str(python),
            "--ollama-url", "http://127.0.0.1:11461",
            "--condition", "known-effect",
            "--trials", str(TRIALS),
            "--repeats", "5",
            "--port", "18973",
            "--model", MODEL,
        ]
    ) is None

    rows = [json.loads(line) for line in (work / "results.jsonl").read_text().splitlines()]
    assert len(rows) == TRIALS
    assert all(row["status"] == "settled" and row["published"] is True for row in rows)
    assert len({row["scenario"] for row in rows}) == TRIALS
    assert len(sim.settled_current) == TRIALS
    assert all(tree == calibrate.DEGRADED_ANSWER_STYLE_TEXT for tree in sim.settled_current.values())
    for payload in sim.proposals.values():
        (mutation,) = payload["mutations"]
        assert mutation["options"]["config"]["text"] == TUTORIAL_SKILL_TEXT
    assert sim.shared_head == TUTORIAL_SKILL_TEXT
    assert calibrate.DEGRADED_ANSWER_STYLE_TEXT != TUTORIAL_SKILL_TEXT
    assert not any(answer in calibrate.DEGRADED_ANSWER_STYLE_TEXT for answer in calibrate.ANSWERS.values())


def test_creating_scenarios_after_a_publish_loses_the_contrast() -> None:
    """The pre-fix order through the real trial functions: trial 1 publishes, and the
    scenario created next forks the restored tree, so trial 2 settles with no contrast
    and does not publish. The ``main()`` test above fails under this order."""
    degraded, original = calibrate.degrade_seed_entries(_seed())
    current = _answer_style(degraded)
    sim = _SimulatedReef()
    results = []
    for index in (1, 2):
        scenario = calibrate.trial_scenario("known-effect", index)
        calibrate.name_scenario(sim, MODEL, scenario)
        results.append(
            calibrate.run_trial(
                sim,
                model=MODEL,
                scenario=scenario,
                index=index,
                condition="known-effect",
                current_entry=current,
                candidate_entry=original,
            )
        )
    assert all(result["status"] == "settled" for result in results)
    first, second = (results[0]["scenario"], results[1]["scenario"])
    assert results[0]["published"] is True
    assert sim.settled_current[first] == calibrate.DEGRADED_ANSWER_STYLE_TEXT
    assert sim.settled_current[second] == TUTORIAL_SKILL_TEXT
    assert results[1]["published"] is False


def test_degraded_seed_shares_no_mutable_state_with_candidate() -> None:
    entries = _seed()
    snapshot = copy.deepcopy(entries)
    degraded, original = calibrate.degrade_seed_entries(entries)
    assert entries == snapshot
    skill = _answer_style(degraded)
    assert skill is not original and skill["config"] is not original["config"]
    skill["config"]["text"] = "mutated"
    skill["config"]["note"] = ["mutable"]
    assert original["config"]["text"] == TUTORIAL_SKILL_TEXT
    assert "note" not in original["config"]
    original["config"]["text"] = "mutated back"
    assert skill["config"]["text"] == "mutated"
