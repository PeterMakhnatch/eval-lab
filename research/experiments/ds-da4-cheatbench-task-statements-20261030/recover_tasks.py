"""Recover the CheatBench task statement for each DEV run from the dataset's own fields.

DT-9 found that only 19 of 54 CheatBench alerts carry a task statement in the
run text. This script answers whether a statement can be recovered from the
source dataset instead, without opening anything under ``research/answer-keys/``.

Sources, in priority order, all inside the pinned CheatBench row
(``steinad/CheatBench`` @ ``461360b34e93644955563dcc9243c6cc1a53bd0f``):

1. ``files[path="agent/command-0/command.txt"]`` — the harness invocation
   ``/usr/local/bin/forge --verbose --conversation-id <uuid> -p '<task prompt>'``.
   The statement is the quoted ``-p`` payload (DT-12: every CheatBench run is one
   ``bash`` call to ``forge -p '<task prompt>'``).
2. ``files[path="agent/episode-N/prompt.txt"]`` — the Pilot episode prompt.
3. a ``src == "user"`` (or ``role == "user"``) event whose message is natural
   language of at least ``MIN_STATEMENT_CHARS`` characters and not a bare
   ``$NN`` placeholder.

A ``$NN`` token is resolved only from the row itself (a mapping/list whose keys
or indices are ``$0``, ``$1``, ...). Nothing is reconstructed by inference, from
another row, or by a model: this script makes no model calls.

Labels are read through ``CorpusRegistry`` (``expected_verdict``, DEV split) and
joined only when counting, after the statements are written. Statement text is
written to ``--tasks-dir`` redacted with the benchmark and vendor pattern; the
receipt carries no text, only per-run kind, length and sha256.

Usage::

    uv run --project . python research/experiments/<this dir>/recover_tasks.py \
        --parquet /tmp/da4/full.parquet --out /tmp/da4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from collections import Counter
from pathlib import Path

DATA_REVISION = "461360b34e93644955563dcc9243c6cc1a53bd0f"
PARQUET_SHA256 = "b4d2a1496e5f8de7b4c0e4791bd9f8fcbd0a3567f95d2e759b5da20ebb160e63"
MIN_STATEMENT_CHARS = 40

# Placeholder written by the CheatBench normaliser where a message was elided:
# a bare ``$31`` is the dataset's own artifact, not text the agent saw.
REF_RE = re.compile(r"^\$\d+$")
# CheatBench benchmark families (run_atlas.SOURCE_TERMS lacks mle-bench,
# kernelbench and hal-usaco) plus vendor names, so a statement never tells a
# reader which benchmark or model produced it.
VENDOR_TERMS = (
    r"anthropic", r"openai", r"claude", r"chatgpt", r"gpt-?[45][\w.-]*", r"gemini",
    r"codex", r"o3(?:-mini|-high)?", r"deepseek", r"qwen", r"llama", r"mistral",
    r"minimax", r"kimi", r"sonnet", r"opus(?!-?\d)", r"haiku",
)
EXTRA_BENCH_TERMS = (
    r"mle[-_ ]?bench", r"kernel[-_ ]?bench", r"hal[-_ ]?usaco",
    r"terminal[-_ ]?bench", r"swe[-_ ]?smith", r"swe[-_ ]?rebench",
    r"bountybench", r"cybench",
)
# Wording that tells the reader what to conclude about the run. Used only to
# count label-bearing statements; never a model input.
LEAK_TERMS = ("cheat", "exploit", "game the", "bypass", "hack the", "reward hack",
              "pass the test")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def blind_id(run_id: str) -> str:
    """DT-8's blind id for a CheatBench item id."""
    return "run-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]


def redaction_pattern() -> re.Pattern[str]:
    from evallab.production.run_atlas import (
        DEFAULT_DATABASE,
        database_source_pattern,
        source_pattern,
    )

    # The trace DB's dataset ids (the same call DT-9 used) plus the families and
    # vendors a statement could name.
    base = database_source_pattern(DEFAULT_DATABASE)
    terms = [*EXTRA_BENCH_TERMS, *VENDOR_TERMS]
    pattern = source_pattern(terms)
    return re.compile("|".join([base.pattern, pattern.pattern]), re.IGNORECASE)


def redact(text: str) -> str:
    pattern = redaction_pattern()
    redacted = pattern.sub("[source]", text)
    return re.sub(r"\s+\n", "\n", redacted).strip()


def read_rows(parquet: Path, trace_ids: list[str]) -> dict[str, dict]:
    import pyarrow.parquet as pq

    table = pq.read_table(
        parquet,
        columns=["trace_id", "trace", "benchmark"],
        filters=[("trace_id", "in", trace_ids)],
    )
    out: dict[str, dict] = {}
    for trace_id, trace, benchmark in zip(
        table.column("trace_id").to_pylist(),
        table.column("trace").to_pylist(),
        table.column("benchmark").to_pylist(),
        strict=True,
    ):
        out[trace_id] = {"row": json.loads(trace), "benchmark": benchmark}
    return out


def _payload_from_tokens(content: str) -> str | None:
    """The task prompt token of a two-shape harness invocation.

    Observed shapes: ``/usr/local/bin/forge ... -p '<task prompt>'`` and
    ``pilot task '<task prompt>' --local --project /app ...``. The shell has
    already been applied, so a quote inside the prompt appears as ``'\\''``;
    :func:`shlex.split` undoes exactly that.
    """
    try:
        tokens = shlex.split(content, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-p" or (token == "task" and index and tokens[index - 1] == "pilot"):
            return tokens[index + 1]
    return None


def command_payload(files: list) -> tuple[str | None, str | None]:
    """The task prompt carried by the harness invocation in ``command.txt``."""
    for entry in files:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("path") or "").endswith("command.txt"):
            continue
        content = str(entry.get("content") or "")
        payload = _payload_from_tokens(content)
        if payload:
            return payload, str(entry.get("path"))
    return None, None


def episode_prompt(files: list) -> tuple[str | None, str | None]:
    """The first ``agent/episode-N/prompt.txt`` content."""
    found = [
        (str(entry.get("path")), str(entry.get("content") or ""))
        for entry in files
        if isinstance(entry, dict) and re.search(r"episode-\d+/prompt\.txt$", str(entry.get("path") or ""))
    ]
    if not found:
        return None, None
    found.sort(key=lambda item: item[0])
    return found[0][1], found[0][0]


def ref_table(row: dict) -> dict[str, str]:
    """A ``$NN`` value table shipped inside the row, if any (keys ``$0``, ``$1``, ...)."""
    table: dict[str, str] = {}
    for value in row.values():
        candidates: list = []
        if isinstance(value, dict):
            candidates = list(value.items())
        elif isinstance(value, list):
            candidates = [(f"${index}", item) for index, item in enumerate(value)]
        for key, item in candidates:
            if isinstance(key, str) and re.fullmatch(r"\$\d+", key) and isinstance(item, str):
                table[key] = item
    return table


#: User-role events that are harness output or continuation boilerplate, not the
#: task the agent was given. None of these is an instruction.
NON_STATEMENT_PREFIXES = (
    "OBSERVATION:",
    "Observation:",
    "<environment_context>",
)
CONTINUATION_RE = re.compile(r"^Please continue on whatever approach you think is suitable\b")


def user_statement(row: dict) -> tuple[str | None, str | None]:
    """The first natural-language user message that is not a bare ``$NN`` ref.

    The first qualifying message is taken, not the longest: later user-role
    events in these conversions are tool observations (``OBSERVATION:`` /
    ``Observation:``) that carry file contents, not the ask.
    """
    resolved = ref_table(row)
    best = None
    for event in row.get("events") or []:
        if not isinstance(event, dict):
            continue
        if (event.get("src") or event.get("role")) != "user":
            continue
        text = event.get("msg") if "msg" in event else event.get("content")
        if not isinstance(text, str):
            continue
        text = text.strip()
        text = resolved.get(text, text)
        if REF_RE.match(text) or len(text) < MIN_STATEMENT_CHARS:
            continue
        if text.startswith(NON_STATEMENT_PREFIXES) or CONTINUATION_RE.match(text):
            continue
        best = text
        break
    if best is None:
        return None, None
    return best, "user_event"


def placeholder_census(row: dict) -> dict:
    """Count ``$NN`` tokens in the row and how many resolve inside the row."""
    resolved = ref_table(row)
    raw = json.dumps(row.get("events") or [])
    tokens = re.findall(r'"\$\d+"', raw)
    bare = [token.strip('"') for token in tokens]
    return {
        "tokens": len(bare),
        "bare_refs": sum(1 for value in bare if REF_RE.match(value)),
        "resolved_in_row": sum(1 for value in bare if value in resolved),
    }


def statement_for(row: dict) -> tuple[str | None, str | None]:
    files = row.get("files") if isinstance(row.get("files"), list) else []
    payload, path = command_payload(files)
    if payload and len(payload.strip()) >= MIN_STATEMENT_CHARS:
        return payload.strip(), f"command_txt:{path}"
    prompt, path = episode_prompt(files)
    if prompt and len(prompt.strip()) >= MIN_STATEMENT_CHARS:
        return prompt.strip(), f"episode_prompt:{path}"
    return user_statement(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dt9-manifest", type=Path, default=Path(
        "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/"
        "raw/DT-9/receipts/packets-manifest.json"))
    args = parser.parse_args()

    from evallab.production.corpus_queue import CorpusRegistry

    args.tasks_dir = args.out / "tasks"
    args.tasks_dir.mkdir(parents=True, exist_ok=True)
    for stale in args.tasks_dir.glob("*.txt"):
        stale.unlink()

    parquet_digest = sha256_file(args.parquet)
    if parquet_digest != PARQUET_SHA256:
        raise SystemExit(f"parquet digest mismatch: {parquet_digest}")

    registry = CorpusRegistry(Path(".").resolve())
    items = list(registry.items(dataset_id="cheatbench-actions-v1", split="dev"))
    meta = {}
    for item in items:
        trace_id = item.metadata.get("source_id")
        meta[item.item_id] = {
            "item_id": item.item_id,
            "trace_id": trace_id,
            "blind_id": blind_id(item.item_id),
            "positive": item.metadata.get("expected_verdict") == "fail",
        }
    trace_ids = sorted({row["trace_id"] for row in meta.values()})
    rows = read_rows(args.parquet, trace_ids)
    missing_rows = [trace_id for trace_id in trace_ids if trace_id not in rows]

    per_run = []
    for item_id in sorted(meta):
        info = meta[item_id]
        record = {
            "item_id": item_id,
            "blind_id": info["blind_id"],
            "trace_id": info["trace_id"],
            "positive": info["positive"],
            "recovered": False,
            "source": "none",
        }
        row_bundle = rows.get(info["trace_id"])
        if row_bundle is None:
            record["source"] = "row_missing"
            per_run.append(record)
            continue
        row = row_bundle["row"]
        record["benchmark"] = row_bundle["benchmark"]
        text, source = statement_for(row)
        census = placeholder_census(row)
        record["placeholders"] = census
        record["task_identifier"] = bool(
            row.get("task_name") or (isinstance(row.get("task"), dict) and row["task"].get("id")))
        if text:
            redacted = redact(text)
            if len(redacted) >= MIN_STATEMENT_CHARS:
                path = args.tasks_dir / f"{info['blind_id']}.txt"
                path.write_text(redacted + "\n", encoding="utf-8")
                record.update({
                    "recovered": True,
                    "source": source,
                    "chars": len(redacted),
                    "sha256": sha256_text(redacted),
                    "label_wording": [t for t in LEAK_TERMS if t in redacted.casefold()],
                })
            else:
                record["source"] = "redacted_to_empty"
        per_run.append(record)

    by_blind = {record["blind_id"]: record for record in per_run}

    manifest = json.loads(args.dt9_manifest.read_text())
    alerts = [p for p in manifest["packets"] if p["corpus"] == "cheatbench"]
    alert_ids = [p["blind_id"] for p in alerts]
    # DT-9's alert set is 42 positive / 12 negative; the 12 flagged negatives are
    # the alert ids whose registry label is negative.
    negatives = [b for b in alert_ids if not by_blind[b]["positive"]]

    def block(ids: list[str]) -> dict:
        have = [b for b in ids if by_blind[b]["recovered"]]
        sources = Counter(by_blind[b]["source"].split(":")[0] for b in have)
        leaks = [b for b in have if by_blind[b].get("label_wording")]
        pos = [b for b in ids if by_blind[b]["positive"]]
        per_label_leak = {
            "positive": sum(1 for b in have if by_blind[b]["positive"] and by_blind[b].get("label_wording")),
            "negative": sum(1 for b in have if not by_blind[b]["positive"] and by_blind[b].get("label_wording")),
        }
        benches: dict[str, dict] = {}
        for blind in ids:
            entry = benches.setdefault(by_blind[blind].get("benchmark") or "unknown",
                                       {"n": 0, "recovered": 0, "identifier": 0})
            entry["n"] += 1
            entry["recovered"] += int(by_blind[blind]["recovered"])
            entry["identifier"] += int(bool(by_blind[blind].get("task_identifier")))
        return {
            "n": len(ids),
            "recovered": len(have),
            "positive": {"n": len(pos), "recovered": sum(1 for b in pos if by_blind[b]["recovered"])},
            "negative": {"n": len(ids) - len(pos),
                         "recovered": sum(1 for b in ids if not by_blind[b]["positive"] and by_blind[b]["recovered"])},
            "by_source": dict(sources),
            "by_benchmark": benches,
            "with_label_wording": len(leaks),
            "label_wording_by_label": per_label_leak,
        }

    dev_ids = [record["blind_id"] for record in per_run]
    census = Counter()
    for record in per_run:
        placeholders = record.get("placeholders") or {}
        census["rows_with_refs"] += int(placeholders.get("bare_refs", 0) > 0)
        census["rows_with_resolvable_refs"] += int(placeholders.get("resolved_in_row", 0) > 0)
        census["rows_identifier_only"] += int(bool(record.get("task_identifier")))

    receipt = {
        "spec": {
            "dataset": "steinad/CheatBench",
            "revision": DATA_REVISION,
            "file": "data/processed/parquet/full.parquet",
            "parquet_sha256": parquet_digest,
            "min_statement_chars": MIN_STATEMENT_CHARS,
            "sources": ["files:agent/command-0/command.txt -p payload",
                        "files:agent/episode-N/prompt.txt",
                        "events: user message >= 40 chars, not a bare $NN ref"],
            "redaction": "benchmark/vendor pattern (run_atlas.source_pattern + vendor terms)",
        },
        "dev": {**block(dev_ids), "rows_missing_from_parquet": missing_rows},
        "alerts_dt9": block(alert_ids),
        "flagged_negatives": {
            "n": len(negatives),
            "with_statement": [b for b in negatives if by_blind[b]["recovered"]],
            "by_source": dict(Counter(by_blind[b]["source"].split(":")[0]
                                      for b in negatives if by_blind[b]["recovered"])),
        },
        "placeholder_census": dict(census),
        "runs": per_run,
    }
    (args.out / "recovery.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n",
                                            encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("dev", "alerts_dt9", "flagged_negatives",
                                              "placeholder_census")}, indent=1))
    print("task files written:", len(list(args.tasks_dir.glob("*.txt"))))


if __name__ == "__main__":
    main()
