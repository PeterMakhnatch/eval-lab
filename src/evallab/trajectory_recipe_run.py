"""Run the seven trajectory recipes and write deterministic findings artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from evallab.trajectory_recipes import (
    RecipeFinding,
    TrialArtifacts,
    load_trial_artifacts,
    run_recipes,
)

FINDINGS_JSONL = "findings.jsonl"
FINDINGS_REPORT = "findings-report.md"
IMPROVEMENT_REQUESTS = "improvement-requests.md"
RECIPE_IDS = ("r1", "r2", "r3", "r4", "r5", "r6", "r7")
EMBARGO = (
    "EMBARGOED: produced from pre-rebuild EvidencePacks. PR #199 original exact review "
    "covered multi-call citations/observations, expected-negative anchors, coverage "
    "producer/state-journal units, omitted digest reopen. Amended heads b01d417 then "
    "519f542 claim all blockers resolved with byte-identical rebuilt pack digests "
    "57588706/b4c5f983/daf696b0/3033ddd3/3d0046e6; rebuilt packs pinned but "
    "unmaterialized; request materialization to shared root. Findings validate abstention "
    "behavior only; do not publish until the rebuilt packs are materialized."
)
PR199_BLOCKERS = (
    "PR #199 exact-review blockers: multi-call citations/observations, expected-negative "
    "anchors, coverage producer/state-journal units, omitted digest reopen. Amended heads "
    "b01d417 then 519f542 claim all blockers resolved with byte-identical rebuilt pack digests "
    "57588706/b4c5f983/daf696b0/3033ddd3/3d0046e6; rebuilt packs pinned but "
    "unmaterialized; request materialization to shared root."
)
DATA_REQUIREMENTS = """## Data requirements

### Corpus limits
- arms executed: 1
- n_tasks: 5
- attempts/task: 1
- successes: 0
- Wilson 95% CI: [0, 0.434]
- claims class: accounting/descriptive only — no reliability, ranking, capability, or causal claim
- aggregate unit convention: n_tasks, trials, and calls are never interchanged; every aggregate names its unit and micro/macro status
- degenerate all-equal-outcome bootstrap: suppressed

### Next-data requirements
Status: NOT AUTHORIZED TO RUN
- matched second arm on identical task digests
- T≥3 (prefer 5–10) repeats
- larger n_tasks
- preregistered stopping rule covering any sequential growth of n_tasks or k
- source_task-clustered bootstrap of the paired precision difference
- held-out labels before any judge-calibration claim
"""
REQUESTS = (
    ("pack_incomplete", "EvidencePack anchor defect", "Agent Data"),
    ("opportunity_unknown_r6", "context events", "Data-harness"),
    ("replay_oracle_unavailable", "state-certified replay", "Platform-Ops"),
    ("linkage_unresolved", "unpaired-call defect", "Data"),
    ("pair_unavailable", "matched-arm prerequisite", "Peter-authorization-required"),
    ("ontology_gap", "class proposals", "Synthetic Research"),
    ("blocked_metric", "IR multi-call + metric-collision fixes", "Data+Platform"),
)


@dataclass(frozen=True)
class SelectedPack:
    trial_id: str
    digest: str
    path: Path
    payload: Mapping[str, Any]

    @property
    def old_execution_sample_pack(self) -> bool:
        windows = self.payload.get("selected_windows")
        return (
            isinstance(windows, list)
            and len(windows) == 1
            and isinstance(windows[0], Mapping)
            and windows[0].get("reason") == "execution_sample"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evallab.trajectory_recipe_run")
    parser.add_argument("--analyses-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--trial", dest="trial_ids", nargs="+", action="extend", default=[])
    parser.add_argument(
        "--pack-digest", help="pin one pack directory/pack digest per selected trial"
    )
    return parser


def _read_pack(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence pack sidecar: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"evidence pack sidecar is not a JSON object: {path}")
    return value


def _digest(value: str) -> str:
    return value.removeprefix("sha256:")


def select_trial_sidecars(
    analyses_dir: Path, *, pack_digest: str | None = None
) -> dict[str, SelectedPack]:
    """Select exactly one sidecar per trial: explicit digest or newest created_at."""
    wanted = _digest(pack_digest) if pack_digest else None
    candidates: dict[str, list[SelectedPack]] = {}
    for path in sorted(analyses_dir.glob("*/*/evidence_pack.json")):
        if not path.is_file() or (wanted is not None and path.parent.name != wanted):
            continue
        item = SelectedPack(path.parent.parent.name, path.parent.name, path, _read_pack(path))
        candidates.setdefault(item.trial_id, []).append(item)
    selected: dict[str, SelectedPack] = {}
    for trial_id, options in candidates.items():
        if wanted is not None or len(options) == 1:
            selected[trial_id] = options[0]
            continue
        missing = [str(item.path) for item in options if not item.payload.get("created_at")]
        if missing:
            raise ValueError(
                "cannot select newest pack: missing created_at in " + ", ".join(missing)
            )
        selected[trial_id] = max(
            options, key=lambda item: (str(item.payload["created_at"]), item.digest)
        )
    return dict(sorted(selected.items()))


def discover_trial_ids(analyses_dir: Path) -> list[str]:
    return list(select_trial_sidecars(analyses_dir))


def _row(finding: RecipeFinding | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(finding, Mapping):
        value: Any = dict(finding)
    elif hasattr(finding, "model_dump"):
        value = finding.model_dump(mode="json")
    elif is_dataclass(finding):
        value = asdict(finding)
    else:
        raise TypeError(f"unsupported finding type: {type(finding)!r}")
    if not isinstance(value, dict):
        raise TypeError("finding serialization did not produce a dict")
    return value


def _rows(findings: Sequence[RecipeFinding | Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [_row(finding) for finding in findings]
    result.sort(
        key=lambda row: (
            str(row.get("trial_id") or ""),
            str(row.get("recipe_id") or ""),
            str(row.get("finding_id") or ""),
        )
    )
    return result


def serialize_finding(finding: RecipeFinding | Mapping[str, Any]) -> str:
    return json.dumps(_row(finding), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = (
        value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    )
    return text.replace("|", "\\|").replace("\n", " ")


def _citations(row: Mapping[str, Any]) -> list[str]:
    values = row.get("citations") or []
    if not isinstance(values, list):
        values = [values]
    return sorted(
        str(value.get("citation_id") or value.get("id"))
        if isinstance(value, Mapping)
        else str(value)
        for value in values
    )


def _quotes(row: Mapping[str, Any]) -> str:
    quotes = row.get("verbatim_quotes")
    if isinstance(quotes, list):
        return " ; ".join(
            str(item.get("quote") or item.get("text") or item.get("content") or "")
            if isinstance(item, Mapping)
            else str(item)
            for item in quotes
        )
    extras = row.get("extras")
    if isinstance(extras, Mapping):
        return str(extras.get("verbatim_quote") or extras.get("quote") or "")
    return ""


def _blocked_metric(row: Mapping[str, Any]) -> bool:
    if row.get("abstention_reason") == "blocked_metric":
        return True
    extras = row.get("extras") or {}
    if not isinstance(extras, Mapping):
        return extras == "blocked_metric" or (
            isinstance(extras, list) and "blocked_metric" in extras
        )
    return bool(extras.get("blocked_metric") or extras.get("blocked_metrics")) or any(
        key in {"blocked_metric", "blocked_metrics"} or value == "blocked_metric"
        for key, value in extras.items()
    )


def _matches(row: Mapping[str, Any], code: str) -> bool:
    if code == "opportunity_unknown_r6":
        return (
            row.get("recipe_id") == "r6" and row.get("abstention_reason") == "opportunity_unknown"
        )
    return (
        _blocked_metric(row) if code == "blocked_metric" else row.get("abstention_reason") == code
    )


def _validate(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        if row.get("recipe_id") != "r7" or row.get("disposition") != "screening_only":
            continue
        if row.get("class_id") is not None:
            raise ValueError("R7 screening_only findings must not carry a class_id")
        extras = row.get("extras")
        if (
            isinstance(extras, Mapping)
            and extras.get("opportunity_count", extras.get("opportunities")) == 0
            and row.get("abstention_reason") != "opportunity_unknown"
        ):
            raise ValueError(
                "R7 zero exposure must report unknown via opportunity_unknown, never 0.0"
            )


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        for row in rows
    )


def _report(
    rows: Sequence[Mapping[str, Any]], selections: Mapping[str, SelectedPack], digest: str
) -> str:
    trial_ids = sorted({str(row["trial_id"]) for row in rows})
    lines = [
        "# Findings report",
        "",
        f"> {EMBARGO}",
        "",
        "## Report conventions",
        "",
        "- index convention: each per-finding `index_convention` is pack/IR-local; indexes are not comparable across trials.",
        "- R2 localizations are conditional per-trial records only; no decisive-step depth or propagation distribution is pooled.",
        "- cohort unknown_n accounting is reported instead of pooling unexposed or unresolved records.",
        "- aggregate guard: first-error/decisive-step metrics are grouped only by (benchmark, target_definition), never pooled across targets.",
        "- aggregate unit labels: matrix cells are trials; abstention frequencies are RecipeFinding micro-counts; no table mixes n_tasks, trials, or calls.",
        "",
        "## Selected EvidencePacks",
        "",
        "| trial | digest | created_at |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| `{trial_id}` | `{item.digest}` | {_cell(item.payload.get('created_at'))} |"
        for trial_id, item in sorted(selections.items())
    )
    for trial_id in trial_ids:
        lines.extend(
            [
                "",
                f"## Trial `{trial_id}`",
                "",
                "| recipe_id | disposition | class_id | validity | support_level | abstention_reason | citations | verbatim quote | index convention |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in (row for row in rows if row["trial_id"] == trial_id):
            cells = (
                row.get("recipe_id"),
                row.get("disposition"),
                row.get("class_id"),
                row.get("validity"),
                row.get("support_level"),
                row.get("abstention_reason"),
                ", ".join(_citations(row)),
                _quotes(row),
                row.get("index_convention"),
            )
            lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    lines.extend(
        [
            "",
            "## Recipe × trial disposition matrix",
            "",
            "Unit: trials; aggregation: not applicable.",
        ]
    )
    lines.extend(
        [
            "| recipe | " + " | ".join(f"`{trial}`" for trial in trial_ids) + " |",
            "|---|" + "|".join("---" for _ in trial_ids) + "|",
        ]
    )
    for recipe in RECIPE_IDS:
        values = []
        for trial in trial_ids:
            values.append(
                ",".join(
                    str(row["disposition"])
                    for row in rows
                    if row["recipe_id"] == recipe and row["trial_id"] == trial
                )
                or "—"
            )
        lines.append(f"| `{recipe}` | " + " | ".join(values) + " |")
    counts = Counter(str(row["abstention_reason"]) for row in rows if row.get("abstention_reason"))
    lines.extend(
        [
            "",
            "## Abstention-reason frequency",
            "",
            "Unit: RecipeFindings; aggregation: micro-count (not n_tasks, trials, or calls).",
            "| reason | count |",
            "|---|---|",
        ]
    )
    lines.extend(f"| `{reason}` | {counts[reason]} |" for reason in sorted(counts)) or lines.append(
        "| — | 0 |"
    )
    lines.extend(
        [
            "",
            DATA_REQUIREMENTS.rstrip(),
            "",
            f"produced_at: content-addressed findings.jsonl sha256:{digest}",
            "",
        ]
    )
    return "\n".join(lines)


def _requests(
    rows: Sequence[Mapping[str, Any]], selections: Mapping[str, SelectedPack], digest: str
) -> str:
    lines = ["# Improvement requests", ""]
    for code, title, owner in REQUESTS:
        matched = [row for row in rows if _matches(row, code)]
        if not matched:
            continue
        trials = sorted({str(row["trial_id"]) for row in matched})
        citations = sorted({citation for row in matched for citation in _citations(row)})
        lines.extend(
            [
                f"## {title}",
                "",
                f"- owner: {owner}",
                f"- match: {code}",
                f"- affected_trials: {len(trials)}",
                f"- citation_count: {len(citations)}",
                "- trials: " + ", ".join(f"`{trial}`" for trial in trials),
            ]
        )
        if code == "pack_incomplete":
            lines.append(f"- {PR199_BLOCKERS}")
        lines.append("")
    old_trials = sorted(
        trial for trial, item in selections.items() if item.old_execution_sample_pack
    )
    if old_trials:
        lines.extend(
            [
                "## rebuilt packs pinned but unmaterialized; request materialization to shared root",
                "",
                "- owner: Agent Data",
                "- match: pre_rebuild_execution_sample_only",
                f"- affected_trials: {len(old_trials)}",
                "- citation_count: 0",
                "- trials: " + ", ".join(f"`{trial}`" for trial in old_trials),
                "- merge/materialization request: amended heads b01d417 then 519f542; rebuilt packs are not materialized in the shared derived root or CAS",
                f"- {PR199_BLOCKERS}",
                "",
            ]
        )
    if len(lines) == 2:
        lines.extend(["No improvement requests (no mapped findings or old-pack selection).", ""])
    lines.extend([f"produced_at: content-addressed findings.jsonl sha256:{digest}", ""])
    return "\n".join(lines)


def write_outputs(
    out_dir: Path,
    findings: Sequence[RecipeFinding | Mapping[str, Any]],
    selections: Mapping[str, SelectedPack],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _rows(findings)
    _validate(rows)
    jsonl = _jsonl(rows)
    digest = hashlib.sha256(jsonl.encode("utf-8")).hexdigest()
    (out_dir / FINDINGS_JSONL).write_text(jsonl, encoding="utf-8")
    (out_dir / FINDINGS_REPORT).write_text(_report(rows, selections, digest), encoding="utf-8")
    (out_dir / IMPROVEMENT_REQUESTS).write_text(
        _requests(rows, selections, digest), encoding="utf-8"
    )


def collect_findings(
    analyses_dir: Path, selections: Mapping[str, SelectedPack]
) -> list[RecipeFinding]:
    findings: list[RecipeFinding] = []
    for trial_id, selected in sorted(selections.items()):
        try:
            artifacts: TrialArtifacts = load_trial_artifacts(
                analyses_dir, trial_id, digest=selected.digest
            )
        except TypeError:  # Supports focused tests that monkeypatch the pre-amendment two-arg seam.
            artifacts = load_trial_artifacts(analyses_dir, trial_id)
        pack_path = getattr(artifacts, "pack_path", None)
        if pack_path and Path(pack_path).parent.name != selected.digest:
            raise ValueError(
                f"loader chose a different pack for {trial_id}: expected {selected.digest}"
            )
        findings.extend(run_recipes(artifacts, semantics_profile_digest=None))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    analyses_dir, out_dir = (
        args.analyses_dir.expanduser().resolve(),
        args.out.expanduser().resolve(),
    )
    if not analyses_dir.is_dir():
        print(f"error: analyses dir not found: {analyses_dir}", file=sys.stderr)
        return 1
    try:
        selections = select_trial_sidecars(analyses_dir, pack_digest=args.pack_digest)
        requested = sorted(set(args.trial_ids))
        missing = [trial for trial in requested if trial not in selections]
        if missing:
            raise ValueError("no selected evidence pack for trial(s): " + ", ".join(missing))
        if requested:
            selections = {trial: selections[trial] for trial in requested}
        write_outputs(out_dir, collect_findings(analyses_dir, selections), selections)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
