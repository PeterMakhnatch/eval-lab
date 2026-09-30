#!/usr/bin/env python3
"""HAR-113 part 2 diagnosis: probe broken tasks whose cause is not in /testbed.

For the census clusters where ``git clean`` is not the cause (pytest missing,
a missing or incompatible dependency, a missing data file), the nop log names
what failed but not where the right interpreter lives. ``diag_solve.sh``,
run by Harbor's oracle agent on a copy of the original package, lists each
interpreter with whether it has pytest and the project module, the login
shell's PATH, and every ``bin/pytest``. Later rounds probe a task still
broken after a repair on that repair's variant (the base names its repair
kind), with commands aimed at the error the repair's nop left. No model;
the verifier grades the untouched repo.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/diag_specs.py
Writes specs to ``specs/diag/``; run them with ``runner.py``.
"""

from __future__ import annotations

import json
import shutil
import sys

from common import HERE, ROOT, census, original, package_dir, records_by
from repair_variants import KINDS

sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import nop_spec  # noqa: E402

PYTEST_MISSING = ("000527", "000960", "000984", "001020", "001072", "001103", "001139")
PYTEST_MISSING += ("001179", "002032")
MISSING_DEPENDENCY = ("000655", "000972", "000973", "002893")
INCOMPATIBLE_DEPENDENCY = ("001018", "001019", "001561", "002078", "002496")
MISSING_DATA_FILE = ("001246",)
TASKS = PYTEST_MISSING + MISSING_DEPENDENCY + INCOMPATIBLE_DEPENDENCY + MISSING_DATA_FILE
#: Second round (``har113-diag2-*``): task -> (base package, shell run after the
#: listing). The base is ``original`` or the repair kind whose variant to probe.
ROUND2 = {
    "000437": (
        "keep-build-outputs",
        "ls -la coremltools | grep -E 'lib|so'; find / -xdev -name 'libmodelpackage*'",
    ),
    "002696": (
        "keep-build-outputs",
        "ls -la sourmash; find / -xdev -name '*lowlevel*' -not -path '/proc/*'",
    ),
    "001561": (
        "original",
        "/root/.venv/bin/pip list 2>/dev/null | grep -i -E 'webargs|marshmallow|flask'; grep -rh -i webargs /testbed/requirements*.txt /testbed/setup.cfg /testbed/setup.py 2>/dev/null",
    ),
    "002078": (
        "original",
        "/opt/numpy-venv/bin/pip list 2>/dev/null | grep -i -E 'cython|meson|numpy'; grep -h -i -A2 cython /testbed/pyproject.toml /testbed/requirements/*.txt 2>/dev/null | head -n 20; sed -n '1,40p' /testbed/.build_env/test_command.sh",
    ),
    "002496": (
        "original",
        "pip list 2>/dev/null | grep -i -E 'setuptools|pytest|django'; ls /usr/local/lib/python3.11/dist-packages | grep -i setuptools; sed -n '1,30p' /testbed/.build_env/test_command.sh",
    ),
    "002893": (
        "original",
        "pip list 2>/dev/null | grep -i -E 'twisted|pytest|incremental'; ls -a /testbed /testbed/src 2>/dev/null | head -n 60; sed -n '1,30p' /testbed/.build_env/test_command.sh",
    ),
    "000655": (
        "original",
        "pip list 2>/dev/null | grep -i -E 'more|setuptools|chainer|numpy|pytest'; sed -n '1,30p' mimo_test_command.sh",
    ),
    "001246": (
        "original",
        "ls burnman/data/input_masses | head; git ls-files burnman/data/input_masses | head; git log --oneline -1",
    ),
}
#: Third round (``har113-diag3-*``): 001246's repo has its ``.git`` hidden by
#: setup, so round two could not ask git; 000984 hangs with no output once
#: pytest resolves (the wrapper's target is printed; SIGINT did not stop the
#: loop, so this probe ran to the agent timeout and was cancelled); 001561 and
#: 002893 failed their pins.
ROUND3 = {
    "001246": (
        "original",
        "G=/var/lib/mimo/git-hidden; B=$(cat /var/lib/mimo/base); "
        "git --git-dir=$G ls-tree -r --name-only $B | grep -c input_masses; "
        "git --git-dir=$G --work-tree=. ls-files -d | head -n 20; "
        "git --git-dir=$G --work-tree=. check-ignore -v burnman/data/input_masses/atomic_masses.dat",
    ),
    "000984": (
        "login-path",
        "cat /usr/local/bin/pytest; timeout -s INT 120 pytest -v -x "
        "socorro/unittest/signature/test_rules.py 2>&1 | tail -n 40",
    ),
    "001561": (
        "original",
        "/root/.venv/bin/python -m pip install --no-deps --no-cache-dir -r requirements.txt "
        "2>&1 | tail -n 12; /root/.venv/bin/python -m pip check 2>&1 | tail -n 12; "
        "/root/.venv/bin/python -c 'import indico.web.args' 2>&1 | tail -n 3",
    ),
    "002893": (
        "original",
        "export PIP_BREAK_SYSTEM_PACKAGES=1; python3 -m pip install --no-deps "
        "--no-build-isolation --no-cache-dir -e . 2>&1 | tail -n 15; echo == isolated; "
        "python3 -m pip install --no-deps --no-cache-dir -e . 2>&1 | tail -n 15; "
        'python3 -c \'import importlib.metadata as m; print("twisted", m.version("twisted"))\'',
    ),
}
#: Fourth round (``har113-diag4-*``): 001561's retry pin failed setup in
#: seconds; run the same install and show where it stops.
ROUND4 = {
    "001561": (
        "original",
        "/root/.venv/bin/python -m pip install --no-cache-dir 'pip<24.1' 2>&1 | tail -n 3; "
        "timeout -k 10 600 /root/.venv/bin/python -m pip install --no-deps --no-cache-dir "
        "-r requirements.txt 2>&1 | grep -v -e '^  Downloading' -e '^Collecting' | tail -n 30",
    ),
}
SPECS = HERE / "specs/diag"
MARGIN_S = 600


def write_probe(short: str, source, name: str, module: str, extra: str) -> str:
    rel = f"derived/har113-probes/{short}-{name.split('-')[1]}"
    target = ROOT / rel
    if not target.exists():
        shutil.copytree(source, target)
        for path in (target, *target.rglob("*")):
            path.chmod(path.stat().st_mode | 0o200)
        (target / "solution").mkdir()
        solve = (HERE / "diag_solve.sh").read_text().replace("__MODULE__", module)
        (target / "solution/solve.sh").write_text(solve.replace("__EXTRA__", extra))
        (target / "solution/solve.sh").chmod(0o755)
    task_id = f"format-code-task-{short}"
    spec = nop_spec(rel, name, f"HAR-113 environment diagnosis of {task_id}")
    spec["agent"] = "oracle"
    spec["timeout_seconds"] += MARGIN_S
    spec["submitted_by"] = "har113-variants"
    (SPECS / f"{name}.json").write_text(json.dumps(spec, indent=1) + "\n")
    return name


def main() -> None:
    rows = census()
    SPECS.mkdir(parents=True, exist_ok=True)
    names = []
    for short in TASKS:
        task_id = f"format-code-task-{short}"
        module = rows[task_id]["project_key"] or "sys"
        names.append(write_probe(short, original(task_id), f"har113-diag-{short}", module, "true"))
    for label, rounds in (("diag2", ROUND2), ("diag3", ROUND3), ("diag4", ROUND4)):
        for short, (base, extra) in rounds.items():
            task_id = f"format-code-task-{short}"
            module = rows[task_id]["project_key"] or "sys"
            source = (
                original(task_id)
                if base == "original"
                else package_dir(records_by(KINDS[base].transform)[task_id])
            )
            names.append(write_probe(short, source, f"har113-{label}-{short}", module, extra))
    print(f"{len(names)} diagnosis specs in {SPECS.name}/")


if __name__ == "__main__":
    main()
