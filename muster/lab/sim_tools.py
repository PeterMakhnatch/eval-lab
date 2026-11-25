from __future__ import annotations

import argparse
import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from muster import cli
from muster.commands import card as card_command
from muster.lab import card, cardfile, finding, parity, toy
from muster.model import InputError
from muster.sim_fixture import Run

VERIFY_SEEDS = (0, 17)
JOBS_SEED = 17
POLICY_SEEDS = (0, 17)
PARITY_SEED = 0
_STALE_FILES = 7
_JOB_FAMILIES = (("toy", 0), ("toy:grader_at_least", 1), ("toy:trap_no_bite", 1))
_VERIFY_KEYS = {"family", "ok", "stale", "episodes", "reason"}


def _card_args(seed: int) -> tuple[str, ...]:
    return ("--schedules", "3", "--worst", "4", "--instances", "1", "--seed", str(seed))


def _card_command(path: Path, family: str, *args: str, json_output: bool = True) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    card_command.configure(parser)
    options = parser.parse_args([family, *args, *(["--json"] if json_output else [])])
    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        status = card_command.run(options, card_path=path)
    return status, stdout.getvalue()


def _verify(sim: Run, path: Path, name: str, *, ok: bool, reason: str | None = None,
            stale: tuple[str, ...] = ()) -> dict:
    status, text = _card_command(path, "toy", "--verify")
    result = json.loads(text)
    valid = (set(result) == _VERIFY_KEYS and text.encode() == cardfile.canonical(result)
             and result["family"] == "toy" and result["ok"] is ok
             and status == int(not ok) and result["stale"] == list(stale))
    if ok:
        valid = valid and result["episodes"] > 0 and result["reason"] is None
    else:
        valid = valid and reason is not None and reason in (result["reason"] or "")
    sim.require("card-verify", name, valid, f"exit={status}; {result}")
    return result


def _verify_sources(sim: Run, path: Path, source: Path, original: bytes) -> None:
    name, = json.loads(original)["sources"]["files"]
    reduced = _verify(sim, path, "valid-json", ok=True)
    status, text = _card_command(path, "toy", "--verify", json_output=False)
    expected = (f"toy: committed card matches 1 source files; "
                f"reduced card PASS ({reduced['episodes']} episodes)\n")
    sim.require("card-verify", "valid-text", status == 0 and text == expected)
    sim.require("card-verify", "unchanged", path.read_bytes() == original)
    revised = json.loads(original)
    revised["sources"]["files"][name] = "0" * 64
    path.write_bytes(cardfile.canonical(revised))
    _verify(sim, path, "saved-digest", ok=False, reason="source files differ", stale=(name,))
    status, text = _card_command(path, "toy", "--verify", json_output=False)
    expected = (f"toy: source files differ; changed or missing: {name}; "
                "rebuild with: python -m muster card toy --write\n")
    sim.require("card-verify", "stale-text", status == 1 and text == expected)
    path.write_bytes(original)
    source.write_text("changed", encoding="utf-8")
    _verify(sim, path, "changed-source", ok=False, reason="source files differ", stale=(name,))
    source.unlink()
    _verify(sim, path, "missing-source", ok=False, reason="source files differ", stale=(name,))
    source.write_text("original", encoding="utf-8")


def _verify_manifest(sim: Run, path: Path, original: bytes) -> None:
    for key, value, reason in (
        ("ok", False, "committed card is not ok"),
        ("family", "wrong", "committed family differs"),
        ("conditions", "wrong", "committed conditions differs"),
        ("policies", "wrong", "committed policies differs"),
    ):
        revised = json.loads(original)
        revised[key] = value
        path.write_bytes(cardfile.canonical(revised))
        _verify(sim, path, f"saved-{key}", ok=False, reason=reason)
    path.unlink()
    _verify(sim, path, "missing-card", ok=False, reason="missing file")
    path.write_bytes(original)
    status, _ = _card_command(path, "toy", *_card_args(sim.seed), "--check")
    sim.require("card-verify", "full-check", status == 0)
    assert original.endswith(b"\n")
    path.write_bytes(original[:-1] + b" ")
    status, _ = _card_command(path, "toy", *_card_args(sim.seed), "--check")
    sim.require("card-verify", "full-check-byte-change", status == 1)
    path.write_bytes(original)


def _verify_stale_bound(sim: Run, path: Path, adapter: SimpleNamespace, source: Path) -> None:
    extra = [source.parent / f"extra-{index}.py" for index in range(_STALE_FILES)]
    for item in extra:
        item.write_text("extra", encoding="utf-8")
    names = tuple(str(item.resolve()) for item in extra)
    with patch.object(adapter, "sources", return_value=[source, *extra]):
        _verify(sim, path, "all-stale-json", ok=False, reason="source files differ", stale=names)
        status, text = _card_command(path, "toy", "--verify", json_output=False)
        expected = (f"toy: source files differ; changed or missing: {', '.join(names[:5])}; "
                    "rebuild with: python -m muster card toy --write\n")
        sim.require("card-verify", "bounded-stale-text", status == 1 and text == expected)


def card_verify(sim: Run) -> None:
    with TemporaryDirectory(prefix="muster-verify-sim-") as folder:
        directory = Path(folder)
        path, source = directory / "card.json", directory / "source.py"
        source.write_text("original", encoding="utf-8")
        adapter = toy.family()
        adapter.sources = lambda: [source]
        with patch.object(finding, "adapter_for", return_value=adapter):
            with patch.object(card, "ProcessPoolExecutor", side_effect=AssertionError("pool used")):
                status, text = _card_command(path, "toy", *_card_args(sim.seed), "--write",
                                             "--jobs", "1")
            sim.require("card-verify", "serial-write",
                        status == 0 and path.read_bytes() == text.encode())
            original = path.read_bytes()
            _verify_sources(sim, path, source, original)
            _verify_manifest(sim, path, original)
            _verify_stale_bound(sim, path, adapter, source)
        bad = toy.family("grader_at_least")
        bad.sources = adapter.sources
        with patch.object(finding, "adapter_for", return_value=bad):
            _verify(sim, path, "reduced-fail", ok=False, reason="reduced card FAIL")
        _verify(sim, path, "no-source-hook", ok=False, reason="adapter has no sources hook")


def _worker_error(sim: Run, path: Path) -> None:
    adapter = toy.family()
    policies = adapter.policies()
    adapter.policies = lambda: {"worker_missing": policies["idle"], **policies}
    parent_pid, adapter_for = os.getpid(), finding.adapter_for

    def parent_adapter(name: str):
        return adapter if os.getpid() == parent_pid else adapter_for(name)

    message = None
    with patch.object(finding, "adapter_for", side_effect=parent_adapter):
        try:
            _card_command(path, "toy", "--schedules", "0", "--worst", "0", "--instances", "1",
                          "--seed", str(sim.seed), "--jobs", "2", "--write")
        except InputError as error:
            message = str(error)
    sim.require("card-jobs", "worker-message", message == "lineup contains an unknown policy",
                str(message))
    sim.require("card-jobs", "worker-no-card", not path.exists())


def card_jobs(sim: Run) -> None:
    with TemporaryDirectory(prefix="muster-jobs-sim-") as folder:
        path = Path(folder) / "card.json"
        common = _card_args(sim.seed)
        for family, expected in _JOB_FAMILIES:
            serial, lhs = _card_command(path, family, *common, "--jobs", "1")
            parallel, rhs = _card_command(path, family, *common, "--jobs", "2")
            sim.require("card-jobs", f"{family}/status", serial == parallel == expected)
            sim.require("card-jobs", f"{family}/bytes", lhs == rhs and bool(lhs)
                        and lhs.encode() == cardfile.canonical(json.loads(lhs)))
        _worker_error(sim, path)


def _cli(*args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = cli.main(list(args))
    return status, stdout.getvalue(), stderr.getvalue()


def _policy_compare(sim: Run, name: str, common: tuple[str, ...], zoo_args: tuple[str, ...],
                    file_args: tuple[str, ...], policy: str) -> None:
    usual, lhs, _ = _cli("lab", *common, *zoo_args)
    local, rhs, _ = _cli("lab", *common, *file_args)
    sim.require("file-policy", f"{name}/exit", usual == local == 0)
    if usual != 0 or local != 0:
        return
    expected, actual = json.loads(lhs), json.loads(rhs)
    sim.require("file-policy", f"{name}/lineup", len(actual) == len(expected) == 1
                and policy in actual[0]["lineup"].values())
    for result in actual:
        result["lineup"] = {seat: "naive" if spec == policy else spec
                            for seat, spec in result["lineup"].items()}
    sim.require("file-policy", f"{name}/episode", actual == expected)


def file_policies(sim: Run) -> None:
    with TemporaryDirectory(prefix="muster-policy-sim-") as folder:
        source = Path(folder) / "policy.py"
        source.write_text(
            'def play(seat):\n'
            '    status, data = yield "GET", "/orders", None\n'
            '    assert status == 200\n'
            '    amount = sum(data["orders"])\n'
            '    if amount:\n'
            '        status, data = yield "GET", "/counter", None\n'
            '        assert status == 200\n'
            '        yield "POST", "/set", {"value": data["value"] + amount}\n'
            'not_callable = 42\n', encoding="utf-8")
        policy = f"file:{source}:play"
        instance = f"i{sim.seed % 4 + 1:02d}"
        common = ("toy", "--condition", "calm", "--instance", instance, "--json",
                  "--schedule", f"seed:{sim.seed}")
        _policy_compare(sim, "uniform", common, ("--policy", "naive"),
                        ("--policy", policy), policy)
        common = ("toy", "--condition", "busy", "--instance", instance, "--json", "--worst", "4")
        _policy_compare(sim, "mixed-worst", common, ("--lineup", "p1=naive,p2=oracle,p3=oracle"),
                        ("--lineup", f"p1={policy},p2=oracle,p3=oracle"), policy)
        for name, spec, message in (
            ("missing-file", f"file:{source.with_name('missing.py')}:play", "expected file:"),
            ("missing-function", f"file:{source}:absent", "no policy 'absent'"),
            ("non-callable", f"file:{source}:not_callable", "no policy 'not_callable'"),
        ):
            status, stdout, stderr = _cli("lab", *common, "--policy", spec)
            sim.require("file-policy", name, status == 3 and not stdout
                        and stderr.startswith("muster:") and message in stderr
                        and len(stderr.splitlines()) == 1 and "Traceback" not in stderr)


def parity_checks(sim: Run) -> None:
    condition = "calm-probe"
    task = parity.TASKS / "depot" / "build" / condition / "depot-i01"
    if not task.is_dir():
        print(f"Skip parity seed {sim.seed}: no built task {task}")
        return
    common = ("parity", "depot", "--condition", condition, "--instances", "1", "--json")
    status, text, _ = _cli(*common)
    result = json.loads(text) if text else []
    sim.require("parity", "packaged-match", status == 0 and result == [
        {"condition": condition, "instance": "i01", "policy": "oracle", "passed": True,
         "detail": ""}])
    with TemporaryDirectory(prefix="muster-parity-sim-") as folder:
        root = Path(folder)
        copied = root / "depot" / "build" / condition / task.name
        shutil.copytree(task, copied)
        verify = copied / "tests/verify.py"
        text = verify.read_text(encoding="utf-8")
        original = '    result = {"reward": reward,'
        assert text.count(original) == 1
        verify.write_text(text.replace(original, '    result = {"reward": 0.0,'), encoding="utf-8")
        with patch.object(parity, "TASKS", root):
            status, text, _ = _cli(*common)
        result = json.loads(text) if text else []
        sim.require("parity", "packaged-score-drift", status == 1 and result == [
            {"condition": condition, "instance": "i01", "policy": "oracle", "passed": False,
             "detail": "score reward: HTTP 0.0 != lab 1.0"}])
