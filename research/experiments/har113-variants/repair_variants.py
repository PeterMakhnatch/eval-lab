#!/usr/bin/env python3
"""HAR-113 part 2: environment repairs for broken census tasks, as variants.

Each repair changes only ``environment/setup/setup.sh`` (re-embedded in the
healthcheck payload); the instruction, hidden tests and test command are
untouched. A repair derives from the task's leak-closed variant when it has
one (``leak_variants.py``), so the repaired package also keeps PyPI blocked.
``REPAIRS`` maps each task to its steps; a later step derives from the
earlier step's variant, so each variant record holds one change.

Repair kinds:

``keep-build-outputs``
    The adapter's setup runs ``git clean -fdx`` in the repo before the agent
    starts. ``-x`` deletes git-ignored files, which include what the image's
    build left in the tree: compiled extensions (pandas ``_libs``, h5py,
    pyproj, scikit-bio, sourmash, fastTSNE, coremltools, DGL), setuptools
    ``*.egg-info`` metadata (``DistributionNotFound``), generated version
    modules (setuptools-scm, hatch-vcs) and ``./configure`` output. The
    repair adds ``--exclude`` patterns for exactly those outputs (``KEEP``).
    Everything else stays cleaned, including ignored ``__pycache__`` and
    ``.pytest_cache`` that could hold traces of hidden tests.

``keep-files``
    The same, for one project's generated modules the general patterns miss
    (sourmash's milksnake ``_lowlevel*.py`` next to its kept library).

``login-path``
    The image puts the project's interpreter (a ``/testbed/.venv``, a pyenv
    version, an ``/opt/*-venv``) first on the *login* shell's PATH, but the
    grader runs the test command in a non-login ``sh -c``, whose PATH is the
    Debian default: it gets a system Python without pytest or the project's
    dependencies. The repair has setup ask a login shell where ``python``,
    ``python3``, ``pytest``, ``pip`` and ``pip3`` resolve and, for each that
    resolves outside the default PATH, write a one-line ``exec`` wrapper to it
    in ``/usr/local/bin`` (first on the default PATH). A wrapper, not a
    symlink, so the interpreter still finds its ``pyvenv.cfg``.

``login-path-pyenv``
    The same, except that a pyenv shim is resolved with the login shell's
    ``pyenv which``: outside the login shell the shim selects pyenv's global
    version, and when that is ``system`` it finds the wrapper again and
    execs in a loop (000984: the nop timed out with no output).

``pin``
    Setup runs one install command for a dependency the image has wrong or
    lacks (the version the repo or the image's own metadata names, or the
    repo's own distribution metadata) before the agent starts. Setup has
    network; the blocklist is applied after it. A failed install fails setup,
    so a trial never runs on a half-repaired image.

A task whose first repair still failed its nop keeps that record, rejected
with the nop as evidence; the retry is a new variant from the same parent.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/repair_variants.py
Writes ``repair_variants.json`` and nop specs to ``specs/repair/``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from common import (
    HERE,
    SETUP_SH,
    derive,
    find_record,
    hf_source,
    original,
    package_dir,
    package_rel,
    record_path,
    records_by,
)
from leak_variants import TRANSFORM as LEAK_TRANSFORM

sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import nop_spec  # noqa: E402

CREATED_BY = "har113-repair"
SPECS = HERE / "specs/repair"
GIT_CLEAN = "git clean -fdx --exclude=node_modules "
#: Build outputs ``git clean -fdx`` must keep: compiled extensions, package
#: metadata, generated version modules, configure output.
KEEP = (
    "*.so",
    "*.pyd",
    "*.egg-info",
    "*.dist-info",
    "version.py",
    "_version.py",
    "_black_version.py",
    "config.vars",
)
TOOLS = ("python", "python3", "pytest", "pip", "pip3")
#: Setup's own last step; login-path wrappers and pins go just before it.
BEFORE_BLOCKLIST = "\nwrite_blocklist\n"


def keep_build_outputs(setup: str, _arg: str | None) -> str:
    if setup.count(GIT_CLEAN) != 1:
        raise SystemExit("setup.sh: expected exactly one adapter git clean line")
    extra = " ".join(f"--exclude='{pattern}'" for pattern in KEEP)
    return setup.replace(GIT_CLEAN, f"git clean -fdx {extra} --exclude=node_modules ")


def keep_files(setup: str, patterns: str | None) -> str:
    lines = setup.splitlines(keepends=True)
    cleans = [i for i, line in enumerate(lines) if line.startswith("git clean -fdx ")]
    if len(cleans) != 1 or not patterns:
        raise SystemExit("setup.sh: expected exactly one git clean line and patterns")
    extra = " ".join(f"--exclude='{pattern}'" for pattern in patterns.split())
    lines[cleans[0]] = lines[cleans[0]].replace("git clean -fdx ", f"git clean -fdx {extra} ", 1)
    return "".join(lines)


def before_blocklist(setup: str, block: str) -> str:
    if setup.count(BEFORE_BLOCKLIST) != 1:
        raise SystemExit("setup.sh: expected exactly one write_blocklist call")
    return setup.replace(BEFORE_BLOCKLIST, block + BEFORE_BLOCKLIST)


LOGIN_PATH_BLOCK = """
# HAR-113 env-login-path@1: the grader runs the test command in a non-login shell,
# whose PATH misses the interpreter this image puts first on the login shell's PATH.
for tool in python python3 pytest pip pip3; do
  want=$(timeout 30 bash -lc "command -v $tool" 2>/dev/null | tail -n 1)
  case "$want" in /usr/local/bin/*|/usr/bin/*|/bin/*|/usr/local/sbin/*|/usr/sbin/*|/sbin/*|"") continue ;; /*) ;; *) continue ;; esac
  printf '#!/bin/sh\\nexec %s "$@"\\n' "$want" > "/usr/local/bin/$tool" && chmod 755 "/usr/local/bin/$tool"
  echo "login PATH: /usr/local/bin/$tool -> $want"
done
"""


def login_path(setup: str, _arg: str | None) -> str:
    return before_blocklist(setup, LOGIN_PATH_BLOCK)


LOGIN_PATH_PYENV_BLOCK = """
# HAR-113 env-login-path@2: the grader runs the test command in a non-login shell,
# whose PATH misses the interpreter this image puts first on the login shell's PATH.
# A pyenv shim is resolved to the version the login shell selects: outside it the
# shim picks pyenv's global version and can exec this wrapper again.
for tool in python python3 pytest pip pip3; do
  want=$(timeout 30 bash -lc "command -v $tool" 2>/dev/null | tail -n 1)
  case "$want" in */.pyenv/shims/*) want=$(timeout 30 bash -lc "pyenv which $tool" 2>/dev/null | tail -n 1) ;; esac
  case "$want" in /usr/local/bin/*|/usr/bin/*|/bin/*|/usr/local/sbin/*|/usr/sbin/*|/sbin/*|"") continue ;; /*) ;; *) continue ;; esac
  printf '#!/bin/sh\\nexec %s "$@"\\n' "$want" > "/usr/local/bin/$tool" && chmod 755 "/usr/local/bin/$tool"
  echo "login PATH: /usr/local/bin/$tool -> $want"
done
"""


def login_path_pyenv(setup: str, tools: str | None) -> str:
    """``tools`` names extra executables to wrap (e.g. ``py.test``)."""
    listed = " ".join((*TOOLS, *(tools or "").split()))
    return before_blocklist(setup, LOGIN_PATH_PYENV_BLOCK.replace(" ".join(TOOLS), listed, 1))


LOGIN_PYTHONPATH_BLOCK = """
# HAR-115 env-login-pythonpath@1: the login shell exports PYTHONPATH (the image's
# .bashrc), the grader's non-login shell does not; a .pth file puts the same
# entries on the non-login python's path.
PP=$(timeout 30 bash -lc 'printf %s "$PYTHONPATH"' 2>/dev/null | tail -n 1)
SITE=$(python -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)
if [ -z "$PP" ] || [ -z "$SITE" ]; then fail "HAR-115 login PYTHONPATH: nothing to carry"; fi
echo "$PP" | tr ':' '\\n' | grep -v '^$' > "$SITE/har115-login-pythonpath.pth"
echo "login PYTHONPATH: $PP -> $SITE/har115-login-pythonpath.pth"
"""


def login_pythonpath(setup: str, _arg: str | None) -> str:
    return before_blocklist(setup, LOGIN_PYTHONPATH_BLOCK)


def pin(setup: str, command: str | None) -> str:
    if not command:
        raise SystemExit("pin needs a command")
    block = (
        "\n# HAR-113 env-pin-dependency@1: setup has network; the blocklist comes after.\n"
        f'if ! ( cd "$CWD" && {command} ) > "$M/har113-pin.log" 2>&1; then\n'
        f'  tail -n 20 "$M/har113-pin.log" >&2; fail "HAR-113 pin failed: {command}"\n'
        "fi\n"
    )
    return before_blocklist(setup, block)


def prefetch(setup: str, command: str | None) -> str:
    if not command:
        raise SystemExit("prefetch needs a command")
    quoted = command.replace("'", "'\"'\"'")
    block = (
        "\n# HAR-146 env-prefetch-network@1: grading needs the network; setup fetches while it is open.\n"
        f'if ! ( cd "$CWD" && {command} ) > "$M/har146-prefetch.log" 2>&1; then\n'
        f"  tail -n 20 \"$M/har146-prefetch.log\" >&2; fail 'HAR-146 prefetch failed: {quoted}'\n"
        "fi\n"
    )
    return before_blocklist(setup, block)


class Kind:
    def __init__(
        self,
        transform: str,
        rewrite: Callable[[str, str | None], str],
        rationale: str,
        inputs: Callable[[str | None], dict],
    ) -> None:
        self.transform = transform
        self.rewrite = rewrite
        self.rationale = rationale
        self.inputs = inputs


UNCHANGED = "Instruction, hidden tests and test command unchanged"
KINDS = {
    "keep-build-outputs": Kind(
        "env-keep-build-outputs@1",
        keep_build_outputs,
        "setup's git clean -fdx deleted build outputs the image left in the repo ({cause}); "
        "keep compiled extensions, egg-info/dist-info metadata, generated version modules "
        "and configure output with --exclude patterns, and re-embed environment/setup in "
        f"the healthcheck payload. {UNCHANGED}",
        lambda _arg: {"kept_patterns": list(KEEP)},
    ),
    "keep-files": Kind(
        "env-keep-files@1",
        keep_files,
        "setup's git clean -fdx deleted generated files the image left in the repo ({cause}); "
        "keep them with --exclude patterns ({arg}), and re-embed environment/setup in the "
        f"healthcheck payload. {UNCHANGED}",
        lambda arg: {"kept_patterns": (arg or "").split()},
    ),
    "login-path": Kind(
        "env-login-path@1",
        login_path,
        "the grader's non-login shell misses the interpreter the image puts on the login "
        "shell's PATH ({cause}); setup writes /usr/local/bin exec wrappers for python, "
        "python3, pytest, pip and pip3 where the login shell resolves them outside the "
        "default PATH, and environment/setup is re-embedded in the healthcheck payload. "
        f"{UNCHANGED}",
        lambda _arg: {"tools": list(TOOLS)},
    ),
    "login-path-pyenv": Kind(
        "env-login-path@2",
        login_path_pyenv,
        "the grader's non-login shell misses the interpreter the image puts on the login "
        "shell's PATH ({cause}); setup writes /usr/local/bin exec wrappers for python, "
        "python3, pytest, pip and pip3 where the login shell resolves them outside the "
        "default PATH, resolving pyenv shims with the login shell's pyenv which, and "
        f"environment/setup is re-embedded in the healthcheck payload. {UNCHANGED}",
        lambda arg: {"tools": [*TOOLS, *(arg or "").split()]},
    ),
    "login-pythonpath": Kind(
        "env-login-pythonpath@1",
        login_pythonpath,
        "the grader's non-login shell misses the PYTHONPATH the image exports in the "
        "login shell ({cause}); setup writes the login shell's PYTHONPATH entries to a "
        ".pth file in the non-login python's site-packages, and environment/setup is "
        f"re-embedded in the healthcheck payload. {UNCHANGED}",
        lambda _arg: {"pth": "har115-login-pythonpath.pth"},
    ),
    "pin": Kind(
        "env-pin-dependency@1",
        pin,
        "the image's dependency is wrong ({cause}); setup runs `{arg}` before the agent "
        "starts (network is open during setup; the blocklist is applied after), failing "
        "setup if it fails, and environment/setup is re-embedded in the healthcheck "
        f"payload. {UNCHANGED}",
        lambda arg: {"command": arg},
    ),
    "prefetch": Kind(
        "env-prefetch-network@1",
        prefetch,
        "grading needs the network ({cause}); setup fetches it before the lock (`{arg}` "
        "runs before the agent starts, while the network is open; the egress lock is taken "
        "after), failing setup if it fails, and environment/setup is re-embedded in the "
        f"healthcheck payload. {UNCHANGED}",
        lambda arg: {"command": arg},
    ),
}

#: A mixed setuptools install (a newer release's files under an older
#: release's dist-info) is replaced by a clean install of the recorded version.
SETUPTOOLS = (
    "S=/usr/local/lib/python3.11/dist-packages && "
    "rm -rf $S/setuptools $S/setuptools-*.dist-info $S/pkg_resources $S/_distutils_hack "
    "$S/distutils-precedence.pth && PIP_BREAK_SYSTEM_PACKAGES=1 /usr/bin/python3.11 -m pip "
    "install --no-deps --no-cache-dir setuptools=={version}"
)

Step = tuple[str, str, str | None]
PANDAS = (
    "002195 002198 002201 002204 002205 002209 002210 002218 002219 002222 002223 002226 "
    "002228 002233"
)
KEEP_BUILD_CAUSES = {
    **dict.fromkeys(PANDAS.split(), "pandas._libs extensions"),
    "000437": "coremltools libmilstoragepython",
    "000817": "DGL build/libdgl.so",
    "001422": "h5py extensions",
    "002248": "fastTSNE._tsne",
    "002401": "pyproj._context",
    "002601": "skbio.metadata._intersection",
    "002696": "sourmash._lowlevel",
    "000651": "chainer.egg-info",
    "000652": "chainer.egg-info",
    "001087": "deepchecks package metadata",
    "001590": "isso.egg-info",
    "000225": "linopy/version.py",
    "000377": "wordcloud/_version.py",
    "001064": "tox/version.py",
    "001813": "locust/_version.py",
    "001927": "doctr/version.py",
    "002172": "drgn internal version module",
    "002354": "_black_version.py",
    "002872": "tox/version.py",
    "000080": "config.vars from ./configure",
}
LOGIN_PATH_CAUSES = {
    "000527": "pytest in /testbed/.venv",
    "000960": "pytest in /opt/torax-venv",
    "000984": "pytest in a pyenv-virtualenv env",
    "001020": "pytest in /testbed/.venv",
    "001072": "pytest in /testbed/.venv",
    "001103": "pytest in a pyenv version",
    "001139": "pytest in /testbed/.venv",
    "001179": "pytest in /testbed/.venv",
    "002032": "pytest in pyenv 3.8",
    "000972": "django in a pyenv version",
    "000973": "pulp in a pyenv version",
    "001018": "pyenv 3.7 has the base code's asyncio.coroutine",
    "001019": "pyenv 3.9 has the base code's collections.MutableMapping",
}
#: task -> steps; each step derives from the previous step's variant.
REPAIRS: dict[str, tuple[Step, ...]] = {
    **{n: (("keep-build-outputs", cause, None),) for n, cause in KEEP_BUILD_CAUSES.items()},
    **{n: (("login-path", cause, None),) for n, cause in LOGIN_PATH_CAUSES.items()},
    "000984": (
        (
            "login-path-pyenv",
            "pytest in the pyenv-virtualenv socorro-env, which only the login shell activates",
            None,
        ),
    ),
    "000437": (
        ("keep-build-outputs", KEEP_BUILD_CAUSES["000437"], None),
        ("login-path", "extensions built for the login shell's pyenv 3.10", None),
    ),
    "002696": (
        ("keep-build-outputs", KEEP_BUILD_CAUSES["002696"], None),
        ("keep-files", "sourmash's milksnake modules beside _lowlevel__lib.so", "_lowlevel*.py"),
    ),
    "001561": (
        (
            "pin",
            "/root/.venv was built for a later indico (webargs 7.0.0b1, no bleach); install "
            "the base commit's pinned requirements (celery 5.0.2's metadata needs pip<24.1)",
            "/root/.venv/bin/python -m pip install --no-cache-dir 'pip<24.1' && "
            "/root/.venv/bin/python -m pip install --no-deps --no-cache-dir -r requirements.txt",
        ),
    ),
    "001246": (
        (
            "keep-files",
            "burnman's data files, git-ignored by /burnman/data/*, are not in git",
            "burnman/data/*",
        ),
    ),
    "000655": (
        (
            "pin",
            "setuptools 59.8.0 dist-info over a newer release's files (more_itertools import)",
            SETUPTOOLS.format(version="59.8.0"),
        ),
    ),
    "002496": (
        (
            "pin",
            "setuptools 57.5.0 dist-info over a newer release's files (FileError import)",
            SETUPTOOLS.format(version="57.5.0"),
        ),
    ),
    "002078": (
        (
            "pin",
            "/opt/numpy-venv Cython 3.2.5 fails numpy's test_cython build; pin Cython 3.0",
            "/opt/numpy-venv/bin/python -m pip install --no-cache-dir 'Cython==3.0.12'",
        ),
    ),
    "002893": (
        (
            "pin",
            "twisted 16.3.0 runs from source with no distribution metadata, which pytest's "
            "unittest plugin reads; its C test extension does not build on Python 3.11, so "
            "write only the metadata",
            "/usr/bin/python3 setup.py -q egg_info --egg-base /usr/local/lib/python3.11/dist-packages",
        ),
    ),
}

#: HAR-146 locked-nop repairs: task -> steps, same shape as REPAIRS. The egress
#: lock blocks grading's network, so setup (network open) pre-fetches it.
HAR146_REPAIRS: dict[str, tuple[Step, ...]] = {
    "000450": (
        (
            "prefetch",
            "the hidden verifier pip-installs test dependencies (mimo_test_command.sh runs "
            '`pip install -e ".[yaml]" pytest jsonpath-ng` when hera/pytest/yaml is missing), '
            "which fails once the egress lock blocks the network",
            "if [ ! -x /testbed/.venv/bin/python ]; then python3 -m venv /testbed/.venv; fi && "
            'if ! /testbed/.venv/bin/python -c "import hera, pytest, yaml" >/dev/null 2>&1; then '
            "/testbed/.venv/bin/python -m pip install --upgrade pip setuptools wheel && "
            '/testbed/.venv/bin/python -m pip install -e ".[yaml]" pytest jsonpath-ng; fi',
        ),
    ),
    "002978": (
        (
            "prefetch",
            "importing kwave downloads the k-Wave C++ binaries (kwave/__init__.py install_binaries "
            "via urlretrieve), which fails once the egress lock blocks the network",
            'python3 -c "from kwave.kgrid import kWaveGrid" && python3 -c "import kwave; '
            "assert getattr(kwave, 'binaries_present', lambda: True)()\"",
        ),
    ),
}


def short(task_id: str) -> str:
    return task_id.removeprefix("format-code-task-")


def build(
    repairs: dict[str, tuple[Step, ...]], *, card: str = "har113", specs: Path = SPECS
) -> dict[str, dict]:
    """Derive each task's repair chain and write its nop spec.

    ``card`` names the records' ``created_by`` (``<card>-repair``) and the nop
    jobs (``<card>-rnop-<id>-<digest12>``). Returns the per-task summary.
    """
    leaks = records_by(LEAK_TRANSFORM)
    specs.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    for n, steps in sorted(repairs.items()):
        task_id = f"format-code-task-{n}"
        leak = leaks.get(task_id)
        parent: Path = package_dir(leak) if leak else original(task_id)
        source = {"kind": "variant", "record": record_path(leak)} if leak else hf_source(task_id)
        chain = []
        for kind_name, cause, arg in steps:
            kind = KINDS[kind_name]
            inputs = {"cause": cause, **kind.inputs(arg)}
            done = find_record(kind.transform, parent, inputs)
            if done is None:
                setup = (parent / SETUP_SH).read_text()
                done = derive(
                    parent,
                    {SETUP_SH: kind.rewrite(setup, arg).encode()},
                    transform=kind.transform,
                    rationale=kind.rationale.format(cause=cause, arg=arg),
                    created_by=f"{card}-repair",
                    inputs=inputs,
                    parent_source=source,
                )
            chain.append({"repair": kind_name, "cause": cause, "record": record_path(done)})
            parent, source = package_dir(done), {"kind": "variant", "record": record_path(done)}
        name = f"{card}-rnop-{n}-{done.variant_digest[7:19]}"
        what = " + ".join(step["repair"] for step in chain)
        label = card.upper().replace("HAR", "HAR-")
        spec = nop_spec(package_rel(done), name, f"{label} {what} repair of {task_id}")
        spec["submitted_by"] = f"{card}-variants"
        (specs / f"{name}.json").write_text(json.dumps(spec, indent=1) + "\n")
        out[task_id] = {
            "repair": what,
            "cause": "; ".join(step["cause"] for step in chain),
            "steps": chain,
            "parent": "leak variant" if leak else "original",
            "variant_digest": done.variant_digest,
            "record": record_path(done),
            "package": package_rel(done),
            "nop_job": name,
        }
    return out


def main() -> None:
    out = build(REPAIRS)
    (HERE / "repair_variants.json").write_text(json.dumps({"tasks": out}, indent=1) + "\n")
    print(f"{len(out)} repaired tasks; nop specs in {SPECS.name}/")


if __name__ == "__main__":
    main()
