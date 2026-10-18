"""Freeze three native coding probes; preparation only, never invoke a model or grader.

Run with an existing Eval Lab Python environment. All generated material, including
external controls and grader fixtures, belongs under a fresh --output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE = "5171c20c7d1f5c00d020db6087eac3041f94bf13"
BUDGET = 12_000
TASKS = {
    "devloop_json": {
        "paths": ["src/evallab/devloop.py"],
        "text": """Repair the developer-loop output contract.

Edit only src/evallab/devloop.py.

Public behavioral acceptance:
- `evallab registry devloop --run --json` must write exactly one complete JSON
  document to stdout, even when its child emits JSON or other stdout text.
  Preserve the existing plan fields. Child stdout and stderr must remain visible
  on stderr in JSON mode; do not swallow child failure diagnostics.
- Return the child's actual exit status, including a nonzero failure status.
- Without --json, retain normal interactive child stdout/stderr behavior.
- Without --run, report the plan without executing a child.
- Keep existing direct-import/text-reference selection, configured testpaths,
  command arguments, and lane behavior. Do not expand dependency closure or
  rewrite the planner as a different test-selection policy.
""",
    },
    "contextpack_priority": {
        "paths": ["src/evallab/contextpack.py"],
        "text": """Repair path-scoped context-pack budget safety ordering.

Edit only src/evallab/contextpack.py.

Public behavioral acceptance:
- For path-scoped `build_context_pack`, generated lookup inventories
  docs/INDEX.md and docs/repo-map.md must shed before ordinary matching living
  documentation, even if inventories contain many more path references.
- docs/NOW.md is the current-state/safety router: retain it until all ordinary
  matching documentation and generated inventories have shed first. At an
  extreme positive budget it may honestly be omitted after those other docs.
- Preserve the existing document corpus, relevance scores, matching semantic
  sections verbatim, source citations, deterministic ordering, and unbudgeted
  selection. Do not solve this by changing scoring or excluding inventories
  from discovery. Do not paraphrase or clip retained semantic sections.
- Keep the mission brief/instructions intact under shedding. Metadata must
  truthfully identify retained/dropped documents and truncation. If protected
  content cannot fit an extreme positive budget, report its real estimated
  size rather than claiming the budget was met or pretending NOW was retained.
- Keep unscoped behavior and the existing nonpositive/unlimited-budget contract.
""",
    },
    "traj_readout": {
        "paths": ["src/evallab/traj.py", "src/evallab/cli.py"],
        "text": """Add bounded reads to the public `evallab traj outline` CLI.

Edit only src/evallab/traj.py and src/evallab/cli.py.

Public behavioral acceptance:
- `traj outline TARGET --summary` omits individual step bodies in both text and
  --json output while retaining full-run metrics and source provenance.
- `traj outline TARGET --steps START:END` selects an inclusive range of logical
  outline step IDs in both text and --json output. These are not original ATIF
  IDs: native IDs can be noncontiguous and restart in continuation documents.
- Bounded output must not recompute full-run counts, phases, loop features,
  native final_metrics token totals, or known/unknown cost from the subset.
  Preserve full source paths/digests and each selected step's original source
  document, digest, and source_step_id across continuation boundaries.
- Malformed, missing, nonpositive, reversed, or entirely nonmatching ranges
  must fail with a nonzero status; an empty successful outline is not a fix.
- Existing full --json output when neither selector is supplied must remain
  unchanged. Preserve existing --verbose behavior and unrelated CLI commands.
""",
    },
}

COMMON = """This is an exploratory native local coding task in an exact pinned source
snapshot, not a hermetic Harbor trial. The root AGENTS.md and other repository
instructions are the same in every arm. The inline context pack below is
supplemental routing context, not permission to expand this task.

Make a real source-only patch inside the stated edit scope. Do not edit or add
instructions, documentation, tests, fixtures, dependencies, configuration,
graders, or unrelated source files. In particular, do not update generated
indexes. This experiment's explicit source-only scope supersedes routine
repository requests to update docs/tests. No installs, builds, paid services,
network/model calls, local generative inference, commits, or pushes. You may
inspect existing source/tests. Do not search outside this snapshot for answers
or evaluation material. Finish the implementation and briefly summarize the
changed behavior and any checks actually performed. No expected solution or
external evaluation code is provided in this prompt.
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
            )
        elif path.is_file():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "kind": "file",
                    "sha256": digest(path.read_bytes()),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            )
    return rows


def copy_snapshot(source: Path, destination: Path) -> None:
    """Clone only the clean Git archive, never an existing checkout/runtime."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # Native APFS copy-on-write, not hardlinks: solver edits cannot alter
        # another arm or the reference corpus. A clone failure is not ignored.
        subprocess.run(["cp", "-cR", str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def load_compiler(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load context compiler: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Candidate source checkout")
    parser.add_argument("--output", type=Path, required=True, help="Fresh owned scratch directory")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--fixed-compiler",
        choices=("baseline", "candidate"),
        help="Use the same compiler for both arms when studying an independent controller factor",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Existing dependency-complete interpreter; no installation is performed",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    root = args.root.resolve()
    output = args.output.resolve()
    # Preserve the venv launcher path: resolving its symlink would lose the venv.
    python = args.python.absolute()
    output.mkdir(parents=True, exist_ok=False)
    control = output / "control"
    sources = control / "sources"
    tooling = control / "tooling"
    sources.mkdir(parents=True)
    tooling.mkdir()
    archive = control / "baseline.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "--output", str(archive), BASELINE],
        cwd=root,
        check=True,
    )
    baseline_root = control / "baseline-workspace"
    baseline_root.mkdir()
    with tarfile.open(archive) as stream:
        stream.extractall(baseline_root, filter="data")

    inventory = file_inventory(baseline_root)
    inventory_path = control / "baseline-files.json"
    json_write(inventory_path, inventory)
    corpus = [row for row in inventory if row["path"].startswith("docs/")]
    corpus_path = control / "documentation-files.json"
    json_write(corpus_path, corpus)

    # Freeze implementation-positive controls before compiling either arm.
    # These are post-hoc controls, explicitly NOT blind model attempts.
    allowed = sorted({path for task in TASKS.values() for path in task["paths"]})
    source_hashes = {}
    for relative in allowed:
        destination = sources / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = (root / relative).read_bytes()
        destination.write_bytes(data)
        source_hashes[relative] = digest(data)
    for name in ("prepare_code_cases.py", "grade_code_case.py"):
        shutil.copy2(Path(__file__).with_name(name), tooling / name)
    grader = tooling / "grade_code_case.py"
    manifest_path = output / "manifest.json"

    # Both compiler modules use baseline dependencies and the exact same
    # baseline corpus. No current working-tree documentation enters either arm.
    sys.path.insert(0, str(baseline_root / "src"))
    baseline_compiler = load_compiler(
        "code_probe_baseline_contextpack",
        baseline_root / "src/evallab/contextpack.py",
    )
    candidate_compiler = load_compiler(
        "code_probe_candidate_contextpack",
        sources / "src/evallab/contextpack.py",
    )
    compilers = {"baseline": baseline_compiler, "candidate": candidate_compiler}
    compiler_arms = (args.fixed_compiler,) if args.fixed_compiler else ("baseline", "candidate")
    packs = {}
    for task_name, task in TASKS.items():
        for compiler_arm in compiler_arms:
            compiler = compilers[compiler_arm]
            pack = compiler.build_context_pack(
                "builder",
                root=baseline_root,
                paths=task["paths"],
                token_budget=BUDGET,
            )
            packs[(task_name, compiler_arm)] = pack

    def grade_command(workspace: Path, task: str, receipt: Path) -> str:
        return shlex.join(
            [
                str(python),
                "-B",
                str(grader),
                "--manifest",
                str(manifest_path),
                "--workspace",
                str(workspace),
                "--task",
                task,
                "--output",
                str(receipt),
            ]
        )

    cases = []
    controls = []
    for task_name, task in TASKS.items():
        public_text = COMMON + "\n" + task["text"]
        for arm in ("baseline", "candidate"):
            compiler_arm = args.fixed_compiler or arm
            pack = packs[(task_name, compiler_arm)]
            prompt = (
                public_text
                + "\n\nBEGIN INLINE CONTEXT PACK\n\n"
                + pack.markdown
                + "\nEND INLINE CONTEXT PACK\n"
            )
            for repeat in range(1, args.repeats + 1):
                # Do not advertise the treatment arm through the solver's cwd.
                opaque_id = digest(f"{task_name}:{arm}:{repeat}".encode())[:24]
                case_root = output / "cases" / opaque_id
                workspace = case_root / "workspace"
                copy_snapshot(baseline_root, workspace)
                artifacts = case_root / "artifacts"
                artifacts.mkdir()
                prompt_path = case_root / "prompt.txt"
                pack_path = case_root / "context-pack.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                pack_path.write_text(pack.markdown, encoding="utf-8")
                cases.append(
                    {
                        "case_id": f"{task_name}-{arm}-{repeat:02d}",
                        "task": task_name,
                        "arm": arm,
                        "repeat": repeat,
                        "compiler_arm": compiler_arm,
                        "workspace": str(workspace),
                        "output_dir": str(artifacts),
                        "prompt_file": str(prompt_path),
                        "pack_file": str(pack_path),
                        "public_task_sha256": digest(public_text.encode()),
                        "prompt_sha256": digest(prompt.encode()),
                        "pack_sha256": digest(pack.markdown.encode()),
                        "pack_metadata": pack.to_dict(),
                        "allowed_paths": task["paths"],
                        "grading_command": grade_command(
                            workspace,
                            task_name,
                            artifacts / "grade.json",
                        ),
                    }
                )
        positive_root = control / "positive" / task_name
        positive_workspace = positive_root / "workspace"
        copy_snapshot(baseline_root, positive_workspace)
        for relative in task["paths"]:
            shutil.copy2(sources / relative, positive_workspace / relative)
        for kind, workspace, expected in (
            ("untouched_baseline", baseline_root, "FAIL"),
            ("posthoc_implementation_positive", positive_workspace, "PASS"),
        ):
            receipt = control / "receipts" / task_name / f"{kind}.json"
            controls.append(
                {
                    "task": task_name,
                    "kind": kind,
                    "workspace": str(workspace),
                    "expected": expected,
                    "observed": None,
                    "grading_command": grade_command(workspace, task_name, receipt),
                    "positive_source_sha256": (
                        {path: source_hashes[path] for path in task["paths"]}
                        if expected == "PASS"
                        else None
                    ),
                }
            )

    command_argv = [
        str(python),
        "-B",
        str(Path(__file__).absolute()),
        "--root",
        str(root),
        "--output",
        str(output),
        "--python",
        str(python),
        "--repeats",
        str(args.repeats),
    ]
    if args.fixed_compiler:
        command_argv.extend(("--fixed-compiler", args.fixed_compiler))
    command = shlex.join(command_argv)
    manifest = {
        "schema": "agent-efficiency-native-code-cases/v1",
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "baseline_revision": BASELINE,
        "baseline_archive": str(archive),
        "baseline_archive_sha256": digest(archive.read_bytes()),
        "baseline_workspace": str(baseline_root),
        "baseline_files": str(inventory_path),
        "baseline_files_sha256": digest(inventory_path.read_bytes()),
        "documentation_files": str(corpus_path),
        "documentation_files_sha256": digest(corpus_path.read_bytes()),
        "root_agents_sha256": digest((baseline_root / "AGENTS.md").read_bytes()),
        "compiler_sources": {
            "baseline_sha256": digest(
                (baseline_root / "src/evallab/contextpack.py").read_bytes(),
            ),
            "candidate_sha256": source_hashes["src/evallab/contextpack.py"],
        },
        "mission": "builder",
        "token_budget": BUDGET,
        "repeats": args.repeats,
        "fixed_compiler": args.fixed_compiler,
        "only_experimental_change": (
            (
                f"Both arms use the {args.fixed_compiler} compiler on the same pinned "
                "baseline corpus; any independent treatment is recorded by the controller."
            )
            if args.fixed_compiler
            else (
                "Baseline versus candidate compiler applied to the same pinned baseline "
                "documentation corpus; its pack is inline in the otherwise identical prompt."
            )
        ),
        "tasks": {
            name: {
                "allowed_paths": task["paths"],
                "public_task_text": COMMON + "\n" + task["text"],
                "public_task_sha256": digest((COMMON + "\n" + task["text"]).encode()),
            }
            for name, task in TASKS.items()
        },
        "tooling_sha256": {
            path.name: digest(path.read_bytes()) for path in sorted(tooling.iterdir())
        },
        "python": str(python),
        "preparation_command": command,
        "installation": (
            "None. Invoke these scripts with an existing Eval Lab dependency-complete "
            "Python environment. Do not uv sync, pip install, or create copied venvs. "
            "Solver source is selected explicitly with PYTHONPATH by the external grader."
        ),
        "cases": cases,
        "controls": controls,
        "executed": {"preparation": True, "grading": False, "model_calls": False},
        "caveats": [
            "Exploratory native local patches, not hermetic Harbor trials.",
            "No network or model calls, dependency installation, builds, or validation "
            "are performed by preparation.",
            "Source snapshots have no .git metadata or venv; all tracked baseline "
            "bytes are present. macOS copies use APFS copy-on-write, not hardlinks.",
            "Fixtures and grader material remain outside solver snapshots, but local "
            "filesystem permissions are not an adversarial sandbox.",
            "Positive controls are known post-hoc implementation overlays, not blind "
            "model attempts or evidence of model coding success.",
            "External behavioral graders are bounded acceptance, not proof of all "
            "correctness; no LLM judge and no outcome-based exclusions.",
            "Pack estimated_tokens is the compiler's character estimate, not provider "
            "token accounting. Model work/usage must be measured separately.",
            "All prepared cases must be retained in analysis, including failures and "
            "scope violations. Preparation does not schedule or execute them.",
        ],
    }
    json_write(manifest_path, manifest)
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "manifest_sha256": digest(manifest_path.read_bytes()),
                "baseline_archive_sha256": manifest["baseline_archive_sha256"],
                "documentation_files_sha256": manifest["documentation_files_sha256"],
                "cases": len(cases),
                "controls": len(controls),
                "grading_executed": False,
                "model_calls": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
