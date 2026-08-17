"""Contracts for the deterministic craft scanner.

Every task directory used here is built in `tmp_path`: nothing reads the real
TB3 corpus, `library/`, the network, the clock, or a database, so the suite
holds on a clean CI runner (`agents/CHECKS.md`, deterministic-test rule).
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab import craft

MANIFEST = """\
schema_version = "1.0"

[task]
name = "{name}"
{version}

[metadata]
category = "Test"
{expert}

[verifier]
timeout_sec = 60.0
{mode}
"""


def make_task(
    root: Path,
    name: str,
    *,
    instruction: str = "Do the thing.\n",
    version: str | None = None,
    expert_hours: float | None = None,
    separate: bool = True,
    dockerfile: str | None = "FROM python:3.12-slim\n",
    environment: dict[str, str] | None = None,
    tests: dict[str, str] | None = None,
    solution: dict[str, str] | None = None,
    compose: str | None = None,
) -> Path:
    """Write a minimal Harbor-shaped task directory and return it."""
    task_dir = root / name
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        MANIFEST.format(
            name=name,
            version="" if version is None else f'version = "{version}"',
            expert=(
                ""
                if expert_hours is None
                else f"expert_time_estimate_hours = {expert_hours}"
            ),
            mode='environment_mode = "separate"' if separate else "",
        )
    )
    (task_dir / "instruction.md").write_text(instruction)
    env_dir = task_dir / "environment"
    env_dir.mkdir()
    if dockerfile is not None:
        (env_dir / "Dockerfile").write_text(dockerfile)
    if compose is not None:
        (env_dir / "docker-compose.yaml").write_text(compose)
    for relative, content in (environment or {}).items():
        target = env_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    for relative, content in (tests or {}).items():
        target = tests_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    solution_dir = task_dir / "solution"
    solution_dir.mkdir()
    for relative, content in (solution or {}).items():
        target = solution_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return task_dir


def source_for(root: Path) -> craft.TaskSource:
    return craft.TaskSource(root=root, source_repo="test/corpus", label="test")


def scan_one(task_dir: Path) -> craft.CraftRecord:
    return craft.scan_task(task_dir, source_for(task_dir.parent))


# --------------------------------------------------------------------------- #
# rule 2: structure, not text
# --------------------------------------------------------------------------- #


def test_runner_named_only_in_a_comment_is_not_evidence(tmp_path: Path) -> None:
    """The defect substring search would introduce, held down by a test.

    59 of the 74 TB3 `test.sh` files name their runner in prose as well as in
    code; a scanner that greps cannot tell a documented runner from an invoked
    one, and would report a shell answer-comparison as a pytest verifier.
    """
    task = make_task(
        tmp_path,
        "commented",
        tests={
            "test.sh": (
                "#!/bin/bash\n"
                "# The pytest call lives in an if condition so set -e does not abort.\n"
                'echo 1 > /logs/verifier/reward.txt\n'
            )
        },
    )

    record = scan_one(task)

    assert "pytest" not in record.verifier_signals
    assert record.verifier_signals == ["shell_only"]
    assert record.verifier_type is None


def test_invoked_runner_is_evidence(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "invoked",
        tests={"test.sh": "#!/bin/bash\npython3 -m pytest /tests/checks.py\n"},
    )

    assert scan_one(task).verifier_type == "pytest"


def test_pytest_detected_from_ast_regardless_of_filename(tmp_path: Path) -> None:
    """`def test_*` in a module named `helpers.py` still collects under pytest."""
    task = make_task(
        tmp_path,
        "ast-named",
        tests={"helpers.py": "def test_reward():\n    assert True\n"},
    )

    assert scan_one(task).verifier_type == "pytest"


def test_test_named_module_without_a_test_callable_is_not_pytest(tmp_path: Path) -> None:
    """The converse: a filename is not a mechanism."""
    task = make_task(
        tmp_path,
        "misnamed",
        tests={"test_score.py": "import json\n\nprint(json.dumps({'reward': 1}))\n"},
    )

    record = scan_one(task)

    assert record.verifier_type is None
    assert record.verifier_signals == ["scorer_script"]


def test_golden_file_requires_a_reference_that_exists(tmp_path: Path) -> None:
    """A filename in a string constant is only evidence if the file is there."""
    missing = make_task(
        tmp_path / "a",
        "no-reference",
        tests={"check.py": 'import json\n\ndef test_x():\n    json.load(open("expected.json"))\n'},
    )
    present = make_task(
        tmp_path / "b",
        "with-reference",
        tests={
            "check.py": (
                'import json\n\ndef test_x():\n    json.load(open("/tests/expected.json"))\n'
            ),
            "expected.json": '{"answer": 1}\n',
        },
    )

    assert craft.scan_task(missing, source_for(tmp_path / "a")).verifier_type == "pytest"
    record = craft.scan_task(present, source_for(tmp_path / "b"))
    assert record.verifier_type == "hybrid"
    assert record.verifier_signals == ["golden_file", "pytest"]


def test_reference_bound_to_a_shell_variable_is_found(tmp_path: Path) -> None:
    """The three FreeCAD tasks address their held-back reference this way."""
    task = make_task(
        tmp_path,
        "shell-bound",
        tests={
            "test.sh": (
                "#!/bin/bash\n"
                'REFERENCE="/opt/grader/reference.FCStd"\n'
                'python3 /tests/run_scorer.py --reference "$REFERENCE"\n'
            ),
            "run_scorer.py": "import sys\n\nsys.exit(0)\n",
            "reference.FCStd": "binary-ish\n",
        },
    )

    record = scan_one(task)

    assert "golden_file" in record.verifier_signals
    assert record.verifier_type == "golden_file"


def test_js_runner_alone_is_unclassified_but_recorded(tmp_path: Path) -> None:
    """The enum has no label for a vitest verifier, so the label is null.

    Calling it `pytest` would be the confidently-wrong answer; the mechanism is
    still recorded, so the null can be explained without re-reading the task.
    """
    task = make_task(
        tmp_path,
        "vitest",
        tests={
            "package.json": json.dumps({"devDependencies": {"vitest": "^2.0.0"}}),
            "app.test.ts": "it('works', () => {});\n",
        },
    )

    record = scan_one(task)

    assert record.verifier_signals == ["unit_js"]
    assert record.verifier_type is None
    assert "verifier_type" in record.unresolved_facets


def test_js_runner_beside_a_golden_file_is_hybrid(tmp_path: Path) -> None:
    """Two mechanisms is `hybrid` even when the enum can only name one of them."""
    task = make_task(
        tmp_path,
        "vitest-golden",
        tests={
            "package.json": json.dumps({"devDependencies": {"playwright": "1.47.0"}}),
            "check.sh": "#!/bin/bash\nnpx playwright test /tests/baseline.json\n",
            "baseline.json": "{}\n",
        },
    )

    record = scan_one(task)

    assert record.verifier_signals == ["golden_file", "unit_js"]
    assert record.verifier_type == "hybrid"


def test_a_stray_test_file_without_a_declared_runner_is_not_a_runner(tmp_path: Path) -> None:
    """`unit_js` comes from the dependency table, never from a file name."""
    task = make_task(
        tmp_path,
        "no-runner",
        tests={
            "package.json": json.dumps({"dependencies": {"zod": "3.23.8"}}),
            "app.test.ts": "it('works', () => {});\n",
        },
    )

    assert "unit_js" not in scan_one(task).verifier_signals


def test_unparseable_verifier_module_is_not_mistaken_for_a_mechanism(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "broken",
        tests={"check.py": "def test_x(:\n    pass\n"},
    )

    evidence = craft.inspect_verifier(task / "tests")

    assert evidence.modules_unparsed == 1
    assert evidence.modules_parsed == 0
    assert "pytest" not in evidence.families


def test_judge_is_detected_from_a_client_import(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "judged",
        tests={"check.py": "import openai\n\n\ndef grade():\n    return openai\n"},
    )

    assert scan_one(task).verifier_type == "judge"


# --------------------------------------------------------------------------- #
# rule 1: never guess a facet
# --------------------------------------------------------------------------- #


def test_llm_only_facets_are_null_and_say_so(tmp_path: Path) -> None:
    record = scan_one(make_task(tmp_path, "sparse"))

    assert record.instruction_style is None
    assert record.difficulty_mechanism is None
    assert "instruction_style" in record.unresolved_facets
    assert "difficulty_mechanism" in record.unresolved_facets
    assert set(craft.LLM_ONLY_FACETS) == {"instruction_style", "difficulty_mechanism"}


def test_human_minutes_comes_from_the_stated_expert_estimate(tmp_path: Path) -> None:
    stated = make_task(tmp_path / "a", "stated", expert_hours=7.0)
    silent = make_task(tmp_path / "b", "silent")

    assert craft.scan_task(stated, source_for(tmp_path / "a")).human_minutes == 420
    record = craft.scan_task(silent, source_for(tmp_path / "b"))
    assert record.human_minutes is None
    assert "human_minutes" in record.unresolved_facets


def test_version_is_the_task_version_not_the_schema_version(tmp_path: Path) -> None:
    """`schema_version` describes the file format, not the task, so it is not it.

    Every TB3 task carries `schema_version` and none carries `[task].version`;
    reporting the former as `version` would fill the column with a fact about
    Harbor's manifest format and call it task provenance.
    """
    pinned = make_task(tmp_path / "a", "pinned", version="2.1")
    unpinned = make_task(tmp_path / "b", "unpinned")

    assert craft.scan_task(pinned, source_for(tmp_path / "a")).version == "2.1"
    record = craft.scan_task(unpinned, source_for(tmp_path / "b"))
    assert record.version is None
    assert "version" in record.unresolved_facets
    assert 'schema_version = "1.0"' in (unpinned / "task.toml").read_text()


def test_no_environment_leaves_service_facets_null(tmp_path: Path) -> None:
    task = make_task(tmp_path, "envless", dockerfile=None)

    record = scan_one(task)

    assert record.env_services_n is None
    assert record.env_multi_container is None
    assert "env_services_n" in record.unresolved_facets
    assert "env_multi_container" in record.unresolved_facets


def test_ambiguous_extension_contributes_no_language(tmp_path: Path) -> None:
    """`.v` is Coq and Verilog; without sibling evidence it is neither."""
    bare = make_task(tmp_path / "a", "bare-v", environment={"Main.v": "Theorem t.\n"})
    coq = make_task(
        tmp_path / "b",
        "coq",
        environment={"Main.v": "Theorem t.\n", "_CoqProject": "-Q . Top\n"},
    )

    assert craft.scan_task(bare, source_for(tmp_path / "a")).env_languages == []
    assert craft.scan_task(coq, source_for(tmp_path / "b")).env_languages == ["coq"]


def test_dockerfile_is_not_reported_as_a_language(tmp_path: Path) -> None:
    """Every environment has one, so recording it would make the column constant."""
    task = make_task(tmp_path, "python-env", environment={"app/main.py": "print(1)\n"})

    assert scan_one(task).env_languages == ["python"]


# --------------------------------------------------------------------------- #
# dependency pinning
# --------------------------------------------------------------------------- #


def test_version_pins_survive_the_shell_lexer(tmp_path: Path) -> None:
    """Splitting every `=` shreds `httpx==0.27.2` and reports a pin as unpinned."""
    task = make_task(
        tmp_path,
        "pinned-pip",
        dockerfile=(
            "FROM python:3.12-slim\n"
            "RUN python3 -m pip install --no-cache-dir httpx==0.27.2 rdflib==7.1.4\n"
        ),
    )

    assert "httpx==0.27.2" in craft.shell_words("pip install httpx==0.27.2")
    assert scan_one(task).pinned_deps is True


def test_unpinned_apt_packages_are_reported_unpinned(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "loose-apt",
        dockerfile="FROM debian:bookworm\nRUN apt-get update && apt-get install -y git curl\n",
    )

    assert scan_one(task).pinned_deps is False


def test_no_dependency_declaration_is_null_not_false(tmp_path: Path) -> None:
    """`False` would read as "declared and unpinned", which is a different fact."""
    task = make_task(tmp_path, "no-deps", dockerfile="FROM python:3.12-slim\nWORKDIR /app\n")

    record = scan_one(task)

    assert record.pinned_deps is None
    assert record.base_image_pin == "tag"


def test_requirements_reference_defers_to_the_referenced_file(tmp_path: Path) -> None:
    pinned = make_task(
        tmp_path / "a",
        "req-pinned",
        dockerfile="FROM python:3.12-slim\nRUN pip install -r requirements.txt\n",
        environment={"requirements.txt": "httpx==0.27.2\nrdflib==7.1.4\n"},
    )
    loose = make_task(
        tmp_path / "b",
        "req-loose",
        dockerfile="FROM python:3.12-slim\nRUN pip install -r requirements.txt\n",
        environment={"requirements.txt": "httpx>=0.27\n"},
    )

    assert craft.scan_task(pinned, source_for(tmp_path / "a")).pinned_deps is True
    assert craft.scan_task(loose, source_for(tmp_path / "b")).pinned_deps is False


def test_lockfile_respecting_installer_counts_as_pinned(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "npm-ci",
        dockerfile="FROM node:22-slim\nRUN npm ci --no-audit\n",
        environment={"package-lock.json": "{}\n"},
    )

    assert scan_one(task).pinned_deps is True


def test_continuation_lines_are_one_instruction(tmp_path: Path) -> None:
    """A folded `RUN` must be lexed whole or its packages disappear."""
    task = make_task(
        tmp_path,
        "folded",
        dockerfile=(
            "FROM python:3.12-slim\nRUN pip install \\\n    numpy==2.1.0 \\\n    scipy==1.14.0\n"
        ),
    )

    assert scan_one(task).pinned_deps is True


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("python@sha256:" + "a" * 64, "digest"),
        ("python:3.12-slim", "tag"),
        ("scratch", "bare"),
    ],
)
def test_base_image_pin_classes(tmp_path: Path, reference: str, expected: str) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"# comment\nFROM {reference}\nRUN true\n")

    assert craft.base_image_pin(dockerfile) == expected


def test_base_image_pin_reads_the_first_stage(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python@sha256:" + "b" * 64 + " AS build\nFROM python:3.12\n")

    assert craft.base_image_pin(dockerfile) == "digest"


# --------------------------------------------------------------------------- #
# anti-cheat and environment shape
# --------------------------------------------------------------------------- #


def test_hidden_tests_requires_the_separate_verifier_declaration(tmp_path: Path) -> None:
    """`task_workbench.py` treats a missing `environment_mode` as not isolated."""
    isolated = make_task(tmp_path / "a", "isolated", separate=True)
    shared = make_task(tmp_path / "b", "shared", separate=False)

    isolated_record = craft.scan_task(isolated, source_for(tmp_path / "a"))
    shared_record = craft.scan_task(shared, source_for(tmp_path / "b"))

    assert isolated_record.anti_cheat == ["hidden_tests", "answer_outside_image"]
    assert isolated_record.answer_hiding == "separate_verifier_image"
    assert shared_record.anti_cheat == []
    assert shared_record.answer_hiding is None


def test_digest_and_process_checks_come_from_the_verifier_itself(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "guarded",
        tests={
            "check.py": (
                "import hashlib\nimport psutil\n\n\ndef test_x():\n"
                "    assert hashlib.sha256(b'').hexdigest()\n"
                "    assert psutil.pids()\n"
            )
        },
    )

    record = scan_one(task)

    assert "digest_check" in record.anti_cheat
    assert "process_check" in record.anti_cheat


def test_signed_expectations_widen_answer_hiding(tmp_path: Path) -> None:
    task = make_task(
        tmp_path,
        "signed",
        tests={
            "check.py": (
                "import hmac\nimport json\n\n\ndef test_x():\n"
                '    json.load(open("/tests/expected.json"))\n'
                "    assert hmac.new(b'k', b'', 'sha256')\n"
            ),
            "expected.json": "{}\n",
        },
    )

    assert scan_one(task).answer_hiding == (
        "separate_verifier_image+reference_artifact_in_tests+signed_expectations"
    )


def test_multi_container_comes_from_the_compose_service_count(tmp_path: Path) -> None:
    single = make_task(tmp_path / "a", "single")
    multi = make_task(
        tmp_path / "b",
        "multi",
        compose="services:\n  app:\n    build: .\n  db:\n    image: postgres:16\n",
    )

    single_record = craft.scan_task(single, source_for(tmp_path / "a"))
    multi_record = craft.scan_task(multi, source_for(tmp_path / "b"))

    assert (single_record.env_services_n, single_record.env_multi_container) == (1, False)
    assert (multi_record.env_services_n, multi_record.env_multi_container) == (2, True)


def test_instruction_chars_counts_characters(tmp_path: Path) -> None:
    task = make_task(tmp_path, "measured", instruction="Résumé the run.\n")

    assert scan_one(task).instruction_chars == 16


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discovery_needs_both_manifest_and_instruction(tmp_path: Path) -> None:
    """The TB3 root holds 76 entries and 74 tasks; `README.md` is not one."""
    make_task(tmp_path, "real")
    (tmp_path / "README.md").write_text("# corpus\n")
    (tmp_path / "dataset.toml").write_text('[dataset]\nname = "x/y"\n')
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "task.toml").write_text("[task]\nname = \"partial\"\n")

    assert [path.name for path in craft.discover_tasks(tmp_path)] == ["real"]


def test_unparseable_manifest_is_skipped_with_a_reason(tmp_path: Path) -> None:
    """The QuixBugs task template is a `{placeholder}` file that is not TOML."""
    make_task(tmp_path, "good")
    template = tmp_path / "template"
    template.mkdir()
    (template / "task.toml").write_text("[task]\nauthors = [\n{authors}\n]\n")
    (template / "instruction.md").write_text("Repair {program}.\n")

    result = craft.scan([source_for(tmp_path)])

    assert [record.task_ref for record in result.records] == ["good"]
    assert len(result.skipped) == 1
    assert "does not parse" in result.skipped[0].reason
    assert result.skipped[0].path.endswith("template")


def test_task_ref_falls_back_to_the_relative_path(tmp_path: Path) -> None:
    task = make_task(tmp_path, "named")
    (task / "task.toml").write_text('[task]\ndescription = ""\n\n[verifier]\ntimeout_sec = 1.0\n')

    assert scan_one(task).task_ref == "named"


def test_source_repo_comes_from_the_dataset_manifest(tmp_path: Path) -> None:
    (tmp_path / "dataset.toml").write_text('[dataset]\nname = "terminal-bench/terminal-bench"\n')

    assert craft.tb3_source(tmp_path).source_repo == "terminal-bench/terminal-bench"
    assert craft.tb3_source(tmp_path / "absent").source_repo == craft.TB3_FALLBACK_SOURCE_REPO


def test_tb3_root_is_injectable(tmp_path: Path) -> None:
    assert craft.tb3_root(environ={craft.TB3_ROOT_ENV: str(tmp_path)}) == tmp_path.resolve()
    assert craft.tb3_root(tmp_path, environ={}) == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# digests, idempotence, churn
# --------------------------------------------------------------------------- #


def test_identical_trees_share_a_digest_and_layout_changes_it(tmp_path: Path) -> None:
    first = make_task(tmp_path / "a", "same", environment={"app/main.py": "print(1)\n"})
    second = make_task(tmp_path / "b", "same", environment={"app/main.py": "print(1)\n"})
    moved = make_task(tmp_path / "c", "same", environment={"src/main.py": "print(1)\n"})

    assert craft.task_digest(first) == craft.task_digest(second)
    assert craft.task_digest(moved) != craft.task_digest(first)


def test_content_change_changes_the_digest(tmp_path: Path) -> None:
    task = make_task(tmp_path, "mutable", environment={"app/main.py": "print(1)\n"})
    before = craft.task_digest(task)
    (task / "environment/app/main.py").write_text("print(2)\n")

    assert craft.task_digest(task) != before


def test_rescan_of_an_unchanged_corpus_churns_nothing(tmp_path: Path) -> None:
    """The acceptance criterion: same digests ⇒ no row churn, and no rewrite.

    The mtime assertion matters as much as the byte comparison: a re-scan that
    rewrites identical bytes still publishes a new artifact to everything
    downstream that watches for one.
    """
    corpus = tmp_path / "corpus"
    make_task(corpus, "one", expert_hours=1.5)
    make_task(corpus, "two", tests={"test.sh": "#!/bin/bash\npytest /tests\n"})
    out = tmp_path / "out"
    source = source_for(corpus)

    first = craft.write_records(craft.scan([source]).records, out)
    stamp = first.path.stat().st_mtime_ns
    payload = first.path.read_bytes()

    second = craft.write_records(craft.scan([source]).records, out)

    assert first.rows == second.rows == 2
    assert first.digest == second.digest
    assert second.churn.is_empty
    assert second.rewritten is False
    assert second.path.stat().st_mtime_ns == stamp
    assert second.path.read_bytes() == payload


def test_churn_names_the_row_whose_digest_moved(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    make_task(corpus, "stable")
    changing = make_task(corpus, "changing", environment={"app/main.py": "print(1)\n"})
    out = tmp_path / "out"
    source = source_for(corpus)
    craft.write_records(craft.scan([source]).records, out)

    (changing / "environment/app/main.py").write_text("print(2)\n")
    result = craft.write_records(craft.scan([source]).records, out)

    assert result.churn.digest_changed == ("test/corpus\tchanging",)
    assert result.churn.added == ()
    assert result.churn.removed == ()
    assert result.churn.facets_changed == ()
    assert result.rewritten is True


def test_churn_separates_a_scanner_change_from_a_corpus_change(tmp_path: Path) -> None:
    """A facet that moves while the digest holds is a scanner change, not noise."""
    corpus = tmp_path / "corpus"
    make_task(corpus, "one")
    out = tmp_path / "out"
    records = craft.scan([source_for(corpus)]).records
    craft.write_records(records, out)

    edited = [records[0].model_copy(update={"pinned_deps": True})]
    result = craft.write_records(edited, out)

    assert result.churn.facets_changed == ("test/corpus\tone",)
    assert result.churn.digest_changed == ()


def test_churn_reports_additions_and_removals(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    make_task(corpus, "kept")
    out = tmp_path / "out"
    source = source_for(corpus)
    craft.write_records(craft.scan([source]).records, out)

    make_task(corpus, "fresh")
    result = craft.write_records(craft.scan([source]).records, out)

    assert result.churn.added == ("test/corpus\tfresh",)
    assert result.churn.removed == ()


def test_the_schema_carries_no_timestamp(tmp_path: Path) -> None:
    """A scan-time column would change every row on every run.

    Idempotence is a property of the schema, not only of the writer, so the
    absence is asserted rather than assumed.
    """
    names = set(craft.CRAFT_SCHEMA.names)

    assert names == set(craft.CraftRecord.model_fields)
    assert not {name for name in names if "time" in name or "date" in name or name.endswith("_at")}


def test_records_are_ordered_deterministically(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    for name in ("zebra", "alpha", "middle"):
        make_task(corpus, name)

    refs = [record.task_ref for record in craft.scan([source_for(corpus)]).records]

    assert refs == sorted(refs)


# --------------------------------------------------------------------------- #
# output safety and CLI
# --------------------------------------------------------------------------- #


def test_writing_inside_a_scanned_corpus_is_refused(tmp_path: Path) -> None:
    """TB3 is read-only by mission rule; the scanner enforces it rather than
    relying on the operator to remember."""
    corpus = tmp_path / "corpus"
    make_task(corpus, "one")

    with pytest.raises(ValueError, match="read-only"):
        craft.assert_output_outside_corpora(corpus / "derived", [source_for(corpus)])

    craft.assert_output_outside_corpora(tmp_path / "out", [source_for(corpus)])


def test_cli_scan_writes_parquet_and_reports_json(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    make_task(corpus, "one", expert_hours=2.0)
    make_task(corpus, "two", tests={"test.sh": "#!/bin/bash\npytest /tests\n"})
    out = tmp_path / "out"

    code = craft.main(["scan", str(corpus), "--out", str(out), "--json"])

    assert code == 0
    parquet = out / craft.PARQUET_NAME
    table = pq.read_table(parquet)
    assert table.num_rows == 2
    assert set(table.schema.names) == set(craft.CRAFT_SCHEMA.names)


def test_cli_refuses_an_empty_scan(capsys: pytest.CaptureFixture[str]) -> None:
    assert craft.main(["scan"]) == 2
    assert "nothing to scan" in capsys.readouterr().err


def test_cli_reports_a_missing_corpus_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert craft.main(["scan", str(tmp_path / "absent")]) == 2
    assert "corpus root not found" in capsys.readouterr().err


def test_distribution_counts_nulls_as_a_bucket(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    make_task(corpus, "classified", tests={"test.sh": "#!/bin/bash\npytest /tests\n"})
    make_task(corpus, "unclassified")

    counts = craft.distribution(craft.scan([source_for(corpus)]).records)

    assert counts["verifier_type"] == {"null": 1, "pytest": 1}
    assert counts["human_minutes_present"] == {"False": 2}


def test_duckdb_views_answer_the_acceptance_query(tmp_path: Path) -> None:
    """`sql/craft_views.sql` must load and group by verifier_type."""
    duckdb = pytest.importorskip("duckdb")
    corpus = tmp_path / "corpus"
    make_task(corpus, "one", tests={"test.sh": "#!/bin/bash\npytest /tests\n"})
    make_task(corpus, "two")
    out = tmp_path / "out"
    craft.write_records(craft.scan([source_for(corpus)]).records, out)

    views = Path(__file__).resolve().parents[1] / "sql/craft_views.sql"
    with duckdb.connect(":memory:") as connection:
        connection.execute(
            f"SET VARIABLE craft_parquet = '{(out / craft.PARQUET_NAME).as_posix()}'"
        )
        connection.execute(views.read_text())
        rows = connection.execute(
            "SELECT verifier_type, tasks FROM v_craft_verifier_type ORDER BY verifier_type"
        ).fetchall()
        corpus_rows = connection.execute(
            "SELECT tasks, verifier_classified, verifier_unclassified FROM v_craft_corpus"
        ).fetchall()

    assert rows == [("pytest", 1), ("unclassified", 1)]
    assert corpus_rows == [(2, 1, 1)]
