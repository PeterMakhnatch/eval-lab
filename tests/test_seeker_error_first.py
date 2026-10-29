"""SB-1 error-first campaign driver: no network, no Modal, no real OMP, no paid call.

Fake OMP runner, fake grader and fake retriever are injected through the driver's own
seams; the real prompt builders, attestation, replay verification and retained-cell
predicate run unchanged.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.production import seeker_error_first as sef
from evallab.production.cloud_trace_retriever import RetrievedTraceHit, TraceRetrievalResult
from evallab.production.qc_reports import _ts4_outcome
from evallab.production.seeker_worker import SeekerRefusalError
from evallab.registry import task_directory_digest

ROUTE = "anthropic/claude-sonnet-5-5"
ROUTE_B = "kimi-code/k3.1-pro-highspeed"
DATASET = "tb4-audit-v1"
HIDDEN = "HIDDEN_TEST_SOURCE_MARKER"
ORIGINAL_MARKER = "ORIGINAL_RUNNER_MARKER"
REPAIRED_MARKER = "REPAIRED_ONLY_MARKER"
TARGET_CHECK = "tests/test_outputs.py::test_output"
ENVIRONMENT = {
    "PATH": "/usr/bin",
    "ANTHROPIC_API_KEY": "paid-a",
    "OPENROUTER_API_KEY": "paid-b",
    "OPENAI_API_KEY": "paid-c",
    "MODAL_TOKEN_ID": "modal-id",
    "MODAL_TOKEN_SECRET": "modal-secret",
}


# ----------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def isolate_sealed_source_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic DEV fixtures must never inspect real sealed-split sources."""
    monkeypatch.setattr(sef, "find_tb4_test_leaks", lambda *_args, **_kwargs: [])


def _write_task(root: Path, name: str, *, repaired: bool) -> Path:
    task = root / name
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "solution").mkdir()
    (task / "instruction.md").write_text(f"Write the checksum for {name}.\n")
    (task / "task.toml").write_text('[task]\nname = "x"\n[verifier]\ntimeout_sec = 60\n')
    (task / "environment/Dockerfile").write_text("FROM python:3.12\n")
    (task / "tests/test_outputs.py").write_text(f"# {HIDDEN}\ndef test_output():\n    assert 1\n")
    (task / "solution/solve.sh").write_text("echo reference\n")
    if repaired:
        (task / "tests/extra.py").write_text("print('hardened')\n")
    return task


def _cohort_payload(tmp_path: Path, names: Sequence[str], *, reservation: float = 0.25) -> dict:
    tasks = []
    for name in names:
        original = _write_task(tmp_path / "orig", name, repaired=False)
        repaired = _write_task(tmp_path / "fixed", name, repaired=True)
        tasks.append(
            {
                "task_id": f"terminal-bench/{name}",
                "dataset_id": DATASET,
                "original_task": str(original),
                "repaired_task": str(repaired),
                "repaired_grader_kind": "package",
                "original_package_digest": task_directory_digest(original),
                "repaired_package_digest": task_directory_digest(repaired),
                "original_reservation_usd": reservation,
                "repaired_reservation_usd": reservation * 2,
            }
        )
    return {"schema": sef.COHORT_SCHEMA, "tasks": tasks}


def _ledger(tmp_path: Path, rows: Sequence[tuple[str, str, str]]) -> Path:
    path = tmp_path / "assignments.csv"
    lines = ["key_id,item_id,task,unit,split"]
    lines += [f"{key},{item},,,{split}" for key, item, split in rows]
    path.write_text("\n".join(lines) + "\n")
    return path


class FakeOmp:
    """Inert OMP subprocess: the only bytes treated as model output."""

    def __init__(
        self, script: Callable[[str, int, str], tuple[str, str, str | None, int]] | None = None
    ):
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.peak_total = 0
        self.delay = 0.0

    @staticmethod
    def reply(route: str, number: int) -> str:
        return json.dumps(
            {
                "files": {"solve.sh": f"#!/bin/bash\necho attempt-{number}\n"},
                "mechanism": "stub the checked output",
                "taxonomy_class": "weak_check",
                "target_check": TARGET_CHECK,
                "files_written": [],
            }
        )

    def __call__(
        self, argv: Sequence[str], timeout: float, environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        route = argv[list(argv).index("--model") + 1]
        with self.lock:
            number = len([c for c in self.calls if c["route"] == route]) + 1
            self.calls.append(
                {"route": route, "prompt": argv[-1], "env": dict(environment), "argv": list(argv)}
            )
            self.running[route] = self.running.get(route, 0) + 1
            self.peak[route] = max(self.peak.get(route, 0), self.running[route])
            self.peak_total = max(self.peak_total, sum(self.running.values()))
        try:
            if self.delay:
                time.sleep(self.delay)
            provider, _, model = route.partition("/")
            text: str | None = self.reply(route, number)
            code = 0
            if self.script is not None:
                provider, model, text, code = self.script(route, number, argv[-1])
                if provider == "" and model == "":
                    provider, _, model = route.partition("/")
            message = {
                "role": "assistant",
                "content": [{"type": "text", "text": text}] if text is not None else [],
                "provider": provider,
                "model": model,
                "usage": {
                    "input": 10, "output": 5, "cacheRead": 100, "cacheWrite": 0, "totalTokens": 115,
                },
            }
            stdout = (
                json.dumps({"type": "agent_start"})
                + "\n"
                + json.dumps({"type": "agent_end", "messages": [message]})
                + "\n"
            )
            return subprocess.CompletedProcess(list(argv), code, stdout, "")
        finally:
            with self.lock:
                self.running[route] -= 1

    def prompts(self, route: str) -> list[str]:
        return [call["prompt"] for call in self.calls if call["route"] == route]


class FakeGrader:
    """Inert replay of the native grade shape; rewards come from per-task scripts."""

    def __init__(self) -> None:
        self.scripts: dict[tuple[str, str], list[tuple[float | None, str]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def script(self, name: str, side: str, *results: tuple[float | None, str]) -> None:
        self.scripts[(name, side)] = list(results)

    def __call__(self, task: Path | str, *, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
        path = Path(task)
        side = "repaired" if "fixed" in path.parts else "original"
        with self.lock:
            queue = self.scripts.get((path.name, side), [])
            reward, status = queue.pop(0) if queue else (0.0, "measured")
            self.calls.append({"task": path.name, "side": side, "kwargs": kwargs})
        output = Path(output_dir)
        verifier = output / "trial" / "verifier"
        verifier.mkdir(parents=True)
        marker = ORIGINAL_MARKER if side == "original" else REPAIRED_MARKER
        log = verifier / "test-stdout.txt"
        failing = reward != 1.0
        log.write_text(
            "=== test session starts ===\n"
            + (f"FAILED {TARGET_CHECK} - AssertionError: expected 5 got 4\n" if failing else "")
            + f"{marker} verifier exit\n"
        )
        ctrf = verifier / "ctrf.json"
        ctrf.write_text(
            json.dumps(
                {
                    "results": {
                        "tests": [
                            {
                                "name": TARGET_CHECK,
                                "status": "failed" if failing else "passed",
                                "trace": f"AssertionError: expected 5 got 4 {marker}" if failing else "",
                            }
                        ]
                    }
                }
            )
        )
        return {
            "status": status,
            "reward": reward,
            "error": "grader exploded" if status == "error" else None,
            "executed": True,
            "trial_dir": str(output / "trial"),
            "output_dir": str(output),
            "per_test": {
                "tests": [{"id": TARGET_CHECK, "status": "failed" if failing else "passed"}],
                "sources": [str(ctrf)],
            },
            "logs": {
                "verifier": [
                    {
                        "path": str(log),
                        "digest": "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest(),
                        "bytes": log.stat().st_size,
                    }
                ]
            },
            "lifecycle": {"closure": {"sandboxes": [{"sandbox_id": "sb", "exit_code": 1}]}},
            "cost": {"native_spend": {"this_run_reserved_usd": 0.05}},
        }


class FakeRetriever:
    def __init__(self, *, hit_on: str | None = None) -> None:
        self.queries: list[tuple[str, str]] = []
        self.hit_on = hit_on

    def retrieve(self, task_id: str, query: str) -> TraceRetrievalResult:
        self.queries.append((task_id, query))
        hits: tuple[RetrievedTraceHit, ...] = ()
        if self.hit_on is not None and query == self.hit_on:
            hits = (
                RetrievedTraceHit(
                    task_id="other-dev-task",
                    defect_class="weak_check",
                    mechanism="RetrievalHitMarker stubbed output accepted",
                ),
            )
        return TraceRetrievalResult(hits=hits, receipt={"queries": len(self.queries)})


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        names: Sequence[str] = ("alpha",),
        *,
        models: Sequence[str] = (ROUTE,),
        ledger_rows: Sequence[tuple[str, str, str]] | None = None,
        workers: int = 2,
        per_model: int = 2,
        resume: bool = False,
    ) -> None:
        self.tmp = tmp_path
        self.names = tuple(names)
        self.cohort = sef.parse_cohort(_cohort_payload(tmp_path, names))
        rows = ledger_rows or [("keyA", f"terminal-bench/{name}", "dev") for name in names]
        self.csv = _ledger(tmp_path, rows)
        self.out = tmp_path / "out"
        self.omp = FakeOmp()
        self.grader = FakeGrader()
        self.retriever = FakeRetriever()
        self.spend_path = tmp_path / "spend.jsonl"
        self.config = sef.CampaignConfig(
            cohort=self.cohort,
            models=tuple(models),
            out=self.out,
            workers=workers,
            per_model_concurrency=per_model,
            resume=resume,
            assignments_csv=self.csv,
        )
        self.approval = sef.ExecutionApproval.require(
            approval="Peter approved the SB-1 test spend",
            max_usd=5.0,
            spend_ledger=tmp_path / "native-ledger.json",
            modal_app_name="sb1-test-app",
        )

    def factory(self, route: str) -> Any:
        allow = frozenset(task.task_id for task in self.cohort.tasks)
        return sef.PromptFileHeadlessGenerator(
            route, runner=self.omp, environment=ENVIRONMENT, dev_task_allowlist=allow
        )

    def run(self, **overrides: Any) -> dict[str, Any]:
        config = overrides.pop("config", self.config)
        return sef.run_campaign(
            config,
            self.approval,
            retriever=overrides.pop("retriever", self.retriever),
            generator_factory=self.factory,
            grader_fn=self.grader,
            spend_log=sef.SpendLog(self.spend_path),
        )

    def cell(self, name: str, route: str = ROUTE) -> Path:
        return self.out / DATASET / name / sef.model_slug(route)

    def result(self, name: str, turn: int, route: str = ROUTE) -> dict[str, Any]:
        return json.loads((self.cell(name, route) / f"turn-{turn}" / "result.json").read_text())


def _confirmed(harness: Harness, name: str) -> None:
    """Turn 1 fails upstream (feeds turn 2); turn 2 is a confirmed differential."""
    harness.grader.script(name, "original", (0.0, "measured"), (1.0, "measured"))
    harness.grader.script(name, "repaired", (0.0, "measured"))


# ------------------------------------------------------------------------- cohort


def test_cohort_loader_types_and_rejections(tmp_path: Path) -> None:
    payload = _cohort_payload(tmp_path, ["alpha", "beta"])
    cohort = sef.parse_cohort(payload)
    first = cohort.tasks[0]
    assert (first.name, first.dataset_id, first.repaired_grader_kind) == ("alpha", DATASET, "package")
    assert first.pair_reservation_usd == pytest.approx(0.75)
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(payload))
    assert sef.load_cohort(path).tasks == cohort.tasks

    def broken(**change: Any) -> dict[str, Any]:
        tasks = [dict(task) for task in payload["tasks"]]
        tasks[0].update(change)
        return {**payload, "tasks": tasks}

    for bad in (
        {**payload, "schema": "wrong/v1"},
        {**payload, "tasks": []},
        broken(original_task="relative/path"),
        broken(original_package_digest="sha256:abc"),
        broken(repaired_grader_kind="adapter"),
        broken(original_reservation_usd=-1),
        broken(repaired_reservation_usd="free"),
        {**payload, "tasks": [payload["tasks"][0], payload["tasks"][0]]},
    ):
        with pytest.raises(sef.CohortError):
            sef.parse_cohort(bad)


# -------------------------------------------------------------------- core protocol


def test_turn2_prompt_places_runner_error_before_retrieval(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.retriever = FakeRetriever(hit_on="weak_check test_outputs")
    _confirmed(harness, "alpha")
    receipt = harness.run()
    assert receipt["ok"]

    first, second = harness.omp.prompts(ROUTE)
    assert sef._FEEDBACK_HEADING not in first
    assert "RetrievalHitMarker" not in first
    assert "Matched Hackbook Cues" not in first and "Primary Hackbook hypothesis" not in first
    assert second.startswith(sef._FEEDBACK_HEADING)
    runner_at = second.index(ORIGINAL_MARKER)
    retrieval_at = second.index("RetrievalHitMarker")
    assert runner_at < second.index("## File: `task.toml`") < retrieval_at
    assert "AssertionError: expected 5 got 4" in second
    assert "sandbox exit codes: 1" in second
    # The failing node name survives; the test file path and hidden source do not.
    assert "test_output" in second and "tests/test_outputs.py" not in second
    assert HIDDEN not in first + second
    assert "[verifier]" not in first + second
    # Only the ORIGINAL grade feeds back; the repaired grade never does.
    assert REPAIRED_MARKER not in first + second

    one, two = harness.result("alpha", 1), harness.result("alpha", 2)
    block = (harness.cell("alpha") / "turn-2/feedback.md").read_text()
    assert two["feedback_source"] == "runner_error"
    assert two["feedback_block_sha256"] == hashlib.sha256(block.encode()).hexdigest()
    assert one["runner_error_sha256"] == two["runner_error_sha256"] is not None
    assert len(block) <= sef.MAX_FEEDBACK_CHARS
    assert two["retrieval_hits"] == 1 and two["retrieval_query"] == "weak_check test_outputs"
    assert (harness.cell("alpha") / "turn-2/prompt.md").read_text() == second


def test_retained_cell_layout_and_qc_predicate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _confirmed(harness, "alpha")
    harness.run()
    turn_1 = harness.cell("alpha") / "turn-1"
    for name in ("prompt.md", "response.jsonl", "result.json", "submission/solve.sh"):
        assert (turn_1 / name).exists(), name
    one, two = harness.result("alpha", 1), harness.result("alpha", 2)
    assert (one["original_reward"], one["repaired_reward"]) == (0.0, None)
    assert one["verified_differential"] is False and one["outcome"] == "upstream_failed"
    assert (two["original_reward"], two["repaired_reward"]) == (1.0, 0.0)
    assert two["verified_differential"] is True and two["outcome"] == "confirmed_differential"
    for cell in (one, two):
        assert cell["attestation_ok"] is True and cell["hide_tests"] is True
        assert cell["requested_model_route"] == ROUTE
        assert (cell["served_provider"], cell["served_model"]) == ("anthropic", "claude-sonnet-5-5")
        assert cell["llm_solve_sha256"] == cell["executed_solve_sha256"]
        assert cell["input_tokens"] == 110 and cell["output_tokens"] == 5
    stdout = (harness.cell("alpha") / "turn-2/response.jsonl").read_bytes()
    assert hashlib.sha256(stdout).hexdigest() == two["llm_stdout_sha256"]
    assert _ts4_outcome(two, ROUTE) == (True, True)
    assert _ts4_outcome(one, ROUTE) == (True, False)
    summary = json.loads((harness.out / "summary.json").read_text())
    paired = summary["models"][0]["paired"]
    assert (paired["denominator"], paired["turn_1_executed_differentials"]) == (1, 0)
    assert paired["turn_2_executed_differentials"] == 1 and paired["turn_2_only_tasks"] == [
        f"{DATASET}/alpha"
    ]


def test_grader_policy_is_modal_only_with_explicit_authority(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.run()
    assert harness.grader.calls
    for call in harness.grader.calls:
        kwargs = call["kwargs"]
        assert kwargs["backend"] == "modal" and kwargs["allow_local_docker"] is False
        assert kwargs["approval"] == "Peter approved the SB-1 test spend"
        assert kwargs["max_usd"] == 5.0 and kwargs["modal_app_name"] == "sb1-test-app"
        assert Path(kwargs["spend_ledger"]).name == "native-ledger.json"
    import os

    assert os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"


def test_omp_call_strips_paid_keys_and_never_falls_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path, models=(ROUTE,))
    harness.run()
    assert harness.omp.calls
    for call in harness.omp.calls:
        assert call["env"] == {"PATH": "/usr/bin"}
        argv = call["argv"]
        assert "--no-tools" in argv and argv.count("--model") == 1
        assert argv[argv.index("--model") + 1] == ROUTE
    assert sef.strip_paid_api_keys(ENVIRONMENT) == {"PATH": "/usr/bin"}


def test_fallback_served_response_is_not_attributed_to_requested_model(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.omp.script = lambda route, number, prompt: (
        "meta", "muse-wonder-1.0-contributor", harness.omp.reply(route, number), 0,
    )
    harness.run()
    one, two = harness.result("alpha", 1), harness.result("alpha", 2)
    assert one["attestation_ok"] is False and one["outcome"] == "served_route_mismatch"
    assert (one["served_provider"], one["served_model"]) == ("meta", "muse-wonder-1.0-contributor")
    assert one["requested_model_route"] == ROUTE and one["original_reward"] is None
    assert two["outcome"] == "not_evaluated_no_runner_error"
    assert harness.grader.calls == []  # a fallback answer is never graded under this route
    assert len(harness.omp.calls) == 1  # and turn 2 has no runner error to feed back
    assert _ts4_outcome(one, ROUTE) == (False, False)
    status, _ = sef.read_turn(harness.cell("alpha") / "turn-1", ROUTE, 1)
    assert status == "unevaluated"
    paired = json.loads((harness.out / "summary.json").read_text())["models"][0]
    assert paired["paired"]["denominator"] == 0 and paired["attested_turn_1"] == 0
    assert paired["unevaluated_cells"][0]["turn_1_outcome"] == "served_route_mismatch"


@pytest.mark.parametrize(
    "original,repaired,expected",
    [
        ([(None, "error")], [], "original_grader_unavailable"),
        ([(1.0, "measured")], [(None, "error")], "repaired_grader_unverified"),
        ([(1.0, "refused")], [], "original_grader_unavailable"),
    ],
)
def test_non_measured_grades_never_count_as_differentials(
    tmp_path: Path,
    original: list[tuple[float | None, str]],
    repaired: list[tuple[float | None, str]],
    expected: str,
) -> None:
    harness = Harness(tmp_path)
    harness.grader.script("alpha", "original", *original, *original)
    harness.grader.script("alpha", "repaired", *repaired, *repaired)
    harness.run()
    one = harness.result("alpha", 1)
    assert one["outcome"] == expected and one["verified_differential"] is False
    assert sef.result_is_evaluated(one) is False
    summary = json.loads((harness.out / "summary.json").read_text())["models"][0]
    assert summary["paired"]["denominator"] == 0
    assert summary["differential_tasks_turn_1"] == [] == summary["differential_tasks_turn_2"]
    assert summary["unevaluated_cells"]


class _ExitCodeGrader(FakeGrader):
    """Fake grader that records the trial exit code the native runner would write."""

    def __init__(self, exit_code: int) -> None:
        super().__init__()
        self.exit_code = exit_code

    def __call__(self, task: Path | str, *, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
        result = super().__call__(task, output_dir=output_dir, **kwargs)
        agent = Path(output_dir) / "trial" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "exit-code.txt").write_text(f"{self.exit_code}\n", encoding="utf-8")
        return result


def test_a_nonzero_candidate_exit_is_not_an_unavailable_grader(tmp_path: Path) -> None:
    # The grader ran and the submitted script died before any assertion: a model
    # failure, not infrastructure, so it must not inflate the unavailable bucket.
    harness = Harness(tmp_path)
    harness.grader = _ExitCodeGrader(1)
    harness.grader.script("alpha", "original", (None, "error"), (None, "error"))
    harness.run()
    one = harness.result("alpha", 1)
    assert one["outcome"] == "candidate_failed"
    assert one["failure_reason"].startswith("candidate solution exited 1;")
    assert one["verified_differential"] is False
    assert sef.result_is_evaluated(one) is False
    assert one["original_reward"] is None and one["repaired_status"] == "not_run"


def test_a_zero_or_absent_candidate_exit_stays_an_unavailable_grader(tmp_path: Path) -> None:
    for exit_code in (0, None):
        harness = Harness(tmp_path / f"case-{exit_code}")
        if exit_code is not None:
            harness.grader = _ExitCodeGrader(exit_code)
        harness.grader.script("alpha", "original", (None, "error"), (None, "error"))
        harness.run()
        one = harness.result("alpha", 1)
        assert one["outcome"] == "original_grader_unavailable"
        assert sef.result_is_evaluated(one) is False


def test_measured_double_pass_is_evaluated_but_not_a_differential(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.grader.script("alpha", "original", (1.0, "measured"), (1.0, "measured"))
    harness.grader.script("alpha", "repaired", (1.0, "measured"), (1.0, "measured"))
    harness.run()
    one = harness.result("alpha", 1)
    assert one["outcome"] == "repaired_grader_also_passed" and sef.result_is_evaluated(one)
    assert one["verified_differential"] is False
    summary = json.loads((harness.out / "summary.json").read_text())["models"][0]["paired"]
    assert summary["denominator"] == 1 and summary["turn_1_executed_differentials"] == 0


def test_tampered_response_invalidates_the_cell(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _confirmed(harness, "alpha")
    harness.run()
    response = harness.cell("alpha") / "turn-2/response.jsonl"
    response.write_text(response.read_text() + " ")
    assert sef.read_turn(harness.cell("alpha") / "turn-2", ROUTE, 2)[0] == "invalid"
    summary = sef.summarize_campaign(harness.config)["models"][0]
    assert summary["paired"]["denominator"] == 0 and summary["differential_tasks_turn_2"] == []


# ----------------------------------------------------------------------- gates


def test_plan_makes_no_model_call_and_no_grader_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ("alpha", "beta"), models=(ROUTE, ROUTE_B))

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("plan must not call a model or a grader")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sef, "run_candidate", forbidden)
    monkeypatch.delenv("EVALLAB_DISABLE_LOCAL_DOCKER", raising=False)
    receipt, code = sef.run_command(
        cohort=_write_cohort_file(tmp_path, harness),
        out=harness.out,
        models=(ROUTE, ROUTE_B),
        workers=6,
        per_model_concurrency=2,
        resume=False,
        mode="plan",
        assignments_csv=harness.csv,
    )
    assert code == 0 and receipt["ok"]
    assert (receipt["cells"], receipt["model_prompts"], receipt["grader_runs"]) == (4, 8, 16)
    assert receipt["model_prompts_per_model"] == {ROUTE: 4, ROUTE_B: 4}
    # models x tasks x 2 turns x (original 0.25 + repaired 0.5)
    assert receipt["modal_cost_upper_bound_usd"] == pytest.approx(2 * 2 * 2 * 0.75)
    assert (receipt["model_calls_made"], receipt["grader_runs_made"]) == (0, 0)
    on_disk = json.loads((harness.out / "plan.json").read_text())
    assert on_disk["cells"] == 4 and on_disk["mode"] == "plan"
    import os

    assert os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"
    assert not any(harness.out.glob("*/*/*/turn-*"))


def _write_cohort_file(tmp_path: Path, harness: Harness) -> Path:
    path = tmp_path / "cohort.json"
    path.write_text(
        json.dumps(
            {
                "schema": sef.COHORT_SCHEMA,
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "dataset_id": task.dataset_id,
                        "original_task": str(task.original_task),
                        "repaired_task": str(task.repaired_task),
                        "repaired_grader_kind": task.repaired_grader_kind,
                        "original_package_digest": task.original_package_digest,
                        "repaired_package_digest": task.repaired_package_digest,
                        "original_reservation_usd": task.original_reservation_usd,
                        "repaired_reservation_usd": task.repaired_reservation_usd,
                    }
                    for task in harness.cohort.tasks
                ],
            }
        )
    )
    return path


@pytest.mark.parametrize(
    "kwargs,missing",
    [
        ({}, ["--approval", "--max-usd", "--spend-ledger", "--modal-app-name"]),
        ({"approval": "ok", "max_usd": 1.0}, ["--spend-ledger", "--modal-app-name"]),
        ({"approval": "  ", "max_usd": 1.0, "modal_app_name": "app"}, ["--approval", "--spend-ledger"]),
    ],
)
def test_execute_without_explicit_authority_refuses_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], missing: list[str]
) -> None:
    harness = Harness(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("refusal must precede every call")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sef, "run_candidate", forbidden)
    with pytest.raises(SeekerRefusalError) as refusal:
        sef.run_command(
            cohort=_write_cohort_file(tmp_path, harness),
            out=harness.out,
            models=(ROUTE,),
            workers=2,
            per_model_concurrency=1,
            resume=False,
            mode="execute",
            assignments_csv=harness.csv,
            cloud_trace_db=tmp_path / "db.sqlite",
            **kwargs,
        )
    for flag in missing:
        assert flag in str(refusal.value)
    assert not harness.out.exists()


def test_execute_needs_retrieval_database(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(SeekerRefusalError, match="cloud-trace-db"):
        sef.run_command(
            cohort=_write_cohort_file(tmp_path, harness),
            out=harness.out,
            models=(ROUTE,),
            workers=2,
            per_model_concurrency=1,
            resume=False,
            mode="execute",
            assignments_csv=harness.csv,
            approval="ok",
            max_usd=1.0,
            spend_ledger=tmp_path / "ledger.json",
            modal_app_name="app",
        )
    assert not harness.out.exists()


@pytest.mark.parametrize(
    "ledger_rows,fragment",
    [
        (
            [("keyA", "terminal-bench/alpha", "dev"), ("keyB", "terminal-bench/alpha", "test")],
            "non-DEV",
        ),
        ([("keyA", "terminal-bench/alpha", "heldout")], "non-DEV"),
        ([("keyA", "terminal-bench/unrelated", "dev")], "no affirmative DEV"),
    ],
)
def test_cross_key_violation_refuses_before_any_call(
    tmp_path: Path, ledger_rows: list[tuple[str, str, str]], fragment: str
) -> None:
    harness = Harness(tmp_path, ("alpha", "beta"), ledger_rows=[
        *ledger_rows, ("keyA", "terminal-bench/beta", "dev"),
    ])
    plan = sef.plan_campaign(harness.config)
    assert plan["ok"] is False
    assert fragment in json.dumps(plan["refused_tasks"]["terminal-bench/alpha"])
    assert "terminal-bench/beta" not in plan["refused_tasks"]
    with pytest.raises(SeekerRefusalError, match="preflight refused"):
        harness.run()
    assert harness.omp.calls == [] and harness.grader.calls == []
    assert not harness.out.exists()
    assert not harness.spend_path.exists()


def test_ledger_digest_drift_refuses(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    drifted = sef.CampaignConfig(
        cohort=sef.Cohort(harness.cohort.tasks, assignments_csv_sha256="0" * 64),
        models=(ROUTE,),
        out=harness.out,
        assignments_csv=harness.csv,
    )
    assert sef.plan_campaign(drifted)["ok"] is False
    with pytest.raises(SeekerRefusalError, match="ledger digest"):
        harness.run(config=drifted)
    assert harness.omp.calls == []


def test_committed_cohort_ledger_digest_matches_committed_ledger(tmp_path: Path) -> None:
    # The cohort builder writes the ledger digest as ``sha256:<hex>``; the plan must
    # compare it to the ledger's bare hex digest instead of calling every real cohort drifted.
    repo = Path(__file__).resolve().parents[1]
    cohort = sef.load_cohort(repo / "research/experiments/sb1-clean-seeker-set-20261027/cohort.json")
    config = sef.CampaignConfig(
        cohort=cohort,
        models=(ROUTE,),
        out=tmp_path / "out",
        assignments_csv=repo / "research/answer-keys/splits/assignments.csv",
    )
    assert sef.plan_campaign(config)["problems"] == []


def test_package_digest_drift_is_a_plan_problem(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (harness.cohort.tasks[0].original_task / "instruction.md").write_text("changed\n")
    plan = sef.plan_campaign(harness.config)
    assert plan["ok"] is False
    assert "digest differs" in json.dumps(plan["refused_tasks"])


def test_non_resume_run_refuses_to_reuse_retained_cells(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.run()
    calls = len(harness.omp.calls)
    with pytest.raises(SeekerRefusalError, match="--resume"):
        harness.run()
    assert len(harness.omp.calls) == calls


# ---------------------------------------------------------------------- resume


def test_resume_skips_only_predicate_passing_cells(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ("alpha", "beta"))
    _confirmed(harness, "alpha")
    harness.omp.script = lambda route, number, prompt: (
        "", "",
        None
        if "terminal-bench/beta" in prompt and sef._FEEDBACK_HEADING not in prompt
        else harness.omp.reply(route, number),
        0,
    )
    harness.run()
    assert harness.result("beta", 1)["outcome"] == "generation_failed_or_truncated"
    assert harness.result("beta", 2)["outcome"] == "not_evaluated_no_runner_error"
    calls_after_first = len(harness.omp.calls)
    assert calls_after_first == 3  # alpha x2, beta turn 1 only

    resumed = Harness.__new__(Harness)
    resumed.__dict__.update(harness.__dict__)
    resumed.config = sef.CampaignConfig(
        cohort=harness.cohort, models=(ROUTE,), out=harness.out, workers=2,
        per_model_concurrency=2, resume=True, assignments_csv=harness.csv,
    )
    resumed.omp.script = None
    resumed.run()
    prompts = harness.omp.prompts(ROUTE)[calls_after_first:]
    assert len(prompts) == 2 and all("beta" in prompt for prompt in prompts)  # alpha skipped
    assert harness.result("beta", 2)["feedback_source"] == "runner_error"
    assert sorted(p.name for p in harness.cell("beta").glob("turn-*.superseded-*")) == [
        "turn-1.superseded-1",
        "turn-2.superseded-1",
    ]
    assert not list(harness.cell("alpha").glob("*superseded*"))
    rows = [json.loads(line) for line in (harness.out / "cells.jsonl").read_text().splitlines()]
    assert len(rows) == 4


def test_resume_reruns_only_turn_two_when_its_digest_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _confirmed(harness, "alpha")
    harness.run()
    response = harness.cell("alpha") / "turn-2/response.jsonl"
    response.write_text("tampered")
    before = len(harness.omp.calls)
    harness.grader.script("alpha", "original", (1.0, "measured"))
    harness.grader.script("alpha", "repaired", (0.0, "measured"))
    resumed_config = sef.CampaignConfig(
        cohort=harness.cohort, models=(ROUTE,), out=harness.out, resume=True,
        assignments_csv=harness.csv,
    )
    harness.run(config=resumed_config)
    assert len(harness.omp.calls) == before + 1
    assert "turn-2.superseded-1" in {p.name for p in harness.cell("alpha").iterdir()}
    assert sef.read_turn(harness.cell("alpha") / "turn-2", ROUTE, 2)[0] == "evaluated"
    assert sef.read_turn(harness.cell("alpha") / "turn-1", ROUTE, 1)[0] == "evaluated"


# ----------------------------------------------------------- concurrency and ledger


def test_per_model_concurrency_is_bounded(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, ("a", "b", "c", "d"), models=(ROUTE, ROUTE_B), workers=6, per_model=1
    )
    harness.omp.delay = 0.03
    harness.run()
    assert harness.omp.peak[ROUTE] == 1 and harness.omp.peak[ROUTE_B] == 1
    assert harness.omp.peak_total <= 2
    assert len(harness.omp.calls) == 4 * 2 * 2


def test_worker_bounds_are_validated(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    for workers, per_model in ((0, 1), (9, 1), (4, 5), (4, 0)):
        with pytest.raises(ValueError):
            sef.CampaignConfig(
                cohort=harness.cohort, models=(ROUTE,), out=harness.out,
                workers=workers, per_model_concurrency=per_model,
            )
    with pytest.raises(ValueError):
        sef.CampaignConfig(cohort=harness.cohort, models=(ROUTE, ROUTE), out=harness.out)


def test_spend_rows_follow_the_shared_ledger_shapes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _confirmed(harness, "alpha")
    harness.run()
    rows = [json.loads(line) for line in harness.spend_path.read_text().splitlines()]
    omp = [row for row in rows if row.get("tool") == "omp"]
    modal = [row for row in rows if row.get("route") == "modal"]
    assert len(omp) == 2 and len(modal) == 3  # turn 1 original; turn 2 original + repaired
    for row in omp:
        assert row["route"] == ROUTE and row["usd"] == 0 and row["calls"] == 1
        assert row["basis"] == (
            "prepaid OMP subscription; $0 marginal; paid keys stripped; no tools; "
            "exact served-model attestation"
        )
        assert row["input_tokens"] == 110 and row["output_tokens"] == 5
    for row in modal:
        assert set(row) == {"cost_usd", "job", "note", "route", "ts"}
        assert row["cost_usd"] == 0.05 and row["job"].startswith("sb1-error-first:")
        assert row["ts"].endswith("+00:00")


# ------------------------------------------------------------ runner error + stats


def test_runner_error_extraction_reads_only_verified_evidence(tmp_path: Path) -> None:
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    good = verifier / "test-stdout.txt"
    good.write_text("real runner line\nFAILED tests/test_outputs.py::test_output - boom\n")
    forged = verifier / "other.log"
    forged.write_text("forged log")
    linked = verifier / "linked.txt"
    linked.symlink_to(good)
    ctrf = verifier / "ctrf.json"
    ctrf.write_text('{"results": {"tests": []}}')

    def entry(path: Path, digest: str | None = None) -> dict[str, Any]:
        value = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return {"path": str(path), "digest": digest or value}

    result = {
        "status": "measured",
        "reward": 0.0,
        "error": None,
        "logs": {
            "verifier": [entry(good), entry(forged, "sha256:" + "0" * 64), entry(linked), entry(ctrf)]
        },
        "per_test": {"tests": [{"id": "tests/test_outputs.py::test_output", "status": "failed"}]},
    }
    error = sef.extract_runner_error(result)
    assert error.verifier_files == ("test-stdout.txt",)
    assert "real runner line" in error.verifier_output and "forged" not in error.verifier_output
    block = sef.render_feedback_block(error)
    assert "tests/test_outputs.py" not in block and "- test_output" in block
    assert "FAILED [scrubbed-test-path]" in block
    assert "sandbox exit codes: not reported" in block


def test_feedback_block_is_bounded_and_scrubbed() -> None:
    error = sef.RunnerError(
        status="measured",
        reward=0.0,
        error="E " + "x" * 5000 + " tests/test_hidden.py",
        sandbox_exit_codes=(1, 2),
        failed_tests=tuple(f"tests/test_outputs.py::test_case_{i}" for i in range(80)),
        excerpts=tuple((f"tests/test_outputs.py::t{i}", "y" * 2000) for i in range(9)),
        verifier_output="z" * 20000 + "\n# pytest verifier note\n[verifier]\nsecret = 1\n",
        verifier_files=("test-stdout.txt",),
    )
    block = sef.render_feedback_block(error)
    assert len(block) <= sef.MAX_FEEDBACK_CHARS
    assert "tests/test_" not in block and "secret = 1" not in block
    accepted = sef.RunnerError("measured", 1.0, None, (0,), (), (), "", ())
    assert "accepted the submission" in sef.render_feedback_block(accepted)
    later = sef.render_feedback_block(accepted, attempt_index=4)
    assert later.startswith("# Runner error from your turn-4 submission")
    assert "Your turn-4 `solve.sh` was run" in later and "turn-1" not in later
    assert sef.RunnerError.from_json(error.to_json()) == error


def test_failure_token_enrichment_and_fallback_query() -> None:
    retriever = FakeRetriever()
    result, query = sef.retrieve_turn2_evidence(
        retriever,
        "terminal-bench/alpha",
        target_check="tests/test_outputs.py::test_output",
        failed_tests=["tests/test_outputs.py::test_output"],
    )
    assert not result.hits and query == "weak_check"
    assert retriever.queries == [
        ("terminal-bench/alpha", "weak_check test_outputs"),
        ("terminal-bench/alpha", "weak_check"),
    ]
    assert sef.failure_tokens("tests/x.py::tests test", []) == []


def test_exact_binomial_statistics() -> None:
    assert sef.exact_binomial_two_sided(0, 0) == 1.0
    assert sef.exact_binomial_two_sided(1, 1) == 1.0
    assert sef.exact_binomial_two_sided(3, 0) == pytest.approx(0.25)
    assert sef.exact_binomial_two_sided(0, 5) == pytest.approx(2 / 32)
    assert sef.exact_binomial_improvement(0, 3) == pytest.approx(1 / 8)
    assert sef.exact_binomial_improvement(3, 0) == 1.0


def test_summary_reports_unevaluated_cells_apart_from_the_paired_counts(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ("alpha", "beta", "gamma", "delta"))
    # alpha: miss -> hit; beta: hit -> hit; gamma: turn 1 grader unavailable; delta: not run at all
    _confirmed(harness, "alpha")
    harness.grader.script("beta", "original", (1.0, "measured"), (1.0, "measured"))
    harness.grader.script("beta", "repaired", (0.0, "measured"), (0.0, "measured"))
    harness.grader.script("gamma", "original", (None, "error"))
    only = sef.CampaignConfig(
        cohort=sef.Cohort(tuple(t for t in harness.cohort.tasks if t.name != "delta")),
        models=(ROUTE,), out=harness.out, assignments_csv=harness.csv,
    )
    harness.run(config=only)
    summary = sef.summarize_campaign(harness.config)
    model = summary["models"][0]
    paired = model["paired"]
    assert paired["denominator"] == 2
    assert (paired["turn_1_executed_differentials"], paired["turn_2_executed_differentials"]) == (1, 2)
    assert (paired["turn_1_only"], paired["turn_2_only"], paired["both"]) == (0, 1, 1)
    assert paired["mcnemar_exact_p_two_sided"] == 1.0 and paired["delta_turn_2_minus_turn_1"] == 0.5
    assert [item["task"] for item in model["unevaluated_cells"]] == [f"{DATASET}/gamma"]
    assert [item["task"] for item in model["missing_cells"]] == [f"{DATASET}/delta"]
    markdown = sef.render_summary_markdown(summary)
    assert ROUTE in markdown and "never zero-filled" in markdown


# -------------------------------------------------------------------------- CLI


def test_cli_plan_smoke_on_two_task_inline_cohort(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ("alpha", "beta"))
    cohort = _write_cohort_file(tmp_path, harness)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("plan must not call anything")

    monkeypatch.setattr(subprocess, "run", forbidden)
    argv = [
        "prod", "seeker-error-first", "--cohort", str(cohort), "--out", str(harness.out),
        "--assignments-csv", str(harness.csv),
    ]
    assert cli.run_cli(argv, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["command"] == "seeker-error-first" and receipt["mode"] == "plan"
    assert (receipt["cells"], receipt["model_prompts"], receipt["grader_runs"]) == (8, 16, 32)
    assert receipt["models"] == list(sef.SB1_MODELS)
    assert receipt["modal_cost_upper_bound_usd"] == pytest.approx(4 * 2 * 2 * 0.75)

    # --execute without authority is refused with a clear error and exit code 2.
    assert cli.run_cli([*argv, "--execute"], workspace=tmp_path) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["type"] == "SeekerRefusalError" and "--approval" in error["message"]
    assert not any(harness.out.glob("*/*/*/turn-*"))
