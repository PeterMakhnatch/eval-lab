"""Export Harbor trials to Docent, keeping continuation and summarization files.

Docent's own Harbor converter rejects any trial with more than one ATIF
document (``trajectory.cont-N.json``) or with ``subagent_trajectory_ref``
(Terminus-2 summarization files), so long MiMo runs would be silently skipped.
This exporter assembles each trial the same way probe-03 does and emits ONE
Docent AgentRun per trial with one transcript per distinct document:

* duplicate          -- a continuation identical to what is already assembled: dropped
* cumulative_superset-- a continuation that starts with every step already assembled:
                        replaces the shorter document (no double counting)
* new_session        -- a continuation that shares only the system prompt: kept as
                        its own transcript, in order
* summarization-*    -- Terminus-2 summarization exchanges: kept as separate
                        transcripts named ``summarization/...``

Reference fields the stock converter refuses (``continued_trajectory_ref``,
``subagent_trajectory_ref``) are removed from the in-memory copy and recorded in
transcript metadata. Raw trial files are never modified.

Run-level metadata gains a flat ``trace_lab`` block (task, reward, stop reason,
assembly pattern, episode coverage, per-document sha256) and, with
``--tags probe-03/<out>/capabilities.jsonl``, the probe-03 tags so Docent
queries can stratify by tag, attribution and treatment key.

Dry run is the default (writes the payload locally). Uploading needs
``--no-dry-run`` and ``DOCENT_API_KEY`` (``keys run -- ...``). Every string in
the payload is secret-scanned first; any hit aborts the upload.

    uv run --no-project --python 3.12 --with docent==0.1.87 python export_harbor.py \\
        <job_or_runs_dir>... --collection NAME [--tags capabilities.jsonl] [--no-dry-run]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

from docent.sdk.integrations.harbor import (
    _attach_harbor_trial_metadata,
    convert_atif_to_agent_run,
    find_harbor_trial_dirs,
)
from docent.sdk.integrations.util import ConversionError

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|dk_[A-Za-z0-9]{10,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|gh[pous]_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|[A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD)[A-Z0-9_]*\s*=\s*[^\s\"'$]{8,}"
    # TOKEN only as a full underscore-delimited segment: TOKENIZER_INFINITY
    # (a public lm_eval constant) must not trip the scan.
    r"|(?:[A-Z0-9_]*_)?TOKENS?(?:_[A-Z0-9_]+)?\s*=\s*[^\s\"'$]{8,})"
)
CONT_RE = re.compile(r"trajectory\.cont-(\d+)\.json$")
SUMM_RE = re.compile(r"trajectory\.summarization-(\d+)-(\w+)\.json$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _step_sig(step: dict) -> tuple:
    return (step.get("source"), json.dumps(step.get("message"), sort_keys=True, default=str))


def _strip_refs(payload: dict) -> tuple[dict, dict]:
    """Remove reference fields Docent's converter rejects; return (copy, removed)."""
    clean = copy.deepcopy(payload)
    removed: dict = {}
    if clean.pop("continued_trajectory_ref", None) is not None:
        removed["continued_trajectory_ref"] = payload.get("continued_trajectory_ref")
    sub_refs = []
    for step in clean.get("steps") or []:
        obs = step.get("observation")
        if not isinstance(obs, dict):
            continue
        for result in obs.get("results") or []:
            if isinstance(result, dict) and result.get("subagent_trajectory_ref"):
                sub_refs.append({"step_id": step.get("step_id"), "ref": result.pop("subagent_trajectory_ref")})
    if sub_refs:
        removed["subagent_trajectory_refs"] = sub_refs
    return clean, removed


def assemble(trial: Path) -> tuple[list[tuple[str, Path, dict]], str, list[str]]:
    """Return ([(transcript_name, path, payload)], assembly_pattern, notes)."""
    agent = trial / "agent"
    head = agent / "trajectory.json"
    conts = sorted(
        (p for p in agent.glob("trajectory.cont-*.json") if CONT_RE.search(p.name)),
        key=lambda p: int(CONT_RE.search(p.name).group(1)),
    )
    mainline: list[tuple[str, Path, dict]] = []
    patterns: list[str] = []
    notes: list[str] = []
    if head.is_file():
        mainline.append(("main", head, json.loads(head.read_text(encoding="utf-8"))))
    else:
        patterns.append("head_missing")
    for cont in conts:
        payload = json.loads(cont.read_text(encoding="utf-8"))
        if not mainline:
            mainline.append((cont.stem.replace("trajectory.", ""), cont, payload))
            continue
        prev_sigs = [_step_sig(s) for s in mainline[-1][2].get("steps") or []]
        sigs = [_step_sig(s) for s in payload.get("steps") or []]
        if sigs[: len(prev_sigs)] == prev_sigs:
            if len(sigs) == len(prev_sigs):
                patterns.append("duplicate")
                notes.append(f"{cont.name} duplicates {mainline[-1][1].name}; dropped")
            else:
                patterns.append("cumulative_superset")
                notes.append(f"{cont.name} extends {mainline[-1][1].name}; replaced it")
                name = mainline[-1][0]
                mainline[-1] = (name, cont, payload)
        else:
            patterns.append("new_session")
            mainline.append((cont.stem.replace("trajectory.", ""), cont, payload))
    summaries = sorted(
        (p for p in agent.glob("trajectory.summarization-*.json") if SUMM_RE.search(p.name)),
        key=lambda p: (int(SUMM_RE.search(p.name).group(1)), p.name),
    )
    docs = list(mainline)
    for summ in summaries:
        docs.append((f"summarization/{summ.stem.replace('trajectory.summarization-', '')}", summ,
                     json.loads(summ.read_text(encoding="utf-8"))))
    pattern = "+".join(dict.fromkeys(patterns)) or "single_head"
    return docs, pattern, notes


def convert_trial(trial: Path, root: Path, tags: dict[str, dict]):
    docs, pattern, notes = assemble(trial)
    if not docs:
        raise ConversionError(f"{trial}: no ATIF documents under agent/")
    runs = []
    doc_meta = []
    for name, path, payload in docs:
        clean, removed = _strip_refs(payload)
        sub_run = convert_atif_to_agent_run(clean)
        for transcript in sub_run.transcripts:
            transcript.name = name
            transcript.metadata = {**(transcript.metadata or {}), "trace_lab_source_file": path.name,
                                   "trace_lab_removed_refs": removed or None}
        runs.append(sub_run)
        doc_meta.append({"file": path.name, "transcript": name, "sha256": _sha256(path),
                         "steps": len(payload.get("steps") or [])})
    run = runs[0]
    for extra in runs[1:]:
        run.transcripts.extend(extra.transcripts)
    config = json.loads((trial / "config.json").read_text(encoding="utf-8"))
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    _attach_harbor_trial_metadata(run, root=root, trajectory_path=docs[0][1], trial_dir=trial,
                                  config_payload=config, result_payload=result)
    agent_result = result.get("agent_result") or {}
    exception = result.get("exception_info") or {}
    mainline_agent_steps = sum(
        sum(1 for s in p.get("steps") or [] if s.get("source") == "agent")
        for n, _, p in docs if not n.startswith("summarization/")
    )
    row = tags.get(trial.name, {})
    block = {
        "trial": trial.name,
        "task": row.get("task") or result.get("task_name"),
        "reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
        "exception_type": exception.get("exception_type"),
        "n_episodes": (agent_result.get("metadata") or {}).get("n_episodes"),
        "mainline_agent_steps": mainline_agent_steps,
        "assembly_pattern": pattern,
        "assembly_notes": notes,
        "documents": doc_meta,
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
    }
    if row:
        for key in ("first_failure", "outcome_relevant_failure"):
            failure = row.get(key)
            if isinstance(failure, dict):
                block[key] = {k: failure.get(k) for k in ("tag", "attribution", "rule_id", "step_ref", "recovered")}
        block["stop_reason"] = (row.get("stop") or {}).get("reason")
        block["treatment_key"] = (row.get("treatment") or {}).get("key")
        block["treatment_key_source"] = (row.get("treatment") or {}).get("key_source")
        block["grader_note"] = row.get("grader_note")
    run.merge_metadata({"trace_lab": block})
    return run


def secret_hits(payload: object) -> list[str]:
    hits: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            hits.extend(m.group(0)[:12] + "..." for m in SECRET_RE.finditer(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+", help="Harbor job dirs or a runs/ dir holding them")
    parser.add_argument("--collection", required=True, help="Docent collection name")
    parser.add_argument("--tags", help="probe-03 capabilities.jsonl to attach")
    parser.add_argument("--dry-run", dest="dry_run", action=argparse.BooleanOptionalAction, default=True,
                        help="write the payload locally (default); --no-dry-run uploads")
    parser.add_argument("--out", help="payload path for dry runs")
    args = parser.parse_args(argv)

    tags: dict[str, dict] = {}
    if args.tags:
        for line in Path(args.tags).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                tags[row["trial"]] = row

    runs, skipped = [], []
    for root in map(Path, args.roots):
        for trial in find_harbor_trial_dirs(root):
            try:
                runs.append(convert_trial(trial, root, tags))
            except ConversionError as exc:
                skipped.append(f"{trial.name}: {exc}")
    for line in skipped:
        print(f"skipped {line}", file=sys.stderr)
    transcripts = sum(len(r.transcripts) for r in runs)
    messages = sum(len(t.messages) for r in runs for t in r.transcripts)
    print(f"{len(runs)} runs, {transcripts} transcripts, {messages} messages, {len(skipped)} skipped")

    payload = [run.model_dump(mode="json") for run in runs]
    hits = secret_hits(payload)
    if hits:
        print(f"refusing: {len(hits)} secret-like strings, e.g. {sorted(set(hits))[:5]}", file=sys.stderr)
        return 3
    if args.dry_run:
        out = Path(args.out or f"{re.sub(r'[^A-Za-z0-9._-]+', '-', args.collection)}-payload.json")
        out.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        print(f"dry run: wrote {out} (nothing uploaded)")
        return 0

    from docent import Docent

    client = Docent()
    collection_id = client.create_collection(
        name=args.collection,
        description=f"{len(runs)} Harbor trials; continuation-aware trace-lab export",
    )
    client.add_agent_runs(collection_id, runs)
    print(f"uploaded {len(runs)} runs: https://docent.transluce.org/dashboard/{collection_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
