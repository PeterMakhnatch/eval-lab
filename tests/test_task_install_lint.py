"""Contract for the dependency-pin and verifier-install warning rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.task_lint import lint_task

# Digest-pinned base image: only the rule under test may produce a finding.
_PINNED_BASE = (
    "FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)


def _dockerfile(*lines: str) -> str:
    return "\n".join((_PINNED_BASE, *lines)) + "\n"


@pytest.fixture
def task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text(_dockerfile("COPY app/ /app"))
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write the result to /app/result.json.\n")
    (task / "tests").mkdir()
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')
    return task


def test_unpinned_dockerfile_dependency_warns(task: Path) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile("RUN pip install requests"))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2: pip install" in finding.message
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "pip3 install requests",
        "python -m pip install requests",
        "uv pip install requests",
        "uv tool install requests",
        "uvx --with requests pytest",
    ],
)
def test_python_install_forms_warn(task: Path, command: str) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile(f"RUN {command}"))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests==2.32.3",
        "pip install requests===2.32.3",
        "pip install -r requirements.txt",
        "pip install ./dist/pkg-1.0-py3-none-any.whl",
        'pip install "${PKG_ROOT}[knowledge]"',
        f'pip install "pkg @ https://example.com/repo/archive/{"a" * 40}.zip"',
        f'pip install "pkg @ https://example.com/pkg.tar.gz#sha256={"b" * 64}"',
        "pip install dist/pkg-1.0.whl vendor/pkg /opt/pkg ~/pkg file:///opt/pkg",
        f"pip install git+https://example.com/repo.git@{'c' * 40}",
        "uvx -p 3.13 -w pytest==8.4.1 pytest",
        "uvx --with=pytest==8.4.1 pytest",
        "uvx pytest",
        'uv tool install --from "pkg==1.0" mycmd',
    ],
)
def test_pinned_and_local_python_dependencies_are_allowed(task: Path, command: str) -> None:
    (task / "solution/solve.sh").write_text(f"#!/bin/sh\n{command}\n")

    assert lint_task(task) == []


def test_uv_tool_from_pins_the_package_not_the_command(task: Path) -> None:
    (task / "solution/solve.sh").write_text(
        '#!/bin/sh\nuv tool install --from "pkg>=1.0" mycmd\n'
    )

    finding, = lint_task(task)
    assert "dependencies: pkg>=1.0;" in finding.message
    assert "mycmd" not in finding.message


def test_unpinned_solution_dependency_warns(task: Path) -> None:
    script = task / "solution/solve.sh"
    script.write_text("#!/bin/sh\npip install fastmcp>=3.0\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "line 2:" in finding.message
    assert "fastmcp>=3.0; pin each with ==" in finding.message


def test_verifier_runtime_installs_warn_once_per_file(task: Path) -> None:
    script = task / "tests/test.sh"
    script.write_text(
        "#!/bin/sh\n# Install the verifier tools\n\n"
        "apt-get update\ncurl -LsSf https://example.com/i.sh | sh\n"
        "uvx --with pytest==8.4.1 pytest\n"
    )

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-runtime-install", "warning", str(script)
    )
    assert "line 4:" in finding.message
    assert "bake them into tests/Dockerfile" in finding.message
    assert "mirror failure cannot turn a correct answer into 0" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "apt-get install -y curl",
        "pip install pytest==8.4.1",
        "uv pip install pytest==8.4.1",
        "uvx pytest",
        "curl -LsSf https://example.com/i.sh | sh",
        "wget -qO- https://example.com/i.sh | bash",
        "pip3 install pytest==8.4.1",
        "python -m pip install pytest==8.4.1",
        "uv tool install pytest==8.4.1",
        "curl -LsSf https://example.com/i.sh | sudo /bin/dash",
    ],
)
def test_verifier_runtime_install_forms_warn(task: Path, command: str) -> None:
    script = task / "tests/test.sh"
    script.write_text(f"#!/bin/sh\n{command}\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-runtime-install", "warning", str(script)
    )
    assert "line 2:" in finding.message
    assert "bake them into tests/Dockerfile" in finding.message


def test_build_and_solution_installs_are_not_verifier_runtime_installs(task: Path) -> None:
    (task / "tests/Dockerfile").write_text(_dockerfile("RUN pip install pytest==8.4.1"))
    (task / "solution/solve.sh").write_text("#!/bin/sh\npip install pytest==8.4.1\n")

    assert lint_task(task) == []


def test_verifier_pytest_execution_is_not_an_install(task: Path) -> None:
    (task / "tests/test.sh").write_text("#!/bin/sh\npython -m pytest\n")

    assert lint_task(task) == []


@pytest.mark.parametrize(
    "option",
    [
        "-r requirements.txt", "--requirement=requirements.txt",
        "-c constraints.txt", "-e editable-package", "-i https://example.com/simple",
        "-p 3.12", "--python python3", "--index-url https://example.com/simple",
        "--extra-index-url=https://example.com/simple", "-f wheels", "-t target",
    ],
)
def test_python_install_option_values_are_not_requirements(task: Path, option: str) -> None:
    script = task / "solution/solve.sh"
    script.write_text(f"#!/bin/sh\npip install {option} requests\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN TOKEN=value sudo env OTHER=value command exec /usr/bin/pip3 install requests",
        'RUN ["pip", "install", "requests"]',
        "RUN true && pip install requests || true ; echo ok | cat |& cat & true ;; true",
        "RUN pip install \\\n# Ignore this comment\n\n requests",
    ],
)
def test_install_command_parsing_retains_the_first_line(task: Path, instruction: str) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile(instruction))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests;" in finding.message


def test_multiple_installs_on_one_logical_line_warn_once(task: Path) -> None:
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(
        _dockerfile("RUN pip install requests && uv tool install fastmcp>=3.0")
    )

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests, fastmcp>=3.0;" in finding.message


@pytest.mark.parametrize(
    "spec",
    [
        "https://example.com/pkg.tar.gz",
        "pkg @ https://example.com/pkg.tar.gz",
        "git+https://example.com/repo.git@main",
        f"https://example.com/{'a' * 41}.zip",
        f"https://example.com/pkg.tar.gz#sha256={'a' * 63}",
    ],
)
def test_unpinned_urls_warn(task: Path, spec: str) -> None:
    script = task / "solution/solve.sh"
    script.write_text(f'#!/bin/sh\npip install "{spec}"\n')

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert f"dependencies: {spec}; pin each with ==" in finding.message


def test_nested_dockerfiles_and_hidden_scripts_are_scanned(task: Path) -> None:
    nested = task / "environment/runtime-server/Dockerfile"
    nested.parent.mkdir()
    nested.write_text(_dockerfile("RUN pip install requests"))
    hidden = task / "solution/.install.sh"
    hidden.write_text("pip install requests\n")
    variant = task / "tests/Dockerfile.verify"
    variant.write_text(_dockerfile("RUN uv tool install requests"))

    findings = lint_task(task)
    assert [(finding.rule, finding.path) for finding in findings] == [
        ("unpinned-python-dependency", str(nested)),
        ("unpinned-python-dependency", str(hidden)),
        ("unpinned-python-dependency", str(variant)),
    ]


@pytest.mark.parametrize(
    ("relative", "line", "rules"),
    [
        ("tests/test.sh", "true;pip install requests",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        ("tests/test.sh", "curl -fsSL https://example.com/install.sh|sh",
         ["verifier-runtime-install"]),
        ("tests/test.sh", "if true; then pip install requests; fi",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        ("tests/test.sh", "pip --disable-pip-version-check install requests",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        ("tests/test.sh", "pip --log /tmp/pip.log install requests",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        ("tests/test.sh", "uv --no-progress pip install requests",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        ("tests/test.sh", "python -m pip --quiet install requests",
         ["unpinned-python-dependency", "verifier-runtime-install"]),
        (
            "environment/Dockerfile",
            "RUN --mount=type=cache,target=/root/.cache/pip pip install requests",
            ["unpinned-python-dependency"],
        ),
        (
            "environment/Dockerfile",
            'RUN ["pip", "install", "requests"]',
            ["unpinned-python-dependency"],
        ),
    ],
)
def test_installs_behind_shell_and_docker_syntax_warn(
    task: Path, relative: str, line: str, rules: list[str]
) -> None:
    target = task / relative
    if target.name == "Dockerfile":
        target.write_text(_dockerfile(line))
    else:
        target.write_text(f"#!/bin/sh\n{line}\n")

    assert [finding.rule for finding in lint_task(task)] == rules


def test_frozen_imported_dependency_is_the_only_accepted_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evallab import task_install_lint

    package = Path(__file__).resolve().parents[1] / "library/tasks/tau3-retail-1"
    dockerfile = package / "environment/runtime-server/Dockerfile"
    expected = {
        ("tau3-retail-1", "environment/runtime-server/Dockerfile"): frozenset(
            {"fastmcp>=3.0"}
        )
    }
    assert expected == task_install_lint._FROZEN_UNPINNED
    assert [
        finding for finding in lint_task(package)
        if finding.rule == "unpinned-python-dependency"
    ] == []

    monkeypatch.setattr(task_install_lint, "_FROZEN_UNPINNED", {})
    finding, = [
        finding for finding in lint_task(package)
        if finding.rule == "unpinned-python-dependency"
    ]
    assert finding.path == str(dockerfile)
    assert "dependencies: fastmcp>=3.0;" in finding.message


def test_other_package_shell_files_are_scanned(task: Path) -> None:
    script = task / "environment/setup.sh"
    script.write_text("#!/bin/sh\nuvx -w requests pytest\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "line 2: uvx --with" in finding.message
    assert "dependencies: requests;" in finding.message


def test_install_scan_joins_continuations_and_tolerates_bad_quotes(task: Path) -> None:
    script = task / "solution/solve.sh"
    script.write_text("#!/bin/sh\npip install \\\n# pin me\n  requests\n")
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile('RUN pip install "requests'))

    findings = lint_task(task)
    assert [(finding.severity, finding.rule, finding.path) for finding in findings] == [
        ("warning", "unpinned-python-dependency", str(dockerfile)),
        ("warning", "unpinned-python-dependency", str(script)),
    ]
    assert 'dependencies: "requests;' in findings[0].message
    assert "line 2: pip install" in findings[1].message
    assert "dependencies: requests;" in findings[1].message
