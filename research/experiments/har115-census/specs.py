#!/usr/bin/env python3
"""HAR-115 Daytona specs: repairs, diagnoses and census nops.

Reuses HAR-113's toolbox (``../har113-variants``): the repair kinds and
``build`` (variants plus ``har115-rnop-*`` nop specs), ``write_probe``
(oracle diagnoses, ``har115-diag-*``) and ``write_specs`` (``har115-nop-*``
for every census task still ``unknown``, lightest image first).

Repairs here are the 51 tasks HAR-113's unknown waves found not sound
(its README section 4), matched to a repair kind from their nop logs:

* keep-build-outputs: the error names a git-ignored build output setup's
  ``git clean -fdx`` deletes (compiled extension, package metadata,
  generated version module); pandas ``_libs`` as in HAR-113's 14.
* login-path-pyenv (``env-login-path@2``, safe for pyenv shims): pytest or
  the project's dependencies are missing on the grader's non-login PATH.
* keep-files: a git-ignored built asset (002055's ``frontend/dist``).

Tasks whose nop is not an environment failure, or whose cause the logs do
not show, get an oracle diagnosis instead. 002649's nop hit a Daytona
conflict, so it is simply nopped again.

Usage (from the worktree root):
    uv run python research/experiments/har115-census/specs.py
Writes ``specs/{repair,diag,unknown,renop}/`` and ``repairs.json``; prints
the run order (repairs, diagnoses, re-nop, then census nops).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "har113-variants"))
from common import CENSUS, original  # noqa: E402
from diag_specs import write_probe  # noqa: E402
from nopspec import nop_spec  # noqa: E402
from repair_variants import build  # noqa: E402
from unknown_specs import write_specs  # noqa: E402

CARD = "har115"
SPECS = HERE / "specs"
PANDAS = (
    "002196 002197 002199 002202 002206 002208 002211 002212 002214 002215 002216 002220 "
    "002221 002224 002229 002230 002231"
)
#: task -> the build output its nop shows missing.
KEEP_BUILD = {
    **dict.fromkeys(PANDAS.split(), "pandas._libs extensions"),
    "000158": "package metadata (PackageNotFoundError)",
    "000159": "flake8_simplify package metadata",
    "000160": "flake8_simplify package metadata",
    "000161": "flake8_simplify package metadata",
    "000190": "pydp._pydp extension",
    "000354": "package metadata (DistributionNotFound)",
    "001607": "cloup/_version.py",
    "002017": "napari/_version.py",
    "002281": "gensim Cython extensions",
    "002307": "pre_commit package metadata",
    "002353": "_black_version.py",
    "002400": "pyproj._network extension",
    "002410": "pylsp/_version.py",
    "002451": "satpy/version.py",
    "002483": "rasterio extensions (rasterio.errors fails to import at pytest start)",
    "002644": "sherpa.utils._utils extension",
    "002739": "statsmodels/_version.py",
    "002873": "tox/version.py",
}
LOGIN_PATH = {
    "001058": "oslo_config missing for the non-login python",
    "001068": "django missing for the non-login python",
    "001406": "pytest: command not found",
}
REPAIRS = {
    **{n: (("keep-build-outputs", cause, None),) for n, cause in KEEP_BUILD.items()},
    **{n: (("login-path-pyenv", cause, None),) for n, cause in LOGIN_PATH.items()},
    "002055": (
        (
            "keep-files",
            "the conftest needs the git-ignored built frontend/dist/static",
            "frontend/dist",
        ),
    ),
    # Round two, from the har115-diag-* listings.
    "001024": (
        (
            "login-path-pyenv",
            "the login shell's pyenv 3.7.17 has pytest and cgi.escape; the grader's "
            "non-login pytest is Python 3.11's, where cgi.escape is gone",
            None,
        ),
    ),
    "000968": (
        (
            "login-path-pyenv",
            "the test command runs py.test; the login shell's pyenv 2.7.18 runs this "
            "Python 2 code (urlparse), the non-login py.test is Python 3's",
            "py.test",
        ),
    ),
    "002191": (
        (
            "pin",
            "the base commit's werkzeug imports OpenSSL.tsafe, which /testbed/.venv's "
            "pyOpenSSL 26.2.0 no longer has; install the last pyOpenSSL with tsafe",
            "/testbed/.venv/bin/python -m pip install --no-cache-dir "
            "'pyOpenSSL==19.1.0' 'cryptography==3.4.8'",
        ),
    ),
    # Round three, from the har115-diag2-* listings. 001176's first attempt
    # (login-path-pyenv) is superseded: its python was right, its path was not.
    "001176": (
        (
            "login-pythonpath",
            "the image's .bashrc exports PYTHONPATH=/testbed; the grader's non-login "
            "python cannot import bzt from /testbed",
            None,
        ),
    ),
    "001618": (
        (
            "pin",
            "/usr/local python 3.9 has pip 20.2.4's dist-info over a later pip's files "
            "(python -m pip fails on import) and the base commit's pip-tools 4.5.0 "
            "predates pip 20.1; reinstall pip 20.0.2 cleanly",
            "S=/usr/local/lib/python3.9/site-packages && rm -rf $S/pip $S/pip-*.dist-info && "
            "/usr/local/bin/python -m ensurepip && "
            "/usr/local/bin/python -m pip install --no-cache-dir 'pip==20.0.2'",
        ),
    ),
}
#: task -> (module the listing looks for, extra shell run after the listing).
DIAGNOSE = {
    "000124": (
        "app",
        "cd /testbed && ls; grep -rn -i -E 'create_all|alembic' --include=*.py . | head -n 20",
    ),
    "000393": (
        "molecule",
        "cd /testbed && python -m pytest -x -q -rA src/molecule/test/unit/provisioner/test_ansible.py"
        " 2>&1 | grep -v '^$' | head -n 60",
    ),
    "000968": ("urlparse", "ls /root/.pyenv/versions 2>&1; command -v python2 py.test"),
    "001024": (
        "supervisor",
        "python3 -c 'import sys; print(sys.version)'; ls /root/.pyenv/versions 2>&1",
    ),
    "001618": (
        "piptools",
        "for py in $(which -a python3 python); do $py -m pip --version; done 2>&1; "
        "python3 -m pip list 2>/dev/null | grep -i -E '^(pip|pip-tools|click) '",
    ),
    "002191": (
        "OpenSSL",
        "/testbed/.venv/bin/python -m pip list 2>/dev/null"
        " | grep -i -E 'openssl|cryptography|werkzeug'",
    ),
}
#: Round two (``har115-diag2-*``).
DIAGNOSE2 = {
    "000393": (
        "molecule",
        "cd /testbed && python -m pip list 2>/dev/null | grep -i -E 'molecule|ansible|jsonschema'; "
        "python -m pytest -x -q src/molecule/test/unit/provisioner/test_ansible.py 2>&1"
        " | grep -i -E -B2 -A6 'validation|schema|invalid|CRITICAL|ERROR ' | head -n 50",
    ),
    "001176": (
        "bzt",
        "bash -lc 'echo PYTHONPATH=$PYTHONPATH; command -v python; python -c \"import bzt, sys;"
        " print(bzt.__file__)\"' 2>&1; cd /testbed && ls; ls -d /testbed/bzt* 2>&1; "
        "python -m pip show bzt 2>&1 | head -n 3",
    ),
    "001618": (
        "pip",
        "ls -d /usr/local/lib/python3.9/site-packages/pip* ; "
        "grep -m1 '^Version' /usr/local/lib/python3.9/site-packages/pip-*/METADATA; "
        "grep -n -i 'pip' /testbed/setup.py | head -n 10; cd /testbed && git log -1 --format=%cd",
    ),
}
RENOP = ("002649",)


def main() -> None:
    pool = {e["task_id"]: e for e in json.loads((CENSUS / "pool.json").read_text())["pool"]}
    order: list[Path] = []
    repairs = build(REPAIRS, card=CARD, specs=SPECS / "repair")
    (HERE / "repairs.json").write_text(json.dumps({"tasks": repairs}, indent=1) + "\n")
    order += [SPECS / "repair" / f"{entry['nop_job']}.json" for entry in repairs.values()]
    for round_name, rounds in (("diag", DIAGNOSE), ("diag2", DIAGNOSE2)):
        for short, (module, extra) in sorted(rounds.items()):
            task_id = f"format-code-task-{short}"
            name = write_probe(
                short,
                original(task_id),
                f"{CARD}-{round_name}-{short}",
                module,
                extra,
                SPECS / "diag",
            )
            order.append(SPECS / "diag" / f"{name}.json")
    (SPECS / "renop").mkdir(parents=True, exist_ok=True)
    for short in RENOP:
        task_id = f"format-code-task-{short}"
        name = f"{CARD}-nop-{short}"
        spec = nop_spec(
            pool[task_id]["task"], name, f"HAR-115 re-nop of {task_id} after a Daytona conflict"
        )
        spec["submitted_by"] = f"{CARD}-variants"
        path = SPECS / "renop" / f"{name}.json"
        path.write_text(json.dumps(spec, indent=1) + "\n")
        order.append(path)
    order += write_specs(CARD, SPECS / "unknown")
    for path in order:
        print(path)


if __name__ == "__main__":
    main()
