"""Generate deterministic Harbor tasks from the pinned QuixBugs source tree."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)
PROGRAM_EXCLUSIONS = {"node"}
JAVA_HELPERS = ("Node.java", "WeightedEdge.java")
AUTHORS = (
    "Derrick Lin",
    "James Koppel",
    "Angela Chen",
    "Armando Solar-Lezama",
)


@dataclass(frozen=True, order=True)
class Target:
    task_id: str
    language: str
    program: str


class QuixBugsAdapter:
    """Convert both 40-program QuixBugs language variants to Harbor tasks."""

    def __init__(
        self,
        output_dir: Path,
        source_root: Path,
        source_url: str,
        source_ref: str,
        language: str = "all",
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.source_root = Path(source_root)
        self.source_url = source_url
        self.source_ref = source_ref
        self.language = language
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.template_dir = Path(__file__).parent / "task-template"

    def list_targets(self) -> list[Target]:
        python_programs = sorted(
            path.stem
            for path in (self.source_root / "python_programs").glob("*.py")
            if not path.stem.endswith("_test") and path.stem not in PROGRAM_EXCLUSIONS
        )
        java_programs = sorted(
            path.stem.lower()
            for path in (self.source_root / "java_programs").glob("*.java")
            if path.name not in JAVA_HELPERS
        )

        targets: list[Target] = []
        if self.language in {"all", "java"}:
            targets.extend(
                Target(f"quixbugs-java-{program}", "java", program)
                for program in java_programs
            )
        if self.language in {"all", "python"}:
            targets.extend(
                Target(f"quixbugs-python-{program}", "python", program)
                for program in python_programs
            )
        return sorted(targets)

    def select_targets(self) -> list[Target]:
        available = self.list_targets()
        by_id = {target.task_id: target for target in available}
        selected = available

        if self.task_ids:
            chosen: list[Target] = []
            for selector in self.task_ids:
                normalized = selector.strip().lower().replace("/", "-")
                if normalized in by_id:
                    chosen.append(by_id[normalized])
                    continue
                bare_matches = [target for target in available if target.program == normalized]
                if not bare_matches:
                    raise ValueError(
                        f"Unknown task ID {selector!r}; use a stable "
                        "quixbugs-<language>-<program> ID"
                    )
                chosen.extend(bare_matches)
            selected = sorted({target.task_id: target for target in chosen}.values())

        if self.limit is not None:
            selected = selected[: self.limit]
        return selected

    def run(self) -> list[str]:
        targets = self.select_targets()
        if not targets:
            raise ValueError("Selection produced no QuixBugs tasks")

        final_output = self.output_dir.resolve()
        final_output.parent.mkdir(parents=True, exist_ok=True)
        if final_output.exists() and any(final_output.iterdir()) and not self.overwrite:
            raise FileExistsError(
                f"{final_output} is not empty; pass --overwrite to replace the output dataset"
            )

        staging_parent = Path(
            tempfile.mkdtemp(prefix=f".{final_output.name}-", dir=final_output.parent)
        )
        staging_output = staging_parent / "generated"
        previous_output = staging_parent / "previous"
        generated: list[str] = []
        try:
            self.output_dir = staging_output
            staging_output.mkdir()
            for target in targets:
                self._generate_target(target)
                generated.append(target.task_id)
            self._write_generation_manifest(generated)

            if final_output.exists():
                final_output.replace(previous_output)
            try:
                staging_output.replace(final_output)
            except BaseException:
                if previous_output.exists() and not final_output.exists():
                    previous_output.replace(final_output)
                raise
            if previous_output.exists():
                shutil.rmtree(previous_output)
        finally:
            self.output_dir = final_output
            shutil.rmtree(staging_parent, ignore_errors=True)

        return generated

    def _generate_target(self, target: Target) -> None:
        task_root = self.output_dir / target.task_id
        if task_root.exists():
            raise FileExistsError(f"Duplicate generated task ID: {target.task_id}")

        task_root.mkdir(parents=True)
        shutil.copy2(self.source_root / "LICENSE", task_root / "LICENSE")
        self._write_task_toml(task_root, target)
        self._write_instruction(task_root, target)
        if target.language == "python":
            self._generate_python(task_root, target)
        else:
            self._generate_java(task_root, target)

    def _write_task_toml(self, task_root: Path, target: Target) -> None:
        author_rows = ",\n".join(f'    {{ name = "{name}" }}' for name in AUTHORS)
        if target.language == "python":
            artifact = f"/app/python_programs/{target.program}.py"
        else:
            artifact = f"/app/src/main/java/java_programs/{target.program.upper()}.java"
        content = (self.template_dir / "task.toml").read_text().format(
            task_id=target.task_id.removeprefix("quixbugs-"),
            language=target.language,
            program=target.program,
            authors=author_rows,
            source_ref=self.source_ref,
            artifact=artifact,
        )
        (task_root / "task.toml").write_text(content)

    def _write_instruction(self, task_root: Path, target: Target) -> None:
        if target.language == "python":
            path = f"/app/python_programs/{target.program}.py"
        else:
            path = f"/app/src/main/java/java_programs/{target.program.upper()}.java"
        content = (self.template_dir / "instruction.md").read_text().format(
            program=target.program,
            language=target.language,
            path=path,
        )
        (task_root / "instruction.md").write_text(content)

    def _generate_python(self, task_root: Path, target: Target) -> None:
        env_dir = task_root / "environment"
        package_dir = env_dir / "python_programs"
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("")
        self._copy_text_normalized(
            self.source_root / "python_programs" / f"{target.program}.py",
            package_dir / f"{target.program}.py",
        )
        shutil.copy2(
            self.template_dir / "environment" / "Dockerfile.python",
            env_dir / "Dockerfile",
        )

        correct = (
            self.source_root / "correct_python_programs" / f"{target.program}.py"
        ).read_text()
        self._write_solution(
            task_root / "solution" / "solve.sh",
            f"/app/python_programs/{target.program}.py",
            correct,
        )

        tests_dir = task_root / "tests"
        cases_dir = tests_dir / "python_testcases"
        json_dir = tests_dir / "json_testcases"
        cases_dir.mkdir(parents=True)
        shutil.copy2(self.template_dir / "tests" / "conftest.py", tests_dir / "conftest.py")
        for filename in ("load_testdata.py", "node.py", f"test_{target.program}.py"):
            self._copy_text_normalized(
                self.source_root / "python_testcases" / filename,
                cases_dir / filename,
            )
        json_case = self.source_root / "json_testcases" / f"{target.program}.json"
        if json_case.exists():
            json_dir.mkdir()
            self._copy_text_normalized(json_case, json_dir / json_case.name)
        shutil.copy2(
            self.template_dir / "tests" / "Dockerfile.python",
            tests_dir / "Dockerfile",
        )
        shutil.copy2(
            self.template_dir / "tests" / "requirements.lock",
            tests_dir / "requirements.lock",
        )
        test_script = (self.template_dir / "tests" / "test-python.sh").read_text().format(
            program=target.program
        )
        self._write_executable(tests_dir / "test.sh", test_script)

    def _generate_java(self, task_root: Path, target: Target) -> None:
        env_dir = task_root / "environment"
        source_dir = env_dir / "src" / "main" / "java" / "java_programs"
        source_dir.mkdir(parents=True)
        class_name = target.program.upper()
        buggy_path = self.source_root / "java_programs" / f"{class_name}.java"
        shutil.copy2(buggy_path, source_dir / buggy_path.name)

        test_path = (
            self.source_root / "java_testcases" / "junit" / f"{class_name}_TEST.java"
        )
        combined_text = buggy_path.read_text() + test_path.read_text()
        for helper in JAVA_HELPERS:
            helper_name = helper.removesuffix(".java")
            if re.search(rf"\b{re.escape(helper_name)}\b", combined_text):
                shutil.copy2(self.source_root / "java_programs" / helper, source_dir / helper)

        for filename in ("build.gradle", "settings.gradle"):
            shutil.copy2(self.template_dir / "environment" / filename, env_dir / filename)
        shutil.copy2(
            self.template_dir / "environment" / "Dockerfile.java",
            env_dir / "Dockerfile",
        )

        correct = (
            self.source_root / "correct_java_programs" / f"{class_name}.java"
        ).read_text()
        correct = re.sub(
            r"^package\s+correct_java_programs\s*;",
            "package java_programs;",
            correct,
            count=1,
            flags=re.MULTILINE,
        )
        self._write_solution(
            task_root / "solution" / "solve.sh",
            f"/app/src/main/java/java_programs/{class_name}.java",
            correct,
        )

        tests_dir = task_root / "tests"
        junit_dir = tests_dir / "java_testcases" / "junit"
        support_dir = tests_dir / "support" / "java_programs"
        junit_dir.mkdir(parents=True)
        support_dir.mkdir(parents=True)
        (support_dir / "package-info.java").write_text("package java_programs;\n")
        for helper in JAVA_HELPERS:
            helper_name = helper.removesuffix(".java")
            if re.search(rf"\b{re.escape(helper_name)}\b", combined_text):
                shutil.copy2(
                    self.source_root / "java_programs" / helper,
                    support_dir / helper,
                )
        test_file = self.source_root / "java_testcases" / "junit" / f"{class_name}_TEST.java"
        self._copy_text_normalized(test_file, junit_dir / test_file.name)
        if "QuixFixOracleHelper" in test_file.read_text():
            helper = self.source_root / "java_testcases" / "junit" / "QuixFixOracleHelper.java"
            self._copy_text_normalized(helper, junit_dir / helper.name)
        shutil.copy2(
            self.template_dir / "tests" / "Dockerfile.java",
            tests_dir / "Dockerfile",
        )
        shutil.copy2(
            self.template_dir / "tests" / "build.gradle.java",
            tests_dir / "build.gradle",
        )
        shutil.copy2(
            self.template_dir / "tests" / "settings.gradle",
            tests_dir / "settings.gradle",
        )
        test_script = (self.template_dir / "tests" / "test-java.sh").read_text().format(
            class_name=class_name
        )
        self._write_executable(tests_dir / "test.sh", test_script)

    @staticmethod
    def _copy_text_normalized(source: Path, destination: Path) -> None:
        """Copy upstream text with one deterministic trailing newline."""
        destination.write_text(source.read_text().rstrip() + "\n")

    @staticmethod
    def _write_solution(path: Path, destination: str, source: str) -> None:
        delimiter = "QUIXBUGS_REFERENCE_SOLUTION_EOF"
        if delimiter in source:
            raise ValueError("Unexpected heredoc delimiter in upstream source")
        script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"cat > {destination} <<'{delimiter}'\n"
            f"{source.rstrip()}\n"
            f"{delimiter}\n"
        )
        QuixBugsAdapter._write_executable(path, script)

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def _write_generation_manifest(self, generated: list[str]) -> None:
        digest = hashlib.sha256()
        for task_id in sorted(generated):
            task_root = self.output_dir / task_id
            for path in sorted(item for item in task_root.rglob("*") if item.is_file()):
                relative = path.relative_to(self.output_dir).as_posix()
                digest.update(relative.encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")

        manifest = {
            "adapter": "quixbugs",
            "source_url": self.source_url,
            "source_ref": self.source_ref,
            "selection": {
                "language": self.language,
                "limit": self.limit,
                "task_ids": self.task_ids,
            },
            "task_count": len(generated),
            "task_ids": sorted(generated),
            "task_tree_sha256": digest.hexdigest(),
        }
        (self.output_dir / "generation_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
