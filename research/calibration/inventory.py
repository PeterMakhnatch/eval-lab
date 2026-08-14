"""Walk the EVIDENCE corpus, sealed keys, and trajectory labels.

This is the shipped inventory used by brief-09 consume docs and by the
verification audits. It does not call a judge.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rubrics import VERDICTS, all_criterion_names

FAMILIES: tuple[str, ...] = (
    "checkout-pool-exhaustion",
    "retry-storm-backlog",
)

REQUIRED_VARIANTS: tuple[str, ...] = (
    "correct",
    "subtly-wrong-cause",
    "right-cause-useless-actions",
    "fabricated-evidence",
    "style-only-fluent",
)

TAXONOMY: tuple[str, ...] = (
    "task_invalid",
    "environment_failure",
    "harness_failure",
    "verifier_false_positive",
    "verifier_false_negative",
    "planning",
    "evidence_use",
    "tool_use",
    "implementation",
    "verification_behavior",
    "context_management",
    "policy_or_refusal",
    "unknown",
)

DOCUMENT_SUFFIXES: tuple[str, ...] = (".md",)
KEY_DIR_NAME = "answer-keys"
LABEL_DIR_NAME = "trajectory-labels"
MANIFEST_NAME = "corpus.json"
VARIANT_COMMENT_RE = re.compile(
    r"<!--\s*calibration-variant:\s*([a-z0-9-]+)\s*-->", re.IGNORECASE
)
FILENAME_VARIANT_RE = re.compile(
    r"^\d{2}-(" + "|".join(re.escape(v) for v in REQUIRED_VARIANTS)
    + r"|empty|copied-evidence)-"
)

HARBOR_PRACTICE_RUNS = Path(
    "/Users/petermakhnatch/Developer/agent-evals/harbor-practice/runs"
)


def worktree_root(start: Path | None = None) -> Path:
    """Return the git worktree root that contains this calibration package."""
    here = Path(start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "calibration").is_dir() and (candidate / ".git").exists():
            return candidate
        if (candidate / "calibration").is_dir() and (candidate / ".git").is_file():
            return candidate
    return Path(__file__).resolve().parents[1]


def calibration_root(root: Path | None = None) -> Path:
    return (root or worktree_root()) / "calibration"


def family_dir(family: str, root: Path | None = None) -> Path:
    return calibration_root(root) / family


@dataclass(frozen=True)
class LabeledDocument:
    family: str
    doc_id: str
    path: Path
    variant: str
    source: str | None = None


@dataclass
class CorpusInventory:
    families: dict[str, list[LabeledDocument]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {family: len(docs) for family, docs in self.families.items()}

    def variant_histogram(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for family, docs in self.families.items():
            out[family] = dict(sorted(Counter(d.variant for d in docs).items()))
        return out

    def missing_required_variants(self) -> dict[str, list[str]]:
        missing: dict[str, list[str]] = {}
        for family, docs in self.families.items():
            present = {d.variant for d in docs}
            absent = [v for v in REQUIRED_VARIANTS if v not in present]
            if absent:
                missing[family] = absent
        return missing


def _load_manifest(family: str, root: Path | None = None) -> list[dict[str, Any]]:
    path = family_dir(family, root) / MANIFEST_NAME
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    docs = payload.get("documents")
    if not isinstance(docs, list):
        raise ValueError(f"{path} missing documents list")
    return docs


def _variant_from_document(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = VARIANT_COMMENT_RE.search(text)
    if match:
        return match.group(1).strip().lower()
    name_match = FILENAME_VARIANT_RE.match(path.name)
    if name_match:
        return name_match.group(1)
    return None


def iter_family_documents(family: str, root: Path | None = None) -> list[LabeledDocument]:
    base = family_dir(family, root)
    if not base.is_dir():
        return []
    manifest = _load_manifest(family, root)
    by_id: dict[str, LabeledDocument] = {}
    if manifest:
        for entry in manifest:
            rel = entry["path"]
            path = base / rel
            if not path.is_file():
                raise FileNotFoundError(f"manifest path missing: {path}")
            by_id[entry["id"]] = LabeledDocument(
                family=family,
                doc_id=entry["id"],
                path=path,
                variant=entry["variant"],
                source=entry.get("source"),
            )
        return [by_id[k] for k in by_id]

    docs: list[LabeledDocument] = []
    for path in sorted(base.iterdir()):
        if not path.is_file() or path.suffix not in DOCUMENT_SUFFIXES:
            continue
        if path.name in {MANIFEST_NAME, "README.md"}:
            continue
        variant = _variant_from_document(path)
        if variant is None:
            raise ValueError(f"{path} has no variant tag")
        docs.append(
            LabeledDocument(
                family=family,
                doc_id=path.stem,
                path=path,
                variant=variant,
            )
        )
    return docs


def corpus_inventory(root: Path | None = None) -> CorpusInventory:
    inventory = CorpusInventory()
    for family in FAMILIES:
        inventory.families[family] = iter_family_documents(family, root)
    return inventory


def answer_key_path(doc: LabeledDocument, root: Path | None = None) -> Path:
    return family_dir(doc.family, root) / KEY_DIR_NAME / f"{doc.doc_id}.json"


def load_answer_key(doc: LabeledDocument, root: Path | None = None) -> dict[str, Any]:
    path = answer_key_path(doc, root)
    return json.loads(path.read_text(encoding="utf-8"))


def audit_answer_keys(root: Path | None = None) -> list[str]:
    """Return human-readable pairing lines. Empty of gaps means every
    document has a key covering every named CR/AQ/EF criterion."""
    lines: list[str] = []
    gaps: list[str] = []
    inventory = corpus_inventory(root)
    for family, docs in inventory.families.items():
        expected = all_criterion_names(family)
        for doc in docs:
            key_path = answer_key_path(doc, root)
            if not key_path.is_file():
                gaps.append(f"MISSING_KEY {family}/{doc.doc_id}")
                lines.append(f"GAP {family}/{doc.doc_id} -> {key_path.name} MISSING")
                continue
            key = json.loads(key_path.read_text(encoding="utf-8"))
            criteria = key.get("criteria") or {}
            missing_names: list[str] = []
            bad_verdicts: list[str] = []
            for dimension, name in expected:
                cell = (criteria.get(dimension) or {}).get(name)
                if not isinstance(cell, dict):
                    missing_names.append(f"{dimension}.{name}")
                    continue
                verdict = cell.get("verdict")
                rationale = cell.get("rationale")
                if verdict not in VERDICTS:
                    bad_verdicts.append(f"{dimension}.{name}={verdict!r}")
                if not isinstance(rationale, str) or not rationale.strip():
                    missing_names.append(f"{dimension}.{name}.rationale")
                elif "\n" in rationale.strip():
                    bad_verdicts.append(f"{dimension}.{name} rationale not one-line")
            if missing_names or bad_verdicts:
                gaps.append(f"{family}/{doc.doc_id}")
                lines.append(
                    f"GAP {family}/{doc.doc_id} missing={missing_names} bad={bad_verdicts}"
                )
            else:
                lines.append(
                    f"OK {family}/{doc.doc_id} -> {key_path.name} "
                    f"criteria={len(expected)} variant={doc.variant}"
                )
    if gaps:
        lines.append(f"GAPS {len(gaps)}")
    else:
        lines.append("GAPS 0")
    return lines


def is_completed_trial_dir(path: Path) -> bool:
    if not (path / "result.json").is_file():
        return False
    return any((path / name).exists() for name in ("trial.log", "verifier", "agent", "steps"))


def iter_completed_trials(root: Path | None = None) -> list[Path]:
    """Trial dirs under harbor-practice/runs and this repo's evidence/runs.

    Job wrappers that only hold child trials are excluded. Trials that never
    wrote result.json are excluded. This repo's gitignored ./runs/ is excluded.
    """
    base = root or worktree_root()
    search_roots = [HARBOR_PRACTICE_RUNS, base / "evidence" / "runs"]
    found: list[Path] = []
    for search in search_roots:
        if not search.is_dir():
            continue
        for result in sorted(search.rglob("result.json")):
            trial = result.parent
            if is_completed_trial_dir(trial):
                found.append(trial)
    return found


def trial_label_path(trial: Path, root: Path | None = None) -> Path:
    data = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    trial_name = data.get("trial_name") or trial.name
    return calibration_root(root) / LABEL_DIR_NAME / f"{trial_name}.json"


def _atif_step_ids(trial: Path) -> set[int]:
    traj = trial / "agent" / "trajectory.json"
    if not traj.is_file():
        return set()
    try:
        payload = json.loads(traj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    steps = payload.get("steps") if isinstance(payload, dict) else None
    ids: set[int] = set()
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("step_id"), int):
                ids.add(step["step_id"])
    return ids


def _has_steps_tree(trial: Path) -> bool:
    return (trial / "steps").is_dir()


def audit_trajectory_labels(root: Path | None = None) -> list[str]:
    lines: list[str] = []
    unlabeled: list[str] = []
    for trial in iter_completed_trials(root):
        label_path = trial_label_path(trial, root)
        rel = _display_trial(trial, root)
        if not label_path.is_file():
            unlabeled.append(rel)
            lines.append(f"UNLABELED {rel}")
            continue
        label = json.loads(label_path.read_text(encoding="utf-8"))
        category = label.get("primary_category")
        evidence = label.get("evidence")
        problems: list[str] = []
        if category not in TAXONOMY:
            problems.append(f"bad_category={category!r}")
        if not isinstance(evidence, list) or not evidence:
            problems.append("no_evidence")
        else:
            cited = evidence[0]
            path = cited.get("path") if isinstance(cited, dict) else None
            step = cited.get("step") if isinstance(cited, dict) else "missing"
            if not path:
                problems.append("missing_path")
            else:
                cited_file = trial / path
                if not cited_file.exists():
                    problems.append(f"missing_cited_file={path}")
            atif_ids = _atif_step_ids(trial)
            if atif_ids:
                if step not in atif_ids:
                    problems.append(f"step={step!r} not in ATIF {sorted(atif_ids)}")
            else:
                if step is not None and not _has_steps_tree(trial):
                    problems.append(f"step must be null without ATIF/steps, got {step!r}")
        if problems:
            unlabeled.append(rel)
            lines.append(f"BAD {rel} -> {label_path.name} {problems}")
        else:
            lines.append(
                f"OK {rel} -> {label_path.name} "
                f"category={category} path={evidence[0].get('path')} "
                f"step={evidence[0].get('step')}"
            )
    if unlabeled:
        lines.append(f"UNLABELED_OR_BAD {len(unlabeled)}")
    else:
        lines.append("UNLABELED_OR_BAD 0")
    return lines


def audit_environment_keys(root: Path | None = None) -> list[str]:
    """Search the worktree (and copied task trees) for sealed keys under environment/."""
    base = root or worktree_root()
    patterns = (
        "**/environment/**/answer-keys/**",
        "**/environment/**/*answer-key*",
        "**/environment/**/*expected-verdict*",
        "**/environment/**/answer_key*",
        "**/environment/**/expected_verdict*",
    )
    hits: list[str] = []
    for pattern in patterns:
        for path in base.glob(pattern):
            if path.is_file():
                hits.append(str(path.relative_to(base)))
    lines = [
        f"search_root={base}",
        f"patterns={', '.join(patterns)}",
    ]
    if hits:
        lines.extend(f"HIT {h}" for h in hits)
        lines.append(f"HITS {len(hits)}")
    else:
        lines.append("HITS 0")
    return lines


def _display_trial(trial: Path, root: Path | None = None) -> str:
    base = root or worktree_root()
    try:
        return str(trial.relative_to(HARBOR_PRACTICE_RUNS))
    except ValueError:
        try:
            return str(trial.relative_to(base / "evidence" / "runs"))
        except ValueError:
            return str(trial)


def format_corpus_inventory(
    inventory: CorpusInventory | None = None, root: Path | None = None
) -> str:
    inv = inventory or corpus_inventory(root)
    lines = ["# corpus inventory", ""]
    for family in FAMILIES:
        docs = inv.families.get(family, [])
        lines.append(f"family={family} count={len(docs)}")
        hist = Counter(d.variant for d in docs)
        for variant, n in sorted(hist.items()):
            lines.append(f"  variant {variant}={n}")
        for required in REQUIRED_VARIANTS:
            status = "present" if required in hist else "MISSING"
            lines.append(f"  required {required} {status}")
        for doc in docs:
            lines.append(f"  doc {doc.doc_id} variant={doc.variant}")
        lines.append("")
    missing = inv.missing_required_variants()
    if missing:
        lines.append(f"missing_required_variants={missing}")
    else:
        lines.append("missing_required_variants={}")
    return "\n".join(lines) + "\n"


def write_audit_bundle(dest: Path, root: Path | None = None) -> dict[str, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    inventory = corpus_inventory(root)
    written = {
        "corpus-inventory.txt": dest / "corpus-inventory.txt",
        "answer-key-audit.txt": dest / "answer-key-audit.txt",
        "no-keys-in-environment.txt": dest / "no-keys-in-environment.txt",
        "trajectory-label-audit.txt": dest / "trajectory-label-audit.txt",
    }
    written["corpus-inventory.txt"].write_text(
        format_corpus_inventory(inventory, root), encoding="utf-8"
    )
    written["answer-key-audit.txt"].write_text(
        "\n".join(audit_answer_keys(root)) + "\n", encoding="utf-8"
    )
    written["no-keys-in-environment.txt"].write_text(
        "\n".join(audit_environment_keys(root)) + "\n", encoding="utf-8"
    )
    written["trajectory-label-audit.txt"].write_text(
        "\n".join(audit_trajectory_labels(root)) + "\n", encoding="utf-8"
    )
    return written


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Audit the EVIDENCE calibration corpus")
    parser.add_argument("--out", type=Path, help="directory to write audit files")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.out:
        write_audit_bundle(args.out, args.root)
    else:
        print(format_corpus_inventory(root=args.root), end="")
        print("\n".join(audit_answer_keys(args.root)))
        print("\n".join(audit_environment_keys(args.root)))
        print("\n".join(audit_trajectory_labels(args.root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
