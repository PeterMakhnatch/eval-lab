"""Publish unverified candidate manifests, never canonical defect Findings."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.failure_library.fixmine.classify import model_family
from evallab.failure_library.fixmine.miner import digest, json_bytes
from evallab.failure_library.fixmine.models import Candidate, Consensus
from evallab.failure_library.store import LibraryStore


def publish_collection(
    store: LibraryStore,
    candidates: list[Candidate],
    decisions: list[Consensus],
    mine_root: Path,
    *,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage pinned diff metadata and model labels without claiming a defect."""
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("duplicate candidate ids")
    seen: set[str] = set()
    labels: list[dict[str, Any]] = []
    for decision in sorted(decisions, key=lambda row: row.candidate_id):
        if decision.candidate_id in seen or decision.candidate_id not in by_id:
            raise ValueError("duplicate or unknown consensus candidate id")
        seen.add(decision.candidate_id)
        if len({model_family(model) for model in decision.models}) < 2:
            raise ValueError("candidate labels require different served model families")
        candidate = by_id[decision.candidate_id]
        if digest((mine_root / candidate.diff_ref).read_bytes()) != candidate.diff_digest:
            raise ValueError("candidate diff digest mismatch")
        labels.append(
            {
                "schema_version": "fixmine-candidate-label/v1",
                "candidate_id": candidate.candidate_id,
                "task_locator": candidate.task_path,
                "parent_revision": candidate.parent_revision,
                "proposed_revision": candidate.fix_revision,
                "diff_digest": candidate.diff_digest,
                "two_family_yes": decision.accepted,
                "defect_class_label": decision.defect_class,
                "direction_label": decision.direction,
                "disagreement": decision.disagreement,
                "models": decision.models,
                "label_refs": decision.label_refs,
                "verification_status": "unverified",
            }
        )
    labels_bytes = b"".join(json_bytes(row) for row in labels)
    (mine_root / "candidate-labels.jsonl").write_bytes(labels_bytes)
    manifest = json.loads((mine_root / "manifest.json").read_text())
    collection = "fixmine-" + mine_root.parent.name
    agreed = [decision for decision in decisions if decision.accepted]
    target = store.findings_root.parent / "candidates"
    target.mkdir(parents=True, exist_ok=True)
    relative_manifest = f"research/failure-library/candidates/{collection}.manifest.json"
    state_prefix = f"state://fixmine/{mine_root.parent.name}/{mine_root.name}"
    if audit is not None:
        audit = {
            key: audit[key]
            for key in (
                "status",
                "model",
                "model_family",
                "requested_sample_size",
                "sample_size",
                "seed",
                "agreement_rate",
                "disagreement_ids",
            )
            if key in audit
        }
    report: dict[str, Any] = {
        "schema_version": "fixmine-candidate-manifest/v1",
        "collection": collection,
        "publication_kind": "unverified_candidate_metadata",
        "verification_status": "unverified",
        "canonical_findings": 0,
        "candidate_manifest": relative_manifest,
        "repo": manifest["repo"],
        "source_revision": manifest["revision"],
        "task_glob": manifest["task_glob"],
        "since": manifest["since"],
        "commits_inspected": manifest["commits_inspected"],
        "candidates": manifest["candidate_count"],
        "candidate_by_split": dict(sorted(Counter(row.split for row in candidates).items())),
        "classified": len(decisions),
        "not_classified": manifest["candidate_count"] - len(decisions),
        "agreed_candidate_labels": len(agreed),
        "rejected_candidate_labels": sum(not decision.accepted for decision in decisions),
        "disagreements": sum(decision.disagreement for decision in decisions),
        "by_class": dict(sorted(Counter(row.defect_class for row in agreed).items())),
        "by_split": dict(sorted(Counter(by_id[row.candidate_id].split for row in agreed).items())),
        "candidates_digest": manifest["candidates_digest"],
        "candidates_ref": state_prefix + "/candidates.jsonl",
        "labels_digest": digest(labels_bytes),
        "labels_ref": state_prefix + "/candidate-labels.jsonl",
        "audit": audit,
    }
    (mine_root / "report.json").write_bytes(json_bytes(report))
    (target / (collection + ".manifest.json")).write_bytes(json_bytes(report))
    note = [
        f"# {collection}",
        "",
        "Unverified upstream-diff candidates, not canonical broken-task findings.",
        "A commit or agreement of models is not independent verification. Proposed",
        "changes and model labels remain candidate metadata; no Finding, tier,",
        "fixed status or upstream_fix/executed evidence is emitted automatically.",
        "Raw model rationale stays private; no executable recipes are published.",
        "",
        f"- Repository: {manifest['repo']}",
        f"- History snapshot: `{manifest['revision']}`",
        f"- Task glob: `{manifest['task_glob']}`; since: `{manifest['since'] or 'all history'}`",
        f"- Non-merge commits inspected: {report['commits_inspected']}",
        f"- Candidate task-version changes: {report['candidates']}",
        f"- Candidate splits: `{json.dumps(report['candidate_by_split'], sort_keys=True)}`",
        f"- Classified: {report['classified']}; not classified: {report['not_classified']}",
        f"- Yes/yes candidate labels: {report['agreed_candidate_labels']}; "
        f"rejected labels: {report['rejected_candidate_labels']}",
        f"- Model disagreement flags: {report['disagreements']}",
        f"- Agreed labels by class: `{json.dumps(report['by_class'], sort_keys=True)}`",
        "- Canonical defect findings: 0",
        f"- Candidate digest: `{manifest['candidates_digest']}`",
        f"- Label digest: `{report['labels_digest']}`",
        f"- Candidate state: `{report['candidates_ref']}`",
        f"- Label state: `{report['labels_ref']}`",
        "",
        "## Provenance and reproducibility",
        "",
        "The blobless clone lives under `raw/git/<repo-slug>/`. Git objects pin",
        "the parent and proposed revision. The query cache retains read-only",
        "candidates, a manifest and content-addressed full diffs. Classifier caches",
        "retain prompts, raw outputs, session JSONL, served-model counts and hashes.",
        "Identity attestation verifies the served model, not a semantic task defect.",
        "Raw sessions, diffs and free-form rationale are not committed.",
        "",
        "```bash",
        f'evallab failures mine --rebuild --cache "$HOME/.local/state/evallab-failure-library/fixmine/{mine_root.parent.name}/{mine_root.name}"',
        "```",
        "",
        "Rebuild mode cannot fetch Git or call a model. Collection is the default:",
        "",
        "```bash",
        f"evallab failures mine --repo {manifest['repo']} --task-glob '{manifest['task_glob']}'"
        + (f" --since {manifest['since']}" if manifest["since"] else ""),
        "```",
        "",
        "New classification requires explicit `--classify --limit N`, with positive",
        "N. This bounds label work without discarding collected candidates. There",
        "is no execution, exploit/re-break campaign or automatic canonical promotion.",
        "",
        "## Splits and validation",
        "",
        "New families use `int(sha256('fixmine/v1\\x1f' + repo_slug + '\\x1f' + family),16) % 5`:",
        "bucket 0 is heldout, otherwise DEV. Directory variants stay grouped; shared",
        "task-table ids group by domain/template; tau Python tasks group by domain",
        "and user id. Existing non-DEV assignments override to cross_test; existing",
        "DEV-only assignments are preserved. Only DEV may feed defensive knowledge,",
        "with uncertainty intact. Classifiers do not receive splits or old labels.",
        "",
        "A proposed defect needs independent, task-version-specific evidence and",
        "benign validation before entering canonical broken-task findings. A source",
        "label, model agreement or a reproducer path alone does not provide this.",
        "",
        "## Audit",
        "",
    ]
    if audit is not None:
        note.append("```json\n" + json.dumps(audit, sort_keys=True, indent=2) + "\n```")
    else:
        note.append("No positive third-family audit is recorded; no agreement rate is claimed.")
    note.append("")
    (target / (collection + ".md")).write_text("\n".join(note))
    return report
