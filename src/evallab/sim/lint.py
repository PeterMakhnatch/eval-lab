from __future__ import annotations

import argparse
import contextlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from random import Random

from evallab.sim.engine import Simulation
from evallab.task_lint import discover_tasks, lint_task
from evallab.tasks_parser import _tasks_lint_command

_HEX = "0123456789abcdef"
_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_FIXED_CODES = (
    "unpinned-image",
    "unpinned-clone",
    "unpinned-python-dependency",
    "verifier-network-download",
)
_EXTRA_CODES = (
    "verifier-isolation",
    "controls-declare-expectation",
    "artifacts-declared",
    "solution-present",
)
_SAFETY_CODES = (
    "artifacts-instructed",
    "artifacts-verified",
    "verifier-reads-undeclared",
    "artifact-overwrites-verifier",
    "verifier-executes-artifact",
    "verifier-installs-agent-dependencies",
    "verifier-runtime-install",
    "dropped-uid-reward-write",
)
_SAFETY_PER_SEED = 3
_TASK = 'artifacts = ["/app/out.txt"]\n\n[verifier]\nenvironment_mode = "separate"\n'
_SOLUTION = "#!/bin/sh\nset -eu\nprintf '%s\\n' ok > /app/out.txt\n"
_TESTS = "#!/bin/sh\nset -eu\ncat /app/out.txt\n"
_REWARD_TASK = (
    'artifacts = ["/app/out.txt", "/logs/verifier/reward.txt"]\n'
    '\n[verifier]\nenvironment_mode = "separate"\n'
)
_REWARD_INSTRUCTION = (
    "Write the final answer to /app/out.txt and the reward to /logs/verifier/reward.txt\n"
)
_DROPPED_UID_TESTS = (
    "#!/bin/sh\nset -eu\ncat /app/out.txt\n"
    "chmod 700 /logs/verifier\n"
    "setpriv --reuid nobody --regid nogroup --clear-groups /tests/run.sh\n"
    "printf '%s\\n' 1 > /logs/verifier/reward.txt\n"
)
_PARSE_CODES = (
    "task-toml-parses",
    "sources-parse",
    "dockerfile-copy-parses",
    "hidden-inputs-not-baked",
)
_INVALID_MANIFEST = 'artifacts = ["/app/out.txt"\n'
_BAD_COPY = "COPY /app/out.txt\n"
_HIDDEN_COPY = "COPY tests/ /app/tests/\n"
_UNPINNED_TASK = "FROM python:3.12-slim\n"
_BAD_SOURCES = {
    "shell": ("solution/broken.sh", '#!/bin/sh\nif [ -n "$x" ; then\n'),
    "python": ("solution/broken.py", "def broken(:\n    return\n"),
    "json": ("tests/broken.json", "{\n"),
}
_WRAPPERS = (
    "timeout 60 {}",
    "nice -n 5 {}",
    "stdbuf -oL {}",
    'bash -c "{}"',
    'sh -c "{}"',
    "`{}`",
    'bash -ec "{}"',
    'bash -lc "{}"',
    'sh -xc "{}"',
    'eval "{}"',
    "setsid {}",
    'su -c "{}"',
    "<<EOF\n{}\nEOF",
)


def _image(digest: str) -> str:
    return f"FROM python:3.12-slim@sha256:{digest}\n"


def _package_files(digest: str) -> dict[str, str]:
    return {
        "task.toml": _TASK,
        "instruction.md": "Write the final answer to /app/out.txt\n",
        "solution/solve.sh": _SOLUTION,
        "environment/Dockerfile": _image(digest),
        "tests/test.sh": _TESTS,
    }


def _wrapper(seed: int) -> Callable[[str], str]:
    return _WRAPPERS[seed % len(_WRAPPERS)].format


def _defects(
    digest: str, wrap: Callable[[str], str]
) -> dict[str, tuple[dict[str, str], tuple[str, ...]]]:
    image = _image(digest)
    setpriv = "setpriv --reuid nobody --regid nogroup --clear-groups /tests/run.sh"
    return {
        "unpinned-image": ({"environment/Dockerfile": "FROM python:3.12-slim\n"}, ()),
        "unpinned-clone": (
            {
                "environment/Dockerfile": image
                + "RUN "
                + wrap("git clone https://example.invalid/tools.git")
                + "\n"
            },
            (),
        ),
        "unpinned-python-dependency": (
            {"environment/Dockerfile": image + "RUN " + wrap("pip install httpx") + "\n"},
            (),
        ),
        "verifier-network-download": (
            {
                "tests/test.sh": _TESTS
                + wrap("curl -fsSL -o /tmp/payload https://example.invalid/payload")
                + "\n"
            },
            (),
        ),
        "verifier-isolation": ({"task.toml": 'artifacts = ["/app/out.txt"]\n'}, ()),
        "controls-declare-expectation": (
            {"controls/probe.sh": "#!/bin/sh\nset -eu\nprintf '%s\\n' ok\n"},
            (),
        ),
        "artifacts-declared": ({"task.toml": '[verifier]\nenvironment_mode = "separate"\n'}, ()),
        "solution-present": ({}, ("solution/solve.sh",)),
        "artifacts-instructed": ({"instruction.md": "Write the final answer.\n"}, ()),
        "artifacts-verified": (
            {"tests/test.sh": "#!/bin/sh\nset -eu\nprintf '%s\\n' ok\n"},
            (),
        ),
        "verifier-reads-undeclared": ({"tests/test.sh": _TESTS + "cat /app/other.txt\n"}, ()),
        "artifact-overwrites-verifier": (
            {
                "task.toml": _REWARD_TASK,
                "instruction.md": _REWARD_INSTRUCTION,
                "tests/test.sh": _TESTS + "cat /logs/verifier/reward.txt\n",
            },
            (),
        ),
        "verifier-executes-artifact": (
            {"tests/test.sh": "#!/bin/sh\nset -eu\n" + wrap("sh /app/out.txt") + "\n"},
            (),
        ),
        "verifier-installs-agent-dependencies": (
            {"tests/test.sh": _TESTS + wrap("xargs -a /app/out.txt pip install") + "\n"},
            (),
        ),
        "verifier-runtime-install": (
            {"tests/test.sh": _TESTS + wrap("apt-get update") + "\n"},
            (),
        ),
        "dropped-uid-reward-write": (
            {"tests/test.sh": _DROPPED_UID_TESTS.replace(setpriv, wrap(setpriv))},
            (),
        ),
    }


def _parse_defects(digest: str, source: str) -> dict[str, dict[str, str]]:
    relative, text = _BAD_SOURCES[source]
    return {
        "task-toml-parses": {"task.toml": _INVALID_MANIFEST},
        "sources-parse": {relative: text},
        "dockerfile-copy-parses": {"environment/Dockerfile": _image(digest) + _BAD_COPY},
        "hidden-inputs-not-baked": {"environment/Dockerfile": _image(digest) + _HIDDEN_COPY},
    }


def _combine(changes: list[dict[str, str]]) -> dict[str, str]:
    combined: dict[str, str] = {}
    for group in changes:
        for relative, text in group.items():
            assert relative == "environment/Dockerfile" or relative not in combined
            combined[relative] = combined.get(relative, "") + text
    return combined


def _clean_distractors(digest: str, wrap: Callable[[str], str]) -> dict[str, dict[str, str]]:
    image = _image(digest)
    return {
        "pinned-installs": {
            "environment/Dockerfile": image
            + "RUN "
            + wrap("pip install requests==2.32.3")
            + "\n"
            + "RUN "
            + wrap(f"git clone https://example.invalid/tools.git {_COMMIT}")
            + "\n"
        },
        "checksummed-download": {
            "tests/test.sh": _TESTS
            + "curl -fsSL -o /tmp/payload https://example.invalid/payload\n"
            + "sha256sum -c /tmp/checksums\n"
        },
        "declared-control": {
            "controls/probe.sh": "#!/bin/sh\n# expect: 0\nset -eu\nprintf '%s\\n' ok\n"
        },
    }


class LintSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step <= 160
        self.sim = sim
        self.root = root

    def safety_codes(self) -> list[str]:
        start = self.sim.seed % len(_SAFETY_CODES)
        return [
            _SAFETY_CODES[(start + offset) % len(_SAFETY_CODES)]
            for offset in range(_SAFETY_PER_SEED)
        ]

    def run(self) -> None:
        rng = self.sim.rng
        digest = "".join(rng.choice(_HEX) for _ in range(64))
        codes = [*_FIXED_CODES, *rng.sample(_EXTRA_CODES, 2), *self.safety_codes()]
        rng.shuffle(codes)
        wrap = _wrapper(self.sim.seed)
        defects = _defects(digest, wrap)
        assert set(codes) <= defects.keys()
        base = _package_files(digest)
        for index, code in enumerate(codes):
            changes, dropped = defects[code]
            package = self.write(f"defect-{index}", {**base, **changes}, dropped)
            self.check(package, (code,), "task_lint_reports_injected_code", code=code)
        self.parse_group(base, digest, rng)
        distractors = _clean_distractors(digest, wrap)
        chosen = rng.sample(sorted(distractors), 2)
        variants = {
            "plain": {},
            "distractors": {
                key: value
                for name in chosen
                for key, value in distractors[name].items()
            },
        }
        clean: list[Path] = []
        for name, changes in variants.items():
            package = self.write(f"clean-{name}", {**base, **changes})
            clean.append(package)
            self.check(package, (), "task_lint_clean_package", variant=name)
        self.discovery()
        self.cli(base, clean[0])

    def parse_group(self, base: dict[str, str], digest: str, rng: Random) -> None:
        source = rng.choice(sorted(_BAD_SOURCES))
        defect_map = _parse_defects(digest, source)
        assert set(defect_map) == set(_PARSE_CODES)
        for code in _PARSE_CODES:
            package = self.write(f"parse-{code}", {**base, **defect_map[code]})
            self.check(package, (code,), "task_lint_parse_code_injected", code=code)
        pair = rng.sample(list(_PARSE_CODES), 2)
        if rng.random() < 0.5:
            pair.reverse()
        changes = _combine([defect_map[code] for code in pair])
        package = self.write("parse-pair", {**base, **changes})
        self.check(package, tuple(pair), "task_lint_parse_pair_injected", codes=pair)

    def discovery(self) -> None:
        collection = self.root / "discovery"
        for relative in (
            "alpha/task.toml",
            "alpha/nested/task.toml",
            "beta/gamma/task.toml",
            "beta/gamma/deeper/task.toml",
            ".hidden/task.toml",
        ):
            manifest = collection / relative
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(_TASK, encoding="utf-8")
        self.sim.begin("lint-discovery", root=collection.name)
        found = [
            task.relative_to(collection).as_posix()
            for task in discover_tasks(
                [collection / "beta" / "gamma", collection, collection, collection / "alpha"]
            )
        ]
        expected = ["alpha", "beta/gamma"]
        self.sim.note(found=found)
        self.sim.require(found == expected, "task_lint_discovery_sorted_packages")

    def cli(self, base: dict[str, str], clean: Path) -> None:
        warned = self.write("cli-warning", {**base, "environment/Dockerfile": _UNPINNED_TASK})
        broken = self.write("cli-error", {**base, "task.toml": _INVALID_MANIFEST})
        sim = self.sim
        sim.begin("lint-cli", clean=clean.name, warned=warned.name, broken=broken.name)
        code, _ = self.invoke([clean])
        sim.require(code == 0, "task_lint_cli_clean_exit_zero")
        code, text = self.invoke([clean], json_output=True)
        sim.require(code == 0 and json.loads(text) == [], "task_lint_cli_clean_json")
        code, text = self.invoke([warned], json_output=True)
        severities = [finding["severity"] for finding in json.loads(text)]
        sim.require(code == 0, "task_lint_cli_warning_exit_zero")
        sim.require(severities == ["warning"], "task_lint_cli_warning_json")
        code, text = self.invoke([broken], json_output=True)
        severities = [finding["severity"] for finding in json.loads(text)]
        sim.require(code == 1, "task_lint_cli_error_exit_one")
        sim.require(severities == ["error"], "task_lint_cli_error_json")

    def invoke(self, paths: list[Path], json_output: bool = False) -> tuple[int, str]:
        buffer = io.StringIO()
        args = argparse.Namespace(paths=paths, json=json_output)
        with contextlib.redirect_stdout(buffer):
            code = _tasks_lint_command(args, self.root)
        return code, buffer.getvalue()

    def write(
        self, name: str, files: dict[str, str], dropped: tuple[str, ...] = ()
    ) -> Path:
        package = self.root / name
        for relative, text in files.items():
            if relative in dropped:
                continue
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return package

    def check(
        self,
        package: Path,
        expected: tuple[str, ...],
        invariant: str,
        **fields: object,
    ) -> None:
        sim = self.sim
        sim.begin("lint-task", package=package.name, expected=sorted(expected), **fields)
        found = sorted(finding.rule for finding in lint_task(package))
        sim.note(found=found)
        sim.require(found == sorted(expected), invariant)
