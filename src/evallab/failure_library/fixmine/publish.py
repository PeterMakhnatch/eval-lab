"""Turn attested two-family decisions into version-pinned V2 findings."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.failure_library.fixmine.classify import model_family
from evallab.failure_library.fixmine.miner import digest, json_bytes
from evallab.failure_library.fixmine.models import Candidate, Consensus
from evallab.failure_library.records import Evidence, Finding, TaskRef, make_finding_id
from evallab.failure_library.store import LibraryStore


def build_findings(
    candidates: list[Candidate], decisions: list[Consensus], mine_root: Path
) -> list[Finding]:
    """No rejected candidate or class-disagreement row can silently become a claim."""
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("duplicate candidate ids")
    seen: set[str] = set()
    findings: list[Finding] = []
    for decision in decisions:
        if decision.candidate_id not in by_id or decision.candidate_id in seen:
            raise ValueError("unknown or duplicate consensus id")
        seen.add(decision.candidate_id)
        if not decision.accepted:
            continue
        if len({model_family(model) for model in decision.models}) < 2:
            raise ValueError("finding requires two different served model families")
        candidate = by_id[decision.candidate_id]
        if digest((mine_root / candidate.diff_ref).read_bytes()) != candidate.diff_digest:
            raise ValueError("fix diff digest mismatch")
        collection = "fixmine-" + candidate.repo_slug
        source_url = candidate.repo_url + "/commit/" + candidate.fix_revision
        locator = candidate.fix_revision + ":" + candidate.task_path
        label_source = "; ".join(decision.label_refs)
        note = "Changed files: " + ", ".join(candidate.changed_files)
        findings.append(
            Finding(
                finding_id=make_finding_id(
                    candidate.repo_slug,
                    candidate.task_id,
                    candidate.parent_revision,
                    decision.defect_class,
                    locator,
                ),
                collection=collection,
                locator=locator,
                task=TaskRef(
                    source_id=candidate.repo_slug,
                    task_id=candidate.task_id,
                    upstream_repo=candidate.repo_url,
                    upstream_revision=candidate.parent_revision,
                    task_path=candidate.task_path,
                ),
                defect_class=decision.defect_class,
                direction=decision.direction,
                summary=decision.summary,
                evidence=[
                    Evidence(
                        kind="upstream_fix",
                        url=source_url,
                        fix_revision=candidate.fix_revision,
                        fix_diff_digest=candidate.diff_digest,
                        note=note[:1000],
                    ),
                    Evidence(
                        kind="judge_label",
                        label_source=label_source,
                        note=(
                            "Both served model families classified this as a task repair: "
                            + ", ".join(decision.models)
                            + (
                                ". Taxonomy or direction disagreement retained."
                                if decision.disagreement
                                else ""
                            )
                        )[:1000],
                    ),
                ],
                tier="V2_upstream_fixed",
                finder=candidate.repo_url.rsplit("/", 1)[0] + " upstream maintainers",
                source_url=source_url,
                observed_at=candidate.committed_at,
                status_now="fixed_upstream",
                fixed_in_revision=candidate.fix_revision,
                split=candidate.split,
                license=candidate.license,
            )
        )
    return sorted(findings, key=lambda finding: finding.finding_id)


def publish_collection(
    store: LibraryStore,
    candidates: list[Candidate],
    decisions: list[Consensus],
    mine_root: Path,
    *,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = json.loads((mine_root / "manifest.json").read_text())
    collection = "fixmine-" + mine_root.parent.name
    findings = build_findings(candidates, decisions, mine_root)
    store.write_findings(collection, findings)
    report: dict[str, Any] = {
        "collection": collection,
        "repo": manifest["repo"],
        "source_revision": manifest["revision"],
        "task_glob": manifest["task_glob"],
        "since": manifest["since"],
        "commits_inspected": manifest["commits_inspected"],
        "candidates": manifest["candidate_count"],
        "classified": len(decisions),
        "not_classified": manifest["candidate_count"] - len(decisions),
        "accepted": len(findings),
        "rejected": sum(not decision.accepted for decision in decisions),
        "disagreements": sum(decision.disagreement for decision in decisions),
        "by_class": dict(sorted(Counter(finding.defect_class for finding in findings).items())),
        "by_split": dict(sorted(Counter(finding.split for finding in findings).items())),
        "tier": "V2_upstream_fixed",
        "candidates_digest": manifest["candidates_digest"],
        "audit": audit,
    }
    (mine_root / "report.json").write_bytes(json_bytes(report))
    note = [
        f"# {collection}",
        "",
        "Upstream repair evidence, not independently executed verification. A change is",
        "published only after two attested, different model families both say it repairs",
        "a pre-existing task. Feature additions and formatting are not repairs. The V2",
        "tier describes the pinned parent and fix pair, not a claim that current HEAD",
        "remains fixed or that an oracle run has succeeded.",
        "",
        f"- Repository: {manifest['repo']}",
        f"- History snapshot: `{manifest['revision']}`",
        f"- Task glob: `{manifest['task_glob']}`; since: `{manifest['since'] or 'all history'}`",
        f"- Non-merge commits inspected: {report['commits_inspected']}",
        f"- Candidate task-version changes: {report['candidates']}",
        f"- Classified: {report['classified']}; not classified: {report['not_classified']}",
        f"- Accepted: {report['accepted']}; rejected: {report['rejected']}",
        f"- Primary label disagreements (retained in state): {report['disagreements']}",
        f"- Accepted by class: `{json.dumps(report['by_class'], sort_keys=True)}`",
        f"- Accepted by split: `{json.dumps(report['by_split'], sort_keys=True)}`",
        f"- Candidate JSONL digest: `{manifest['candidates_digest']}`",
        f"- Cache query: `{mine_root.parent.name}/{mine_root.name}` under Failure Library state `fixmine/`",
        "",
        "## Provenance and reproducibility",
        "",
        "The blobless clone lives under `raw/git/<repo-slug>/`. Its immutable Git",
        "objects pin both versions. The query cache contains read-only `candidates.jsonl`,",
        "`manifest.json` and content-addressed full diffs. `classifiers/` retains prompts,",
        "raw stdout/stderr, session JSONL, served-model counts and byte digests. Neither",
        "raw model sessions nor upstream source bytes are committed. Rebuild with the",
        "stored query cache; no remote Git or model call is allowed in rebuild mode:",
        "",
        "```bash",
        f'evallab failures mine --rebuild --cache "$HOME/.local/state/evallab-failure-library/fixmine/{mine_root.parent.name}/{mine_root.name}"',
        "```",
        "",
        "The rebuild revalidates raw session attestation and diff hashes. Initial mining:",
        "",
        "```bash",
        f"evallab failures mine --repo {manifest['repo']} --task-glob '{manifest['task_glob']}'"
        + (f" --since {manifest['since']}" if manifest["since"] else ""),
        "```",
        "",
        "Default mining classifies every candidate with the two subscription routes.",
        "`--collect-only` skips classifiers; `--limit N` explicitly bounds classification",
        "without discarding candidates. `--audit` adds a seeded, third-family 30-row audit.",
        "",
        "## Splits",
        "",
        "New families use `int(sha256('fixmine/v1\\x1f' + repo_slug + '\\x1f' + family),16) % 5`:",
        "bucket 0 is heldout, other buckets DEV. A task directory is its family (variant",
        "suffixes are removed); shared task-table ids are grouped by domain/template;",
        "tau-bench Python task lists group by domain and user id. All revisions of a",
        "family stay together. Any existing key non-DEV assignment overrides hashing",
        "to cross_test; existing DEV-only task assignments are preserved. Only DEV",
        "may feed knowledge. Classifiers never receive splits or existing labels.",
        "",
        "## Audit",
        "",
    ]
    if audit is not None:
        note.append("```json\n" + json.dumps(audit, sort_keys=True, indent=2) + "\n```")
    else:
        note.append(
            "No collection-specific third-family audit has been run; see the aggregate fixmine audit note for coverage."
        )
    note.append("")
    store.findings_path(collection).with_suffix(".md").write_text("\n".join(note))
    return report
