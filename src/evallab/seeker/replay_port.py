"""Differential port replay for the four tasks GT-2 built port glue for.

The candidate never executes on the host as a shell script: files its `solve.sh`
would place under /app are extracted statically (heredoc bodies and `cp` of
companion files), staged, and handed to the PM port's own `scoring_script.score`,
exactly as GT-2's replay did for its registered witnesses. Tasks whose candidate
needs to *run* code to produce artifacts are reported as unsupported here.

    .venv/bin/python -m evallab.seeker.replay_port --driver sonnet --task vf2-speedup-networkx
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PORT_ROOT = Path.home() / "Developer/eval-lab-data/tb4-20261020/karotte-port"
PORT_PYTHON = Path("/private/tmp/rs1-port-venv/bin/python")
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-21/reward-seeker"
PKG = {
    "cargo-flight-dispatch": "cargo_flight_dispatch",
    "fin-saccr-rwa": "fin_saccr_rwa",
    "vf2-speedup-networkx": "vf2_speedup_networkx",
    "html-js-filter": "html_js_filter",
}
HEREDOC = re.compile(r"cat > (?P<target>/app/\S+) <<'(?P<tag>[A-Z]+)'\n(?P<body>.*?)\n(?P=tag)\n", re.S)
CP_LINE = re.compile(r'cp (?:"\$DIR/|")?(?P<src>[A-Za-z0-9_.\-/]+)"? (?P<target>/app/\S+)')


def extract_app_files(submission: Path, app: Path) -> list[str]:
    """Statically place the files solve.sh would write under /app into `app`."""
    solve = (submission / "solve.sh").read_text()
    written = []
    for match in HEREDOC.finditer(solve):
        target = app / match.group("target").removeprefix("/app/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(match.group("body") + "\n")
        written.append(str(target.relative_to(app)))
    for match in CP_LINE.finditer(solve):
        source = submission / Path(match.group("src")).name
        if not source.is_file():
            continue
        target = app / match.group("target").removeprefix("/app/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(str(target.relative_to(app)))
    return written


def child(task: str, submission: Path, stage: Path, result: Path) -> None:
    """Runs in a subprocess with the port task dir as cwd (imports its scoring_script)."""
    app = stage / "app"
    app.mkdir(parents=True)
    written = extract_app_files(submission, app)
    reward: float | None = None
    notes = f"extracted: {written}"
    if task == "vf2-speedup-networkx":
        from environment.tasks.vf2_speedup_networkx import scoring_script as ss

        ss._read_speed_seed = lambda store=None: 123456789  # as the port's own tests pin it
        reward, _meta = ss.score(app)
    elif task == "html-js-filter":
        from environment.tasks.html_js_filter import scoring_script as ss

        reward, _meta = ss.score(app / "filter.py")
    elif task == "fin-saccr-rwa":
        from environment.tasks.fin_saccr_rwa import scoring_script as ss

        artifacts = [app / "output/sa_ccr_results.csv", app / "output/sa_ccr_workings.xlsx"]
        if not all(a.is_file() for a in artifacts):
            result.write_text(json.dumps({"reward": None, "error": "artifacts not statically extractable", "notes": notes, "extracted": written}))
            return
        reward, _meta = ss.score(*artifacts)
    elif task == "cargo-flight-dispatch":
        from environment.paths import INTERMEDIATE_DATA_DIR
        from environment.tasks.cargo_flight_dispatch import scoring_script as ss
        from environment.tasks.cargo_flight_dispatch.workspace import CODE_FILES, install_workspace

        work = stage / "work"
        install_workspace(INTERMEDIATE_DATA_DIR, work, work / "data", work / "output/flight_plan.json")
        for name in CODE_FILES:
            staged = app / name
            if staged.is_file():
                shutil.copy2(staged, work / name)
        reward, _meta = ss.score(*[work / n for n in CODE_FILES])
    result.write_text(json.dumps({"reward": reward, "error": None, "notes": notes, "extracted": written}))


def replay(driver: str, task: str, out: Path) -> dict[str, Any]:
    submission = out / "submissions" / driver / task
    port_task = PORT_ROOT / task
    stage = Path(f"/private/tmp/rs1-port/{driver}-{task}")
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(
        port_task, stage / "port", ignore=shutil.ignore_patterns("venvs", "__pycache__", ".git")
    )
    result = stage / "result.json"
    proc = subprocess.run(
        [str(PORT_PYTHON if PORT_PYTHON.is_file() else sys.executable), __file__, "--child", task, str(submission), str(stage), str(result)],
        cwd=stage / "port",
        env={"PYTHONPATH": str(stage / "port/src"), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True, text=True, timeout=1200, check=False,
    )
    if result.is_file():
        payload = json.loads(result.read_text())
    else:
        payload = {"reward": None, "error": f"child exit {proc.returncode}: {proc.stderr[-500:]}"}
    record = {"driver": driver, "task": task, "mode": "port-static-extract", **payload}
    target = out / "replay/port" / f"{driver}-{task}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=1) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--driver")
    parser.add_argument("--task")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--child", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        child(args.child[0], Path(args.child[1]), Path(args.child[2]), Path(args.child[3]))
        return 0
    record = replay(args.driver, args.task, args.out.expanduser())
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
