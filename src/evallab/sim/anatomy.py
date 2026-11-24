from __future__ import annotations

from pathlib import Path
from random import Random

from evallab.package_anatomy import (
    DockerInstruction,
    PatchFile,
    _is_executed_from_image,
    analyze_package,
    baked_files,
    dumps_card,
    parse_dockerfile,
    parse_unified_diff,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_HEX = "0123456789abcdef"
_JOINED_ARGS = "apt-get update &&" + " " * 6 + "apt-get install -y curl"
_BAKED_CASES = 12
_PACKAGE_SEEDS = 3
_Baked = tuple[str, list[DockerInstruction], list[str], dict[str, tuple[str, int]]]
_RUNNER_DIRS = ("tests", "ci", "checks", "spec")
_RUNNER_COMMANDS = ("pytest", "python -m pytest", "uv run pytest", "py.test")
_RUNNER_FLAGS = ("", " -q", " -x", " --tb short")
_RUNNER_FORMS = ("plain", "dot_slash", "hidden", "hidden_slash", "dot_slash_hidden")
_RUNNER_FILES = 2


def _token(rng: Random, count: int) -> str:
    assert 1 <= count <= 64
    return "".join(rng.choice(_HEX) for _ in range(count))


def _write(root: Path, files: dict[str, str]) -> None:
    assert root.name
    assert 1 <= len(files) <= 16
    for relative, text in files.items():
        assert relative and "\x00" not in text
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert root.is_dir()


def _runner_operand(form: str, name: str) -> tuple[str, bool]:
    assert form in _RUNNER_FORMS
    assert name in _RUNNER_DIRS
    operands = {
        "plain": (name, False),
        "dot_slash": (f"./{name}", False),
        "hidden": (f".{name}", True),
        "hidden_slash": (f".{name}/", True),
        "dot_slash_hidden": (f"./.{name}/", True),
    }
    assert len(operands) == len(_RUNNER_FORMS)
    return operands[form]


def _runner_baked(rng: Random, name: str) -> tuple[list[str], list[str]]:
    assert name in _RUNNER_DIRS
    visible = [f"/app/{name}/test_{_token(rng, 8)}.py" for _ in range(_RUNNER_FILES)]
    hidden = [f"/app/.{name}/test_{_token(rng, 8)}.py" for _ in range(_RUNNER_FILES)]
    assert len(set(visible) | set(hidden)) == 2 * _RUNNER_FILES
    return sorted(visible), sorted(hidden)


def _diff_text(marker: str) -> str:
    assert marker.isalnum() and len(marker) == 8
    rows = [
        "diff --git a/src/mod.py b/src/mod.py",
        "index 1111111..2222222 100644",
        "--- a/src/mod.py",
        "+++ b/src/mod.py",
        "@@ -1,3 +10,4 @@ def alpha():",
        " context-one",
        "-removed-one",
        f"+added-{marker}",
        " context-two",
        "@@ -20,2 +40,2 @@ def beta():",
        "-removed-two",
        f"+added-two-{marker}",
        "diff --git a/src/fresh.py b/src/fresh.py",
        "new file mode 100644",
        "index 0000000..3333333 100644",
        "--- /dev/null",
        "+++ b/src/fresh.py",
        "@@ -0,0 +1,2 @@ fresh ctx",
        "+created-a",
        f"+created-{marker}",
        "diff --git a/src/gone.py b/src/gone.py",
        "deleted file mode 100644",
        "index 4444444..0000000 100644",
        "--- a/src/gone.py",
        "+++ /dev/null",
        "@@ -5,1 +0,0 @@ gone ctx",
        "-deleted-line",
    ]
    assert len(rows) == 27
    assert rows[4].startswith("@@ -1,3 +10,4 @@")
    assert rows[9].startswith("@@ -20,2 +40,2 @@")
    assert rows[17].startswith("@@ -0,0 +1,2 @@")
    assert rows[25].startswith("@@ -5,1 +0,0 @@")
    return "\n".join(rows) + "\n"


def _require_modified(sim: Simulation, patch: PatchFile, marker: str) -> None:
    assert isinstance(patch, PatchFile)
    sim.require(patch.path == "src/mod.py", "anatomy_diff_modified_path")
    sim.require(patch.new_file is False, "anatomy_diff_modified_not_new")
    sim.require(patch.deleted is False, "anatomy_diff_modified_not_deleted")
    sim.require(len(patch.hunks) == 2, "anatomy_diff_modified_hunk_count")
    first, second = patch.hunks
    sim.require(first["start"] == 10, "anatomy_diff_first_start")
    sim.require(first["ctx"] == "def alpha():", "anatomy_diff_first_ctx")
    sim.require(first["patch_line"] == 5, "anatomy_diff_first_patch_line")
    sim.require(
        first["lines"]
        == [
            (" ", "context-one", 6),
            ("-", "removed-one", 7),
            ("+", f"added-{marker}", 8),
            (" ", "context-two", 9),
        ],
        "anatomy_diff_first_lines",
    )
    sim.require(second["start"] == 40, "anatomy_diff_second_start")
    sim.require(second["ctx"] == "def beta():", "anatomy_diff_second_ctx")
    sim.require(second["patch_line"] == 10, "anatomy_diff_second_patch_line")
    sim.require(
        second["lines"]
        == [
            ("-", "removed-two", 11),
            ("+", f"added-two-{marker}", 12),
        ],
        "anatomy_diff_second_lines",
    )
    sim.require(second["patch_line"] == first["lines"][-1][2] + 1, "anatomy_diff_hunk_resets_patch_line")
    sim.require(second["lines"][0][2] == second["patch_line"] + 1, "anatomy_diff_hunk_line_follows_header")
    sim.require(second["patch_line"] != second["start"], "anatomy_diff_patch_line_not_new_start")


def _require_created(sim: Simulation, patch: PatchFile, marker: str) -> None:
    assert isinstance(patch, PatchFile)
    sim.require(patch.path == "src/fresh.py", "anatomy_diff_new_path")
    sim.require(patch.new_file is True, "anatomy_diff_new_flag")
    sim.require(patch.deleted is False, "anatomy_diff_new_not_deleted")
    sim.require(len(patch.hunks) == 1, "anatomy_diff_new_hunk_count")
    hunk = patch.hunks[0]
    sim.require(hunk["start"] == 1, "anatomy_diff_new_start")
    sim.require(hunk["ctx"] == "fresh ctx", "anatomy_diff_new_ctx")
    sim.require(hunk["patch_line"] == 18, "anatomy_diff_new_patch_line")
    sim.require(
        hunk["lines"] == [("+", "created-a", 19), ("+", f"created-{marker}", 20)],
        "anatomy_diff_new_lines",
    )


def _require_deleted(sim: Simulation, patch: PatchFile) -> None:
    assert isinstance(patch, PatchFile)
    sim.require(patch.path == "src/gone.py", "anatomy_diff_deleted_path")
    sim.require(patch.new_file is False, "anatomy_diff_deleted_not_new")
    sim.require(patch.deleted is True, "anatomy_diff_deleted_flag")
    sim.require(len(patch.hunks) == 1, "anatomy_diff_deleted_hunk_count")
    hunk = patch.hunks[0]
    sim.require(hunk["start"] == 0, "anatomy_diff_deleted_start")
    sim.require(hunk["ctx"] == "gone ctx", "anatomy_diff_deleted_ctx")
    sim.require(hunk["patch_line"] == 26, "anatomy_diff_deleted_patch_line")
    sim.require(hunk["lines"] == [("-", "deleted-line", 27)], "anatomy_diff_deleted_lines")


def _dockerfile() -> str:
    text = (
        "run apt-get update && \\\n"
        "    apt-get install -y curl\n"
        "\n"
        "# comment kept out\n"
        "workdir   /app\n"
    )
    rows = text.splitlines()
    assert len(rows) == 5
    assert rows[0].endswith("\\")
    assert rows[1].startswith("    ")
    assert rows[2] == ""
    assert rows[3].startswith("#")
    assert rows[4].startswith("workdir")
    return text


def _baked_workdir_cases() -> list[_Baked]:
    return [
        (
            "absolute_workdir",
            [
                DockerInstruction(1, "WORKDIR", "/opt/app"),
                DockerInstruction(2, "COPY", "readme.txt notes.txt"),
            ],
            ["environment/readme.txt"],
            {"/opt/app/notes.txt": ("environment/readme.txt", 2)},
        ),
        (
            "relative_workdir",
            [
                DockerInstruction(1, "WORKDIR", "/opt"),
                DockerInstruction(2, "WORKDIR", "nested"),
                DockerInstruction(3, "COPY", "readme.txt out.txt"),
            ],
            ["environment/readme.txt"],
            {"/opt/nested/out.txt": ("environment/readme.txt", 3)},
        ),
        (
            "directory",
            [DockerInstruction(4, "COPY", "tests/ /app/tests/")],
            ["environment/tests/nested/a.py", "environment/tests/test.sh"],
            {
                "/app/tests/nested/a.py": ("environment/tests/nested/a.py", 4),
                "/app/tests/test.sh": ("environment/tests/test.sh", 4),
            },
        ),
        (
            "glob",
            [DockerInstruction(5, "COPY", "*.py /app/code/")],
            ["environment/a.py", "environment/b.py", "environment/c.txt"],
            {
                "/app/code/a.py": ("environment/a.py", 5),
                "/app/code/b.py": ("environment/b.py", 5),
            },
        ),
    ]


def _baked_form_cases() -> list[_Baked]:
    return [
        (
            "json_array",
            [DockerInstruction(6, "COPY", '["foo.txt", "/app/foo.txt"]')],
            ["environment/foo.txt"],
            {"/app/foo.txt": ("environment/foo.txt", 6)},
        ),
        (
            "from_stage",
            [DockerInstruction(7, "COPY", "--from=builder foo.txt /app/foo.txt")],
            ["environment/foo.txt"],
            {},
        ),
        (
            "two_sources",
            [DockerInstruction(8, "COPY", "a.txt b.txt /app/out/")],
            ["environment/a.txt", "environment/b.txt"],
            {
                "/app/out/a.txt": ("environment/a.txt", 8),
                "/app/out/b.txt": ("environment/b.txt", 8),
            },
        ),
        (
            "multi_source",
            [DockerInstruction(9, "COPY", "a.txt b.txt /app/out")],
            ["environment/a.txt", "environment/b.txt"],
            {
                "/app/out/a.txt": ("environment/a.txt", 9),
                "/app/out/b.txt": ("environment/b.txt", 9),
            },
        ),
    ]


def _baked_dot_cases() -> list[_Baked]:
    return [
        (
            "dotenv",
            [DockerInstruction(10, "COPY", ".env /app/.env")],
            ["environment/.env"],
            {"/app/.env": ("environment/.env", 10)},
        ),
        (
            "dot_dir",
            [DockerInstruction(11, "COPY", ".config /app/cfg/")],
            ["environment/.config/x"],
            {"/app/cfg/x": ("environment/.config/x", 11)},
        ),
        (
            "dot_slash",
            [DockerInstruction(12, "COPY", "./readme.txt /app/readme.txt")],
            ["environment/readme.txt"],
            {"/app/readme.txt": ("environment/readme.txt", 12)},
        ),
        (
            "copy_dot",
            [DockerInstruction(13, "COPY", ". /srv/")],
            [
                "environment/.config/x",
                "environment/.dockerignore",
                "environment/.env",
                "environment/Dockerfile",
                "environment/foo.txt",
            ],
            {
                "/srv/.config/x": ("environment/.config/x", 13),
                "/srv/.env": ("environment/.env", 13),
                "/srv/foo.txt": ("environment/foo.txt", 13),
            },
        ),
    ]


def _baked_cases() -> list[_Baked]:
    cases = _baked_workdir_cases()
    cases.extend(_baked_form_cases())
    cases.extend(_baked_dot_cases())
    assert len(cases) == _BAKED_CASES
    return cases


def _package_files(marker: str, digest: str) -> dict[str, str]:
    assert len(marker) == 12 and set(marker) <= set(_HEX)
    assert len(digest) == 64 and set(digest) <= set(_HEX)
    image = f"python:3.12-slim@sha256:{digest}"
    return {
        "task.toml": (
            'schema_version = "1"\n'
            "\n"
            "[task]\n"
            f'name = "sim-anatomy-{marker}"\n'
            "\n"
            "[environment]\n"
            'workdir = "/app"\n'
            f'docker_image = "{image}"\n'
            "\n"
            "[verifier]\n"
            "timeout_sec = 900.0\n"
        ),
        "instruction.md": f"Keep marker {marker} in the hidden assertion.\n",
        "environment/Dockerfile": (
            f"FROM {image}\n"
            "WORKDIR /app\n"
            "COPY .env /app/.env\n"
            "COPY tests/ /app/tests/\n"
        ),
        "environment/.env": f"MARKER={marker}\n",
        "environment/tests/test_hidden.py": "def test_baked():\n    assert True\n",
        "tests/test.sh": "#!/bin/sh\nset -eu\npython -m unittest discover -s /tests\n",
        "tests/test_hidden.py": (
            "def test_hidden():\n"
            f"    assert {marker!r} == {marker!r}\n"
            "    assert 1 + 1 == 2\n"
        ),
    }


def _require_package(
    sim: Simulation,
    root: Path,
    index: int,
    marker: str,
    digest: str,
    seen: list[str],
) -> None:
    assert 0 <= index < _PACKAGE_SEEDS
    assert len(seen) <= index
    files = _package_files(marker, digest)
    base = root / "packages" / str(index)
    one = base / "one" / "pkg"
    two = base / "two" / "pkg"
    assert one.name == two.name == "pkg"
    _write(one, files)
    _write(two, files)
    first = analyze_package(one)
    second = analyze_package(two)
    assert isinstance(first, dict) and isinstance(second, dict)
    card = dumps_card(first)
    other = dumps_card(second)
    assert isinstance(card, str) and isinstance(other, str) and card
    sim.require(card == other, "anatomy_card_identical")
    sim.require(first["package_digest"] == second["package_digest"], "anatomy_package_digest_identical")
    sim.require(first["task"] == "pkg" and second["task"] == "pkg", "anatomy_shared_basename")
    sim.require(digest in card, "anatomy_seed_in_card")
    sim.require(first["package_digest"] not in seen, "anatomy_seed_packages_differ")
    seen.append(first["package_digest"])


class AnatomySimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        self.check_diff()
        self.check_dockerfile()
        self.check_baked()
        self.check_determinism()
        self.check_runner()
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "anatomy_step_budget")

    def check_diff(self) -> None:
        marker = _token(self.rng, 8)
        text = _diff_text(marker)
        self.sim.begin("anatomy-diff", marker=marker)
        parsed = parse_unified_diff(text)
        self.sim.require(len(parsed) == 3, "anatomy_diff_file_count")
        _require_modified(self.sim, parsed[0], marker)
        _require_created(self.sim, parsed[1], marker)
        _require_deleted(self.sim, parsed[2])

    def check_dockerfile(self) -> None:
        sim = self.sim
        sim.begin("anatomy-dockerfile")
        parsed = parse_dockerfile(_dockerfile())
        assert len(parsed) <= 8
        sim.require(len(parsed) == 2, "anatomy_dockerfile_instruction_count")
        sim.require(parsed[0].line == 1, "anatomy_dockerfile_continuation_line")
        sim.require(parsed[0].op == "RUN", "anatomy_dockerfile_op_upper")
        sim.require(parsed[0].args == _JOINED_ARGS, "anatomy_dockerfile_joined_args")
        sim.require(parsed[1].line == 5, "anatomy_dockerfile_workdir_line")
        sim.require(parsed[1].op == "WORKDIR", "anatomy_dockerfile_workdir_op")
        sim.require(parsed[1].args == "/app", "anatomy_dockerfile_args_stripped")
        sim.require(
            all(item.line not in {2, 3, 4} for item in parsed),
            "anatomy_dockerfile_skips_blank_and_comment",
        )

    def check_runner(self) -> None:
        rng = self.rng
        forms = list(_RUNNER_FORMS)
        rng.shuffle(forms)
        name = rng.choice(_RUNNER_DIRS)
        visible, hidden = _runner_baked(rng, name)
        baked = [*visible, *hidden]
        rng.shuffle(baked)
        self.sim.begin("anatomy-runner", name=name, forms=",".join(forms))
        for form in forms:
            operand, is_hidden = _runner_operand(form, name)
            line = f"{rng.choice(_RUNNER_COMMANDS)} {operand}{rng.choice(_RUNNER_FLAGS)}"
            expected = hidden if is_hidden else visible
            assert expected and set(expected) <= set(baked)
            self.sim.require(_is_executed_from_image(line, baked) == expected, f"anatomy_runner_{form}")

    def check_baked(self) -> None:
        cases = _baked_cases()
        assert len(cases) == _BAKED_CASES
        for index, (label, instructions, env_paths, expected) in enumerate(cases):
            assert index < _BAKED_CASES
            assert label
            self.sim.begin("anatomy-baked", case=label)
            self.sim.require(baked_files(instructions, env_paths) == expected, f"anatomy_baked_{label}")

    def check_determinism(self) -> None:
        seen: list[str] = []
        for index in range(_PACKAGE_SEEDS):
            assert index < _PACKAGE_SEEDS
            marker = _token(self.rng, 12)
            digest = _token(self.rng, 64)
            self.sim.begin("anatomy-package", index=index, marker=marker)
            _require_package(self.sim, self.root, index, marker, digest, seen)
        assert len(seen) == _PACKAGE_SEEDS
