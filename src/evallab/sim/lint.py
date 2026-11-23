from __future__ import annotations

from pathlib import Path

from evallab.sim.engine import Simulation
from evallab.task_lint import lint_task

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


def _defects(digest: str) -> dict[str, tuple[dict[str, str], tuple[str, ...]]]:
    image = _image(digest)
    return {
        "unpinned-image": ({"environment/Dockerfile": "FROM python:3.12-slim\n"}, ()),
        "unpinned-clone": (
            {
                "environment/Dockerfile": image
                + "RUN git clone https://example.invalid/tools.git\n"
            },
            (),
        ),
        "unpinned-python-dependency": (
            {"environment/Dockerfile": image + "RUN pip install httpx\n"},
            (),
        ),
        "verifier-network-download": (
            {
                "tests/test.sh": _TESTS
                + "curl -fsSL -o /tmp/payload https://example.invalid/payload\n"
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
            {"tests/test.sh": "#!/bin/sh\nset -eu\nsh /app/out.txt\n"},
            (),
        ),
        "verifier-installs-agent-dependencies": (
            {"tests/test.sh": _TESTS + "xargs -a /app/out.txt pip install\n"},
            (),
        ),
        "verifier-runtime-install": ({"tests/test.sh": _TESTS + "apt-get update\n"}, ()),
        "dropped-uid-reward-write": ({"tests/test.sh": _DROPPED_UID_TESTS}, ()),
    }


def _clean_distractors(digest: str) -> dict[str, dict[str, str]]:
    image = _image(digest)
    return {
        "pinned-installs": {
            "environment/Dockerfile": image
            + "RUN pip install requests==2.32.3\n"
            + f"RUN git clone https://example.invalid/tools.git {_COMMIT}\n"
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
        defects = _defects(digest)
        assert set(codes) <= defects.keys()
        base = _package_files(digest)
        for index, code in enumerate(codes):
            changes, dropped = defects[code]
            package = self.write(f"defect-{index}", {**base, **changes}, dropped)
            self.check(package, (code,), "task_lint_reports_injected_code", code=code)
        distractors = _clean_distractors(digest)
        chosen = rng.sample(sorted(distractors), 2)
        variants = {
            "plain": {},
            "distractors": {
                key: value
                for name in chosen
                for key, value in distractors[name].items()
            },
        }
        for name, changes in variants.items():
            package = self.write(f"clean-{name}", {**base, **changes})
            self.check(package, (), "task_lint_clean_package", variant=name)

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
