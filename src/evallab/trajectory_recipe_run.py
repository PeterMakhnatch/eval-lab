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
    pin = parser.add_mutually_exclusive_group()
    pin.add_argument(
        "--campaign-report",
        type=Path,
        help="pin trial packs from a CampaignReport JSON (source_refs)",
    )
    pin.add_argument(
        "--pack-map",
        type=Path,
        help="pin trial packs from a JSON object {trial_id: pack_digest}",
    )
    pin.add_argument(
        "--pack-digest",
        help="pin one pack directory/pack digest; requires a single --trial",
    )
    parser.add_argument("--report-id", default=None, help="echoed verbatim in the findings report")
    parser.add_argument("--cas-id", default=None, help="echoed verbatim in the findings report")
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


def _matches_digest(item: SelectedPack, wanted: str) -> bool:
    pack_digest = _digest(str(item.payload.get("pack_digest") or ""))
    target = _digest(wanted)
    return _digest(item.digest) == target or (bool(pack_digest) and pack_digest == target)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object: {path}")
    return value


def load_campaign_report_map(path: Path) -> dict[str, str]:
    """Build a deterministic trial_id → pack_digest map from a CampaignReport."""
    value = _read_json_object(path, label="campaign report")
    refs = value.get("source_refs")
    items = refs if isinstance(refs, list) else value.get("items")
    if not isinstance(items, list):
        items = []
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("malformed campaign report source_ref: not an object")
        trial_raw, digest_raw = item.get("trial_id"), item.get("pack_digest")
        if trial_raw is None or not str(trial_raw):
            raise ValueError("malformed campaign report source_ref: missing trial_id")
        if digest_raw is None or not str(digest_raw):
            raise ValueError(
                f"malformed campaign report source_ref for trial {trial_raw}: missing pack_digest"
            )
        trial_id, digest = str(trial_raw), _digest(str(digest_raw))
        previous = mapping.get(trial_id)
        if previous is not None and previous != digest:
            raise ValueError(f"conflicting report pack digests for trial {trial_id}")
        mapping[trial_id] = digest
    return mapping


def load_pack_map(path: Path) -> dict[str, str]:
    """Load a plain JSON object {trial_id: pack_digest}."""
    value = _read_json_object(path, label="pack map")
    mapping: dict[str, str] = {}
    for trial_raw, digest_raw in value.items():
        if not isinstance(digest_raw, str) or not digest_raw:
            raise ValueError(f"malformed pack map entry for trial {trial_raw}")
        mapping[str(trial_raw)] = _digest(digest_raw)
    return mapping


def _discover_sidecars(analyses_dir: Path) -> dict[str, list[SelectedPack]]:
    candidates: dict[str, list[SelectedPack]] = {}
    for path in sorted(analyses_dir.glob("*/*/evidence_pack.json")):
        if not path.is_file():
            continue
        item = SelectedPack(path.parent.parent.name, path.parent.name, path, _read_pack(path))
        candidates.setdefault(item.trial_id, []).append(item)
    return candidates


def _pin_one(
    trial_id: str,
    options: Sequence[SelectedPack],
    wanted: str,
    *,
    source: str,
) -> SelectedPack:
    matches = [item for item in options if _matches_digest(item, wanted)]
    if not matches:
        raise ValueError(f"trial {trial_id} listed in {source} but pack missing on disk")
    if len(matches) > 1:
        raise ValueError(f"duplicate on-disk packs matching {source} entry for trial {trial_id}")
    return matches[0]


def _assert_map_digests(
    selected: Mapping[str, SelectedPack], digest_map: Mapping[str, str], *, source: str
) -> None:
    allowed = {_digest(value) for value in digest_map.values()}
    for trial_id, item in selected.items():
        identities = {_digest(item.digest)}
        payload = item.payload.get("pack_digest")
        if payload:
            identities.add(_digest(str(payload)))
        if identities.isdisjoint(allowed):
            raise ValueError(
                f"selected pack digest for trial {trial_id} is not in the {source} map"
            )


def select_trial_sidecars(
    analyses_dir: Path,
    *,
    pack_digest: str | None = None,
    digest_map: Mapping[str, str] | None = None,
    requested: Sequence[str] | None = None,
    pin_source: str = "report",
) -> dict[str, SelectedPack]:
    """Select exactly one sidecar per trial from a pin map, digest, or unique sidecar.

    Pin maps (campaign-report / pack-map) require exactly one on-disk pack matching
    the mapped digest. Unpinned multi-generation dirs are rejected; there is no
    created_at / dirname fallback.
    """
    grouped = _discover_sidecars(analyses_dir)
    if requested:
        wanted_trials = sorted(set(requested))
    elif digest_map is not None:
        wanted_trials = sorted(digest_map)
    else:
        wanted_trials = sorted(grouped)
    selected: dict[str, SelectedPack] = {}
    if digest_map is not None:
        for trial_id in wanted_trials:
            if trial_id not in digest_map:
                raise ValueError(f"requested trial {trial_id} not listed in {pin_source}")
            selected[trial_id] = _pin_one(
                trial_id, grouped.get(trial_id, ()), digest_map[trial_id], source=pin_source
            )
        _assert_map_digests(selected, digest_map, source=pin_source)
        return dict(sorted(selected.items()))
    if pack_digest is not None:
        wanted = _digest(pack_digest)
        for trial_id in wanted_trials:
            matches = [item for item in grouped.get(trial_id, ()) if _matches_digest(item, wanted)]
            if len(matches) > 1:
                raise ValueError(
                    f"duplicate on-disk packs matching report entry for trial {trial_id}"
                )
            if len(matches) == 1:
                selected[trial_id] = matches[0]
        return dict(sorted(selected.items()))
    for trial_id in wanted_trials:
        options = grouped.get(trial_id, [])
        if len(options) > 1:
            raise ValueError("ambiguous pack generations; supply --campaign-report or --pack-map")
        if len(options) == 1:
            selected[trial_id] = options[0]
    return dict(sorted(selected.items()))


def discover_trial_ids(analyses_dir: Path) -> list[str]:
    return sorted(_discover_sidecars(analyses_dir))


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
    rows: Sequence[Mapping[str, Any]],
    selections: Mapping[str, SelectedPack],
    digest: str,
    *,
    selection_mode: str = "single-generation",
    report_id: str | None = None,
    cas_id: str | None = None,
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
        f"- selection_mode: {selection_mode}",
    ]
    if report_id:
        lines.append(f"- report_id: {report_id}")
    if cas_id:
        lines.append(f"- cas_id: {cas_id}")
    lines.extend(
        [
            "",
            "| trial | digest | created_at | selection_mode |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| `{trial_id}` | `{item.digest}` | {_cell(item.payload.get('created_at'))} | `{selection_mode}` |"
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
    *,
    selection_mode: str = "single-generation",
    report_id: str | None = None,
    cas_id: str | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _rows(findings)
    _validate(rows)
    jsonl = _jsonl(rows)
    digest = hashlib.sha256(jsonl.encode("utf-8")).hexdigest()
    (out_dir / FINDINGS_JSONL).write_text(jsonl, encoding="utf-8")
    (out_dir / FINDINGS_REPORT).write_text(
        _report(
            rows,
            selections,
            digest,
            selection_mode=selection_mode,
            report_id=report_id,
            cas_id=cas_id,
        ),
        encoding="utf-8",
    )
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
    requested = sorted(set(args.trial_ids))
    if args.pack_digest is not None and len(requested) != 1:
        print("error: global digest requires single-trial mode", file=sys.stderr)
        return 1
    if not analyses_dir.is_dir():
        print(f"error: analyses dir not found: {analyses_dir}", file=sys.stderr)
        return 1
    try:
        digest_map: dict[str, str] | None = None
        pin_source = "report"
        selection_mode = "single-generation"
        if args.campaign_report is not None:
            digest_map = load_campaign_report_map(args.campaign_report.expanduser().resolve())
            pin_source = "report"
            selection_mode = "campaign-report"
        elif args.pack_map is not None:
            digest_map = load_pack_map(args.pack_map.expanduser().resolve())
            pin_source = "pack map"
            selection_mode = "pack-map"
        elif args.pack_digest is not None:
            selection_mode = "pack-digest"
        selections = select_trial_sidecars(
            analyses_dir,
            pack_digest=args.pack_digest,
            digest_map=digest_map,
            requested=requested or None,
            pin_source=pin_source,
        )
        missing = [trial for trial in requested if trial not in selections]
        if missing:
            raise ValueError("no selected evidence pack for trial(s): " + ", ".join(missing))
        if requested:
            selections = {trial: selections[trial] for trial in requested}
        write_outputs(
            out_dir,
            collect_findings(analyses_dir, selections),
            selections,
            selection_mode=selection_mode,
            report_id=args.report_id,
            cas_id=args.cas_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
