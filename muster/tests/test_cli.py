"""The `muster` command: matrix, attribute and run, with a fake `harbor` that writes synthetic trials."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from seat_fixture import write_matrix, write_task

import muster
from muster import cli, commands

SRC = Path(muster.__file__).parent
HELLO = {
    "p1=oracle,p2=oracle": [1.0],
    "p1=oracle,p2=nop": [0.0],
    "p1=nop,p2=oracle": [0.0],
    "p1=nop,p2=nop": [0.0],
}

# The fake plays hello-seats: p1's note gives the reward when both seats leave a record. An oracle
# in p1 waits for p2's note and times out when p2 is not an oracle.
FAKE = """#!{python}
import json, os, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from seat_fixture import TIMEOUT, write_job
if "--version" in sys.argv:
    print("0.26.0+fake")
    sys.exit(0)
config = json.loads(Path(sys.argv[sys.argv.index("-c") + 1]).read_text())
print("fake harbor:", config["job_name"])
if config["job_name"] in os.environ.get("FAKE_FAIL", "").split(";"):
    sys.exit(1)
seats = {{}}
for p in config["participants"]:
    script = p.get("kwargs", {{}}).get("script")
    seats[p["seat"]] = Path(script).stem if script else p["name"] if p["name"] in ("oracle", "nop") else p
policy = {{seat: value if isinstance(value, str) else "agent" for seat, value in seats.items()}}
posted = policy["p1"] in ("oracle", "spoof", "agent") or policy.get("p2") == "spoof"
attempt = {{"rewards": {{"reward": float(posted and "nop" not in policy.values())}}}}
if policy["p1"] == "oracle" and policy.get("p2", "oracle") != "oracle":
    attempt.update(exception=TIMEOUT, seat_exceptions={{"p1": TIMEOUT}})
attempts = [attempt] * (config["n_attempts"] * len(config["tasks"]))
write_job(Path(config["jobs_dir"]) / config["job_name"], seats, attempts)
"""


@pytest.fixture
def harbor(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "harbor"
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable, tests=str(Path(__file__).parent)))
    path.chmod(0o755)
    monkeypatch.setenv("MUSTER_HARBOR", str(path))
    return path


@pytest.fixture
def task(tmp_path):
    return write_task(tmp_path / "tasks", "hello", {"p1": True, "p2": False}, policies=("idle", "spoof"))


def run(capsys, *argv):
    status = cli.main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_attribute_prints_the_report_and_its_status_follows_the_findings(tmp_path, capsys):
    sound = write_matrix(tmp_path / "sound", HELLO, manifest={"attempts": 1})
    status, out, err = run(capsys, "attribute", sound)
    assert (status, err) == (0, "") and out.startswith("task hello-seats: 4 trials in 4 rosters\n")
    assert out.endswith("0 errors, 0 warnings\n")
    leaky = write_matrix(tmp_path / "leaky", {**HELLO, "p1=nop,p2=nop": [1.0]})
    status, out, _ = run(capsys, "attribute", leaky)
    assert status == 1 and "free-pass" in out


def test_attribute_json_is_sorted_and_stable(tmp_path, capsys):
    out = write_matrix(tmp_path / "m", HELLO, manifest={"attempts": 1})
    status, first, _ = run(capsys, "attribute", out, "--json")
    _, second, _ = run(capsys, "attribute", out, "--json")
    report = json.loads(first)
    assert (
        status == 0
        and first == second
        and first == json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert [row["roster"] for row in report["rosters"]] == list(HELLO)
    assert report["findings"] == [] and report["errors"] == 0


def test_attribute_takes_the_task_facts_from_the_manifest(tmp_path, capsys):
    table = {
        "p1=oracle,p2=oracle": [0.75],
        "p1=idle,p2=idle": [0.0],
        "p1=idle,p2=spoof": [0.75],
        "p1=idle,p2=late": [0.75],
    }
    manifest = {"task": {"oracle_reward": 0.75}, "exploits": ["spoof"], "null_policy": "idle", "attempts": 1}
    out = write_matrix(tmp_path / "m", table, manifest=manifest)
    report = json.loads(run(capsys, "attribute", out, "--json")[1])
    assert (report["oracle_reward"], report["exploits"], report["null_policy"]) == (0.75, ["spoof"], "idle")
    assert [f["code"] for f in report["findings"]] == [
        "tested-seat-not-needed",
        "tested-seat-not-needed",
        "adversary-wins",
    ]
    more = json.loads(run(capsys, "attribute", out, "--json", "--exploits", "late,spoof")[1])
    assert more["exploits"] == ["spoof", "late"]
    assert [f["roster"] for f in more["findings"] if f["code"] == "adversary-wins"] == [
        "p1=idle,p2=spoof",
        "p1=idle,p2=late",
    ]
    plain = json.loads(run(capsys, "attribute", out, "--json", "--null", "nop")[1])
    assert plain["null_policy"] == "nop" and plain["findings"] == []  # no roster has a nop seat


def test_unusable_input_gives_one_line_and_status_3(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    (tmp_path / "file").write_text("x")
    mixed = write_matrix(tmp_path / "mixed", {"p1=oracle,p2=oracle": [1.0]})
    write_matrix(mixed, {"p1=nop,p2=nop": [0.0]}, task="muster/other")
    broken = write_matrix(tmp_path / "broken", HELLO)
    (broken / "matrix.json").write_text("[")
    for path in (tmp_path / "missing", tmp_path / "empty", tmp_path / "file", mixed, broken):
        status, out, err = run(capsys, "attribute", path)
        assert (status, out) == (3, "")
        assert err.startswith("muster: ") and err.count("\n") == 1


def test_matrix_dry_run_writes_the_configs_and_starts_nothing(tmp_path, task, capsys, monkeypatch):
    monkeypatch.delenv("MUSTER_HARBOR", raising=False)
    out = tmp_path / "out"
    argv = [
        "matrix",
        task,
        "--policies",
        "oracle,idle",
        "--attempts",
        3,
        "--concurrency",
        2,
        "--out",
        out,
        "--dry-run",
    ]
    status, text, err = run(capsys, *argv, "--timeout-multiplier", "0.05")
    assert (status, err) == (0, "")
    assert text.splitlines()[0] == "dry run: 4 rosters and 12 trials for task muster/hello"
    assert sorted(p.name for p in out.iterdir()) == [
        "matrix.json",
        "p1=idle,p2=idle",
        "p1=idle,p2=oracle",
        "p1=oracle,p2=idle",
        "p1=oracle,p2=oracle",
    ]
    assert [p.name for p in (out / "p1=oracle,p2=idle").iterdir()] == ["job.json"]
    config = json.loads((out / "p1=oracle,p2=idle" / "job.json").read_text())
    assert config == {
        "job_name": "p1=oracle,p2=idle",
        "jobs_dir": str(out / "p1=oracle,p2=idle" / "jobs"),
        "n_attempts": 3,
        "n_concurrent_trials": 2,
        "quiet": True,
        "agent_timeout_multiplier": 0.05,
        "environment": {"type": "docker", "delete": True},
        "participants": [
            {"seat": "p1", "name": "oracle"},
            {
                "seat": "p2",
                "import_path": "muster.agents.script:ScriptAgent",
                "kwargs": {"script": str(task / "policies" / "idle.sh")},
            },
        ],
        "tasks": [{"path": str(task)}],
    }
    manifest = json.loads((out / "matrix.json").read_text())
    assert manifest["dry_run"] is True and manifest["harbor"] is None and manifest["harbor_version"] is None
    assert manifest["task"] == {
        "path": str(task),
        "name": "muster/hello",
        "checksum": None,
        "seats": [{"seat": "p1", "under_test": True}, {"seat": "p2", "under_test": False}],
        "oracle_reward": None,
    }
    assert (manifest["policies"], manifest["attempts"], manifest["agent_timeout_multiplier"]) == (
        ["oracle", "idle"],
        3,
        0.05,
    )
    assert [entry["roster"] for entry in manifest["rosters"]] == [
        "p1=oracle,p2=oracle",
        "p1=oracle,p2=idle",
        "p1=idle,p2=oracle",
        "p1=idle,p2=idle",
    ]
    assert json.loads(run(capsys, *argv, "--json")[1])["trials"] == 12


def test_matrix_runs_every_roster_and_prints_the_attribution_report(tmp_path, task, harbor, capsys):
    out = tmp_path / "out"
    argv = [
        "matrix",
        task,
        "--policies",
        "oracle,idle,spoof",
        "--null",
        "idle",
        "--exploits",
        "spoof",
        "--out",
        out,
    ]
    status, text, err = run(capsys, *argv, "--timeout-multiplier", "0.05")
    assert status == 1  # error findings
    assert err.splitlines()[0] == "muster: roster 1 of 9: p1=oracle,p2=oracle" and len(err.splitlines()) == 9
    assert "task hello-seats: 9 trials in 9 rosters" in text
    assert "p1=oracle,p2=idle    1       1  timeout p1 1/1" in text
    assert "Roster p1=idle,p2=spoof: tested seat p1 is idle and reward is 1, the all-oracle reward." in text
    assert "Roster p1=idle,p2=spoof: p2=spoof gives reward 1, above 0 with p2=idle." in text
    assert "trial-error" not in text  # the timeouts of control rosters are normal
    manifest = json.loads((out / "matrix.json").read_text())
    assert (manifest["harbor"], manifest["harbor_version"], manifest["dry_run"]) == (
        str(harbor),
        "0.26.0+fake",
        False,
    )
    assert manifest["task"]["checksum"] == "f8ca14185fadd92a" and manifest["null_policy"] == "idle"
    assert manifest["started_at"] <= manifest["finished_at"] and manifest["finished_at"].endswith("Z")
    assert all(entry["returncode"] == 0 and entry["timed_out"] is False for entry in manifest["rosters"])
    assert "fake harbor: p1=idle,p2=spoof" in (out / "p1=idle,p2=spoof" / "harbor.log").read_text()
    again = run(capsys, "attribute", out)
    assert again == (1, text, "")  # the same files give the same report


def test_matrix_of_oracle_and_nop(tmp_path, task, harbor, capsys):
    status, text, _ = run(capsys, "matrix", task, "--out", tmp_path / "out", "--json")
    report = json.loads(text)
    assert status == 0 and report["errors"] == 0 and report["findings"] == []
    assert [
        (row["roster"], row["rewards"]["reward"]["mean"], row["timeouts"]) for row in report["rosters"]
    ] == [
        ("p1=oracle,p2=oracle", 1.0, 0),
        ("p1=oracle,p2=nop", 0.0, 1),
        ("p1=nop,p2=oracle", 0.0, 0),
        ("p1=nop,p2=nop", 0.0, 0),
    ]
    assert {e["seat"]: (e["effect"], e["pairs"]) for e in report["effects"]} == {
        "p1": (0.5, 2),
        "p2": (0.5, 2),
    }


def test_matrix_refuses_unusable_requests(tmp_path, task, harbor, capsys, monkeypatch):
    out = tmp_path / "out"
    cases = [
        (["matrix", tmp_path / "missing", "--out", out], "is not a task directory"),
        (
            ["matrix", task, "--out", out, "--policies", "oracle,nop,idle", "--limit", 8],
            "9 rosters, more than the limit of 8",
        ),
        (["matrix", task, "--out", out, "--policies", "oracle,ghost"], "unknown policy 'ghost'"),
        (["matrix", task, "--out", out, "--null", "idle"], "'idle' is not one of the policies oracle, nop"),
        (
            ["matrix", task, "--out", out, "--exploits", "spoof"],
            "'spoof' is not one of the policies oracle, nop",
        ),
        (["matrix", task, "--out", out, "--harbor", tmp_path / "nowhere"], "is not an executable file"),
    ]
    for argv, message in cases:
        status, text, err = run(capsys, *argv)
        assert (status, text) == (3, "") and message in err and err.count("\n") == 1
    assert not out.exists()  # nothing was written
    monkeypatch.delenv("MUSTER_HARBOR")
    assert "no harbor command" in run(capsys, "matrix", task, "--out", out)[2]
    with pytest.raises(SystemExit) as usage:
        cli.main(["matrix", str(task), "--attempts", "0"])
    assert usage.value.code == 2


def test_matrix_when_harbor_fails(tmp_path, task, harbor, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "p1=oracle,p2=nop")
    status, text, _ = run(capsys, "matrix", task, "--out", tmp_path / "some")
    assert status == 1 and "harbor ended with status 1 for roster p1=oracle,p2=nop." in text
    assert "p1=oracle,p2=nop     0       -  no trial" in text
    monkeypatch.setenv("FAKE_FAIL", ";".join(HELLO))
    status, text, err = run(capsys, "matrix", task, "--out", tmp_path / "none")
    assert (status, text) == (3, "") and "no seat trial under" in err and "harbor.log" in err


def roster_file(tmp_path, data) -> Path:
    path = tmp_path / "roster.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_run_dry_run_writes_one_job_over_all_tasks(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MUSTER_HARBOR", raising=False)
    for name in ("box-i01", "box-i00"):
        write_task(tmp_path / "team-3", name, {"p1": True, "p2": True})
    each = {"name": "terminus-2", "model_name": "deepseek/deepseek-flash", "kwargs": {"temperature": 0}}
    out = tmp_path / "runs" / "boxes-team-3"
    argv = [
        "run",
        tmp_path / "team-3",
        "--roster",
        roster_file(tmp_path, {"each": each}),
        "--out",
        out,
        "--attempts",
        2,
    ]
    status, text, err = run(capsys, *argv, "--concurrency", 2, "--timeout-multiplier", 2, "--dry-run")
    assert (status, err) == (0, "")
    assert f"job directory: {out}" in text and "dry run: 4 trials planned. Nothing was started." in text
    assert "roster: p1=terminus-2/deepseek/deepseek-flash, p2=terminus-2/deepseek/deepseek-flash" in text
    assert sorted(p.name for p in out.parent.iterdir()) == ["boxes-team-3.job.json"]
    assert json.loads((out.parent / "boxes-team-3.job.json").read_text()) == {
        "job_name": "boxes-team-3",
        "jobs_dir": str(out.parent),
        "n_attempts": 2,
        "n_concurrent_trials": 2,
        "quiet": True,
        "agent_timeout_multiplier": 2.0,
        "environment": {"type": "docker", "delete": True},
        "participants": [{"seat": "p1", **each}, {"seat": "p2", **each}],
        "tasks": [
            {"path": str(tmp_path / "team-3" / "box-i00")},
            {"path": str(tmp_path / "team-3" / "box-i01")},
        ],
    }
    listed = {
        "participants": [
            {"seat": "p2", "name": "nop"},
            {"seat": "p1", "name": "codex", "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}},
        ]
    }
    status, text, _ = run(
        capsys,
        "run",
        tmp_path / "team-3" / "box-i00",
        "--roster",
        roster_file(tmp_path, listed),
        "--out",
        out,
        "--dry-run",
        "--json",
    )
    report = json.loads(text)
    assert status == 0 and report["planned_trials"] == 1
    assert [p["seat"] for p in report["participants"]] == ["p1", "p2"]  # seat order of the task
    assert "OPENAI" not in text  # a report shows seat, agent and model only


def test_run_refuses_unusable_rosters_and_tasks(tmp_path, capsys):
    one = write_task(tmp_path / "set", "a", {"p1": True, "p2": True})
    write_task(tmp_path / "set", "b", {"p1": True, "p3": True})
    oracle = {"name": "oracle"}
    cases = [
        (tmp_path / "set", {"each": oracle}, "the tasks do not have the same seats"),
        (one, "{", "is not valid JSON"),
        (one, {"each": oracle, "participants": []}, 'needs "participants" or "each", and not both'),
        (one, {}, 'needs "participants" or "each", and not both'),
        (one, {"each": {"seat": "p1", "name": "oracle"}}, '"each" is one object without "seat"'),
        (
            one,
            {"participants": [{"seat": "p1", **oracle}]},
            "the roster names the seats p1; the task has p1, p2",
        ),
        (
            one,
            {"participants": [{"seat": "p1", **oracle}, {"seat": "p1", **oracle}]},
            "the roster names the seats p1, p1",
        ),
        (one, {"each": {"model_name": "x/y"}}, "participant p1 has no name and no import_path"),
        (
            one,
            {"each": {**oracle, "env": {"OPENAI_API_KEY": "sk-live-123"}}},
            "env OPENAI_API_KEY holds a value; write ${OPENAI_API_KEY}",
        ),
    ]
    for path, data, message in cases:
        status, text, err = run(
            capsys,
            "run",
            path,
            "--roster",
            roster_file(tmp_path, data),
            "--out",
            tmp_path / "out",
            "--dry-run",
        )
        assert (status, text) == (3, "") and message in err and "sk-live-123" not in err
    assert not (tmp_path / "out.job.json").exists()
    assert run(capsys, "run", one, "--roster", tmp_path / "missing.json", "--dry-run")[0] == 3


def test_run_starts_harbor_and_the_output_is_a_harbor_job_directory(tmp_path, harbor, capsys, monkeypatch):
    for name in ("a", "b"):
        write_task(tmp_path / "set", name, {"p1": True, "p2": True})
    roster = roster_file(tmp_path, {"each": {"name": "terminus-2", "model_name": "deepseek/deepseek-flash"}})
    out = tmp_path / "runs" / "set"
    status, text, err = run(
        capsys, "run", tmp_path / "set", "--roster", roster, "--out", out, "--attempts", 2
    )
    assert (status, err) == (0, "")
    assert sorted(p.name for p in out.parent.iterdir()) == ["set", "set.harbor.log", "set.job.json"]
    assert sorted(p.name for p in out.iterdir()) == [
        "config.json",
        "result.json",
        "task__001",
        "task__002",
        "task__003",
        "task__004",
    ]
    assert (
        "harbor ended with status 0" in text and "trials: 4 of 4, 4 with a valid reward, 0 unreadable" in text
    )
    assert "reward key  mean  n\nreward         1  4\n" in text
    monkeypatch.setenv("FAKE_FAIL", "failed")
    status, text, _ = run(
        capsys, "run", tmp_path / "set", "--roster", roster, "--out", tmp_path / "runs" / "failed"
    )
    assert status == 1 and "harbor ended with status 1" in text and "trials: 0 of 2" in text


def test_every_command_module_has_the_four_functions():
    found = dict(commands.modules())
    assert {"attribute", "matrix", "run"} <= set(found)
    for module in found.values():
        assert all(callable(getattr(module, name)) for name in ("configure", "build", "text", "run"))


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as version:
        cli.main(["--version"])
    assert version.value.code == 0 and capsys.readouterr().out == f"muster {muster.__version__}\n"
    with pytest.raises(SystemExit) as bare:
        cli.main([])
    assert bare.value.code == 2


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    return names


def test_the_core_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {"muster"}
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name != "agents":
            assert imports(path) <= allowed, path
    assert "harbor" in imports(SRC / "agents" / "script.py")


def test_only_the_runner_starts_a_process():
    starters = {"subprocess", "multiprocessing", "pty"}
    for path in sorted(SRC.rglob("*.py")):
        if path.name != "runner.py" and path.parent.name != "agents":
            source = path.read_text()
            assert not imports(path) & starters, path
            assert "os.system" not in source and "os.exec" not in source and "os.spawn" not in source, path


def test_importing_muster_does_not_import_harbor():
    code = "import sys, muster, muster.cli, muster.commands.matrix, muster.commands.run; print('harbor' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "False"
