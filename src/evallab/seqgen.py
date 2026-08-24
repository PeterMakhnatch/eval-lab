"""Deterministic, sequence-first synthetic Harbor-task generator (SEQGEN v0).

Generates valid tool sequences over a synthetic record-pipeline domain, selects
for maximal op-bigram coverage, and instantiates self-contained Harbor task packages.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.schemas import ProvenanceMetadata

SEQGEN_VERSION = "0.1.0"
TRANSFORM_ID = "seqgen@0.1.0"
BASE_IMAGE = (
    "python:3.13-slim-bookworm@"
    "sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)

# --- Domain Specification ---------------------------------------------------
DOMAIN_SPEC: dict[str, Any] = {
    "name": "linear_record_pipeline",
    "version": "0.1.0",
    "initial_schema": {
        "id": "int",
        "region": "str",
        "status": "str",
        "amount": "int",
        "day": "int",
    },
    "value_pools": {
        "region": ["north", "south", "east", "west"],
        "status": ["shipped", "pending", "cancelled", "returned"],
        "amount": {"min": 5, "max": 500},
        "day": {"min": 1, "max": 28},
    },
    "ops": [
        "filter_eq",
        "filter_ge",
        "select",
        "sort_by",
        "dedupe_by",
        "head",
        "group_sum",
    ],
    "terminal_op": "write",
}

# --- Single Source of Truth for Record-Pipeline Tool (RP_SOURCE) ------------
RP_SOURCE = '''#!/usr/bin/env python3
"""Self-contained record-pipeline tool for linear JSONL tasks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def apply_filter_eq(rows: list[dict[str, Any]], field: str, value: str) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get(field)) == str(value)]


def apply_filter_ge(rows: list[dict[str, Any]], field: str, value: int) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if int(row.get(field, 0)) >= int(value)]


def apply_select(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    return [{k: row[k] for k in fields if k in row} for row in rows]


def apply_sort_by(
    rows: list[dict[str, Any]], field: str, order: str = "asc"
) -> list[dict[str, Any]]:
    descending = order.lower() == "desc"
    return sorted([dict(r) for r in rows], key=lambda r: r.get(field), reverse=descending)


def apply_dedupe_by(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    result: list[dict[str, Any]] = []
    for r in rows:
        val = r.get(field)
        if val not in seen:
            seen.add(val)
            result.append(dict(r))
    return result


def apply_head(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return [dict(r) for r in rows[:n]]


def apply_group_sum(
    rows: list[dict[str, Any]], group_field: str, value_field: str
) -> list[dict[str, Any]]:
    sums: dict[str, int] = {}
    for r in rows:
        g = str(r.get(group_field))
        v = int(r.get(value_field, 0))
        sums[g] = sums.get(g, 0) + v
    total_key = f"total_{value_field}"
    return [{group_field: g, total_key: sums[g]} for g in sorted(sums.keys())]


def apply_write(rows: list[dict[str, Any]], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            line = json.dumps(row, sort_keys=True, separators=(",", ":"))
            f.write(line + "\\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Record pipeline tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # filter-eq
    p_feq = subparsers.add_parser("filter-eq", help="Filter rows where field equals value")
    p_feq.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_feq.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_feq.add_argument("--field", required=True, help="Field name")
    p_feq.add_argument("--value", required=True, help="Value to match")

    # filter-ge
    p_fge = subparsers.add_parser("filter-ge", help="Filter rows where field >= value")
    p_fge.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_fge.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_fge.add_argument("--field", required=True, help="Field name")
    p_fge.add_argument("--value", type=int, required=True, help="Integer threshold")

    # select
    p_sel = subparsers.add_parser("select", help="Select and order fields")
    p_sel.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_sel.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_sel.add_argument("--fields", nargs="+", required=True, help="Fields to keep")

    # sort-by
    p_srt = subparsers.add_parser("sort-by", help="Sort rows by field")
    p_srt.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_srt.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_srt.add_argument("--field", required=True, help="Field name")
    p_srt.add_argument("--order", choices=["asc", "desc"], default="asc", help="Sort order")

    # dedupe-by
    p_ddp = subparsers.add_parser("dedupe-by", help="Deduplicate rows by first seen field value")
    p_ddp.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_ddp.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_ddp.add_argument("--field", required=True, help="Field name")

    # head
    p_hd = subparsers.add_parser("head", help="Keep first N rows")
    p_hd.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_hd.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_hd.add_argument("-n", "--n", type=int, required=True, help="Number of rows")

    # group-sum
    p_grp = subparsers.add_parser("group-sum", help="Group by string field and sum integer field")
    p_grp.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_grp.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")
    p_grp.add_argument("--group-field", required=True, help="Group key field")
    p_grp.add_argument("--value-field", required=True, help="Value field to sum")

    # write
    p_wrt = subparsers.add_parser("write", help="Write rows to output path in canonical form")
    p_wrt.add_argument("--in", dest="in_path", required=True, help="Input JSONL path")
    p_wrt.add_argument("--out", dest="out_path", required=True, help="Output JSONL path")

    args = parser.parse_args()
    rows = read_jsonl(args.in_path)

    if args.command == "filter-eq":
        out_rows = apply_filter_eq(rows, args.field, args.value)
    elif args.command == "filter-ge":
        out_rows = apply_filter_ge(rows, args.field, args.value)
    elif args.command == "select":
        fields = args.fields
        if len(fields) == 1 and "," in fields[0]:
            fields = [f.strip() for f in fields[0].split(",") if f.strip()]
        out_rows = apply_select(rows, fields)
    elif args.command == "sort-by":
        out_rows = apply_sort_by(rows, args.field, args.order)
    elif args.command == "dedupe-by":
        out_rows = apply_dedupe_by(rows, args.field)
    elif args.command == "head":
        out_rows = apply_head(rows, args.n)
    elif args.command == "group-sum":
        out_rows = apply_group_sum(rows, args.group_field, args.value_field)
    elif args.command == "write":
        out_rows = rows
    else:
        sys.exit(f"Unknown command: {args.command}")

    apply_write(out_rows, args.out_path)


if __name__ == "__main__":
    main()
'''


def _load_rp_functions() -> dict[str, Any]:
    """Execute RP_SOURCE in an isolated namespace to obtain pure op functions."""
    namespace: dict[str, Any] = {}
    exec(RP_SOURCE, namespace)
    return namespace


_RP = _load_rp_functions()


# --- Dataset Generation -----------------------------------------------------
def generate_dataset(rng: random.Random) -> list[dict[str, Any]]:
    """Generate a deterministic synthetic orders dataset (40-60 rows)."""
    num_rows = rng.randint(40, 60)
    regions = DOMAIN_SPEC["value_pools"]["region"]
    statuses = DOMAIN_SPEC["value_pools"]["status"]
    amount_cfg = DOMAIN_SPEC["value_pools"]["amount"]
    day_cfg = DOMAIN_SPEC["value_pools"]["day"]

    rows: list[dict[str, Any]] = []
    for i in range(num_rows):
        rows.append(
            {
                "id": i + 1,
                "region": rng.choice(regions),
                "status": rng.choice(statuses),
                "amount": rng.randint(amount_cfg["min"], amount_cfg["max"]),
                "day": rng.randint(day_cfg["min"], day_cfg["max"]),
            }
        )
    return rows


# --- Preconditions and Op Enumeration ---------------------------------------
def enumerate_valid_ops(
    schema: dict[str, str],
    rows: list[dict[str, Any]],
    prev_op_args: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Enumerate all valid (op, args) transitions from the current state."""
    valid: list[dict[str, Any]] = []
    str_fields = [f for f, t in schema.items() if t == "str"]
    int_fields_not_id = [f for f, t in schema.items() if t == "int" and f != "id"]
    current_fields = list(schema.keys())

    # 1. filter_eq — must strictly shrink: a filter that keeps every row (for
    # example repeating a value the rows were already filtered or deduped to)
    # is a vacuous step.
    for f in str_fields:
        vals = sorted({str(r[f]) for r in rows if f in r})
        for v in vals:
            res = _RP["apply_filter_eq"](rows, f, v)
            if 0 < len(res) < len(rows):
                valid.append({"op": "filter_eq", "args": {"field": f, "value": v}})

    # 2. filter_ge
    for f in int_fields_not_id:
        vals = sorted({int(r[f]) for r in rows if f in r})
        for v in vals:
            res = _RP["apply_filter_ge"](rows, f, v)
            if 0 < len(res) < len(rows):
                valid.append({"op": "filter_ge", "args": {"field": f, "value": v}})

    # 3. select (proper subset of current fields, len >= 2)
    if len(current_fields) >= 3:
        for k in range(2, len(current_fields)):
            for subset in itertools.combinations(current_fields, k):
                valid.append({"op": "select", "args": {"fields": list(subset)}})

    # 4. sort_by — only when it changes the row order; sorting rows that are
    # already in the target order is a vacuous step.
    for f in current_fields:
        for order in ("asc", "desc"):
            res = _RP["apply_sort_by"](rows, f, order)
            if res != rows:
                valid.append({"op": "sort_by", "args": {"field": f, "order": order}})

    # 5. dedupe_by
    for f in current_fields:
        res = _RP["apply_dedupe_by"](rows, f)
        if 0 < len(res) < len(rows):
            valid.append({"op": "dedupe_by", "args": {"field": f}})

    # 6. head (n in {3, 5, 10})
    for n in (3, 5, 10):
        if n < len(rows):
            valid.append({"op": "head", "args": {"n": n}})

    # 7. group_sum — only when it actually aggregates: if the rows are already
    # unique per group value, the op is an identity modulo a field rename.
    for g in str_fields:
        for v in int_fields_not_id:
            res = _RP["apply_group_sum"](rows, g, v)
            if 0 < len(res) < len(rows):
                valid.append({"op": "group_sum", "args": {"group_field": g, "value_field": v}})

    # Filter out consecutive duplicate (op, args), and forbid a sort_by that
    # immediately follows a sort_by on the same field: with a stable sort the
    # second ordering fully overrides the first, so the pair is a no-op
    # composition that pads sequence length without adding verifiable work.
    if prev_op_args is not None:
        prev_op, prev_args_json = prev_op_args
        prev_args = json.loads(prev_args_json)

        def _degenerate(op_dict: dict[str, Any]) -> bool:
            same_op_args = (
                op_dict["op"] == prev_op
                and json.dumps(op_dict["args"], sort_keys=True) == prev_args_json
            )
            same_field_resort = (
                prev_op == "sort_by"
                and op_dict["op"] == "sort_by"
                and op_dict["args"]["field"] == prev_args.get("field")
            )
            return same_op_args or same_field_resort

        valid = [op_dict for op_dict in valid if not _degenerate(op_dict)]

    return valid


def apply_op_to_state(
    schema: dict[str, str], rows: list[dict[str, Any]], op: str, args: dict[str, Any]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Apply an operation to the current schema and rows using _RP functions."""
    if op == "filter_eq":
        next_rows = _RP["apply_filter_eq"](rows, args["field"], args["value"])
        next_schema = dict(schema)
    elif op == "filter_ge":
        next_rows = _RP["apply_filter_ge"](rows, args["field"], args["value"])
        next_schema = dict(schema)
    elif op == "select":
        next_rows = _RP["apply_select"](rows, args["fields"])
        next_schema = {f: schema[f] for f in args["fields"] if f in schema}
    elif op == "sort_by":
        next_rows = _RP["apply_sort_by"](rows, args["field"], args.get("order", "asc"))
        next_schema = dict(schema)
    elif op == "dedupe_by":
        next_rows = _RP["apply_dedupe_by"](rows, args["field"])
        next_schema = dict(schema)
    elif op == "head":
        next_rows = _RP["apply_head"](rows, args["n"])
        next_schema = dict(schema)
    elif op == "group_sum":
        g_field = args["group_field"]
        v_field = args["value_field"]
        next_rows = _RP["apply_group_sum"](rows, g_field, v_field)
        next_schema = {g_field: "str", f"total_{v_field}": "int"}
    else:
        raise ValueError(f"Unknown op: {op}")

    return next_schema, next_rows


# --- Reachability & Bigram Coverage -----------------------------------------
ALL_OPS = DOMAIN_SPEC["ops"]  # 7 ops


def compute_reachable_bigrams() -> set[tuple[str, str]]:
    """Compute the statically reachable op-type bigrams.

    Rule:
    - Every op can transition to 'write' (terminal).
    - Between the 7 ops: any op can follow any op EXCEPT:
      * 'select' cannot follow 'group_sum' — group_sum produces exactly 2
        fields, while select requires a proper subset of length >= 2 (which
        requires >= 3 fields);
      * 'group_sum' cannot follow 'group_sum' — the first leaves one row per
        group value, so a second aggregation over the only remaining str field
        never does work (the does-work precondition refuses it).
    Total reachable: 7*7 - 2 + 7 = 54 bigrams.
    """
    reachable: set[tuple[str, str]] = set()
    excluded = {("group_sum", "select"), ("group_sum", "group_sum")}
    for op1 in ALL_OPS:
        # Terminal transition
        reachable.add((op1, "write"))
        for op2 in ALL_OPS:
            if (op1, op2) in excluded:
                continue
            reachable.add((op1, op2))
    return reachable


def extract_bigrams(sequence: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extract consecutive op-type pairs including terminal 'write'."""
    op_types = [step["op"] for step in sequence] + ["write"]
    return [(op_types[i], op_types[i + 1]) for i in range(len(op_types) - 1)]


def extract_unigrams(sequence: list[dict[str, Any]]) -> set[str]:
    """Extract distinct op-types in sequence (excluding terminal write)."""
    return {step["op"] for step in sequence}


# --- Candidate Generation ---------------------------------------------------
class Candidate:
    def __init__(
        self,
        index: int,
        seed: int,
        dataset: list[dict[str, Any]],
        sequence: list[dict[str, Any]],
        output_rows: list[dict[str, Any]],
    ) -> None:
        self.index = index
        self.seed = seed
        self.dataset = dataset
        self.sequence = sequence
        self.output_rows = output_rows
        self.unigrams = extract_unigrams(sequence)
        self.bigrams = extract_bigrams(sequence)

    @property
    def sequence_key(self) -> str:
        return json.dumps(self.sequence, sort_keys=True)


def generate_candidate_pool(
    master_seed: int, pool_size: int, max_attempts: int = 500
) -> list[Candidate]:
    """Generate `pool_size` distinct valid sequence candidates deterministically."""
    candidates: list[Candidate] = []
    seen_seq_keys: set[str] = set()

    for attempt in range(max_attempts):
        if len(candidates) >= pool_size:
            break

        candidate_seed = master_seed * 10007 + attempt
        rng = random.Random(candidate_seed)
        dataset = generate_dataset(rng)

        target_len = rng.randint(3, 6)
        current_schema = dict(DOMAIN_SPEC["initial_schema"])
        current_rows = [dict(r) for r in dataset]
        seq: list[dict[str, Any]] = []

        valid_path = True
        prev_op_args: tuple[str, str] | None = None

        for _ in range(target_len):
            valid_next = enumerate_valid_ops(current_schema, current_rows, prev_op_args)
            if not valid_next:
                valid_path = False
                break
            chosen = rng.choice(valid_next)
            op = chosen["op"]
            args = chosen["args"]
            current_schema, current_rows = apply_op_to_state(current_schema, current_rows, op, args)
            seq.append({"op": op, "args": args})
            prev_op_args = (op, json.dumps(args, sort_keys=True))

        if not valid_path or len(seq) < 3 or len(current_rows) == 0:
            continue
        if len({step["op"] for step in seq}) < 2:
            continue

        cand = Candidate(
            index=len(candidates),
            seed=candidate_seed,
            dataset=dataset,
            sequence=seq,
            output_rows=current_rows,
        )

        if cand.sequence_key in seen_seq_keys:
            continue

        seen_seq_keys.add(cand.sequence_key)
        candidates.append(cand)

    if len(candidates) < pool_size:
        raise RuntimeError(f"Could only generate {len(candidates)} candidates (needed {pool_size})")

    return candidates


# --- Greedy Bigram-Coverage Selection ---------------------------------------
def select_candidates_by_coverage(
    candidates: list[Candidate], count: int
) -> tuple[list[Candidate], dict[str, Any]]:
    """Greedily select `count` candidates maximizing newly covered bigrams."""
    selected: list[Candidate] = []
    covered_bigrams: set[tuple[str, str]] = set()
    covered_unigrams: set[str] = set()
    reachable_bigrams = compute_reachable_bigrams()

    remaining = list(candidates)
    coverage_contributions: list[dict[str, list[Any]]] = []

    for _ in range(min(count, len(remaining))):
        best_candidate: Candidate | None = None
        best_new_bigrams: set[tuple[str, str]] = set()
        best_new_unigrams: set[str] = set()

        for cand in remaining:
            new_bg = set(cand.bigrams) - covered_bigrams
            new_ug = cand.unigrams - covered_unigrams
            if best_candidate is None:
                best_candidate = cand
                best_new_bigrams = new_bg
                best_new_unigrams = new_ug
            else:
                if len(new_bg) > len(best_new_bigrams):
                    best_candidate = cand
                    best_new_bigrams = new_bg
                    best_new_unigrams = new_ug
                elif len(new_bg) == len(best_new_bigrams):
                    if len(new_ug) > len(best_new_unigrams):
                        best_candidate = cand
                        best_new_bigrams = new_bg
                        best_new_unigrams = new_ug
                    elif len(new_ug) == len(best_new_unigrams):
                        if cand.index < best_candidate.index:
                            best_candidate = cand
                            best_new_bigrams = new_bg
                            best_new_unigrams = new_ug

        assert best_candidate is not None
        selected.append(best_candidate)
        remaining.remove(best_candidate)
        covered_bigrams |= best_new_bigrams
        covered_unigrams |= best_new_unigrams

        coverage_contributions.append(
            {
                "new_bigrams": sorted(f"{a}->{b}" for a, b in best_new_bigrams),
                "new_unigrams": sorted(best_new_unigrams),
            }
        )

    all_unigrams_set = set(ALL_OPS)
    missing_unigrams = sorted(all_unigrams_set - covered_unigrams)
    missing_bigrams = sorted(f"{a}->{b}" for a, b in (reachable_bigrams - covered_bigrams))

    coverage_summary = {
        "unigrams": {
            "covered": sorted(covered_unigrams),
            "total": len(ALL_OPS),
            "missing": missing_unigrams,
        },
        "bigrams": {
            "covered": sorted(f"{a}->{b}" for a, b in covered_bigrams),
            "reachable": len(reachable_bigrams),
            "missing": missing_bigrams,
        },
        "contributions": coverage_contributions,
    }

    return selected, coverage_summary


# --- Instruction Rendering --------------------------------------------------
def render_instruction(sequence: list[dict[str, Any]]) -> str:
    """Render a declarative English goal from the operation sequence."""
    steps_clauses: list[str] = []
    i = 0
    n = len(sequence)

    while i < n:
        step = sequence[i]
        op = step["op"]
        args = step["args"]

        # Check for consecutive filter operations
        if op in ("filter_eq", "filter_ge"):
            filter_conditions: list[str] = []
            while i < n and sequence[i]["op"] in ("filter_eq", "filter_ge"):
                cur_op = sequence[i]["op"]
                cur_args = sequence[i]["args"]
                if cur_op == "filter_eq":
                    filter_conditions.append(f"`{cur_args['field']}` is `{cur_args['value']}`")
                elif cur_op == "filter_ge":
                    filter_conditions.append(
                        f"`{cur_args['field']}` is at least {cur_args['value']}"
                    )
                i += 1
            if len(filter_conditions) == 1:
                steps_clauses.append(f"Keep only records where {filter_conditions[0]}.")
            else:
                steps_clauses.append(f"Keep only records where {' and '.join(filter_conditions)}.")
            continue

        # Check for adjacent sort_by + head
        if op == "sort_by" and i + 1 < n and sequence[i + 1]["op"] == "head":
            head_step = sequence[i + 1]
            k = head_step["args"]["n"]
            field = args["field"]
            order = args.get("order", "asc")
            direction = "largest" if order == "desc" else "smallest"
            steps_clauses.append(
                f"Keep the {k} records with the {direction} `{field}` values (stable ties)."
            )
            i += 2
            continue

        if op == "sort_by":
            field = args["field"]
            order = args.get("order", "asc")
            dir_str = "descending" if order == "desc" else "ascending"
            steps_clauses.append(f"Sort records by `{field}` in {dir_str} order (stable sort).")
        elif op == "head":
            k = args["n"]
            steps_clauses.append(f"Keep the first {k} records.")
        elif op == "dedupe_by":
            field = args["field"]
            steps_clauses.append(
                f"Deduplicate records by `{field}`, "
                f"retaining only the first record seen for each distinct value."
            )
        elif op == "select":
            fields = args["fields"]
            fields_str = ", ".join(f"`{f}`" for f in fields)
            steps_clauses.append(f"Project each record to retain only the fields: {fields_str}.")
        elif op == "group_sum":
            g = args["group_field"]
            v = args["value_field"]
            steps_clauses.append(
                f"Group records by `{g}` and compute the sum of `{v}` as `total_{v}`. "
                f"The resulting records must have fields `{g}` and `total_{v}`, "
                f"sorted ascending by `{g}`."
            )
        else:
            steps_clauses.append(f"Apply transformation: {op} with {args}.")

        i += 1

    steps_text = "\n".join(f"{idx + 1}. {clause}" for idx, clause in enumerate(steps_clauses))

    return f"""# Record Processing Pipeline

Transform the newline-delimited JSON records in `/app/data/orders.jsonl` \
into `/app/output/result.jsonl`.

## Required transformations

{steps_text}

## Output requirements

The output file `/app/output/result.jsonl` must contain one JSON object per line \
with keys sorted alphabetically in compact format (`sep=(',', ':')`) with a trailing newline.

- Input path: `/app/data/orders.jsonl`
- Output path: `/app/output/result.jsonl`
- Do not modify files under `/app/data`.
- Do not create any other file under `/app/output`.

The helper utility `/app/bin/rp` is provided in the environment \
(run `/app/bin/rp --help` for details), but any implementation approach is permitted.
"""


# --- Solution Script Generation ---------------------------------------------
def render_solve_sh(sequence: list[dict[str, Any]]) -> str:
    """Generate solution/solve.sh invoking /app/bin/rp subcommands."""
    lines: list[str] = [
        "#!/bin/sh",
        "set -eu",
        "",
        'INPUT="/app/data/orders.jsonl"',
        'OUTPUT="/app/output/result.jsonl"',
        "mkdir -p /app/output",
        "",
    ]

    cur_in = '"$INPUT"'
    for step_idx, step in enumerate(sequence):
        op = step["op"]
        args = step["args"]
        step_out = f"/tmp/step_{step_idx}.jsonl"

        if op == "filter_eq":
            f_val = args["field"]
            v_val = args["value"]
            lines.append(
                f"/app/bin/rp filter-eq --in {cur_in} --out {step_out} "
                f"--field {f_val} --value {v_val}"
            )
        elif op == "filter_ge":
            f_val = args["field"]
            v_val = args["value"]
            lines.append(
                f"/app/bin/rp filter-ge --in {cur_in} --out {step_out} "
                f"--field {f_val} --value {v_val}"
            )
        elif op == "select":
            fields_str = " ".join(args["fields"])
            lines.append(f"/app/bin/rp select --in {cur_in} --out {step_out} --fields {fields_str}")
        elif op == "sort_by":
            f_val = args["field"]
            ord_val = args.get("order", "asc")
            lines.append(
                f"/app/bin/rp sort-by --in {cur_in} --out {step_out} "
                f"--field {f_val} --order {ord_val}"
            )
        elif op == "dedupe_by":
            f_val = args["field"]
            lines.append(f"/app/bin/rp dedupe-by --in {cur_in} --out {step_out} --field {f_val}")
        elif op == "head":
            lines.append(f"/app/bin/rp head --in {cur_in} --out {step_out} -n {args['n']}")
        elif op == "group_sum":
            gf_val = args["group_field"]
            vf_val = args["value_field"]
            lines.append(
                f"/app/bin/rp group-sum --in {cur_in} --out {step_out} "
                f"--group-field {gf_val} --value-field {vf_val}"
            )
        else:
            raise ValueError(f"Unknown op for solve.sh: {op}")

        cur_in = step_out

    lines.append(f'/app/bin/rp write --in {cur_in} --out "$OUTPUT"')
    lines.append("")
    return "\n".join(lines)


def render_fair_alternative(sequence: list[dict[str, Any]]) -> str:
    """Render a solver independent of the bundled record-pipeline executable."""
    sequence_json = json.dumps(sequence, sort_keys=True, separators=(",", ":"))
    script = """#!/bin/sh
set -eu
ROOT="${SEQGEN_APP_ROOT:-/app}"
export SEQGEN_APP_ROOT="$ROOT"
mkdir -p "$ROOT/output"
python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["SEQGEN_APP_ROOT"])
rows = [
    json.loads(line)
    for line in (root / "data/orders.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
sequence = json.loads('__SEQUENCE_JSON__')
for step in sequence:
    op, args = step["op"], step["args"]
    if op == "filter_eq":
        rows = [dict(row) for row in rows if str(row.get(args["field"])) == str(args["value"])]
    elif op == "filter_ge":
        rows = [dict(row) for row in rows if int(row.get(args["field"], 0)) >= int(args["value"])]
    elif op == "select":
        rows = [{key: row[key] for key in args["fields"] if key in row} for row in rows]
    elif op == "sort_by":
        rows = sorted(
            [dict(row) for row in rows],
            key=lambda row: row.get(args["field"]),
            reverse=args.get("order", "asc").lower() == "desc",
        )
    elif op == "dedupe_by":
        seen, kept = set(), []
        for row in rows:
            value = row.get(args["field"])
            if value not in seen:
                seen.add(value)
                kept.append(dict(row))
        rows = kept
    elif op == "head":
        rows = [dict(row) for row in rows[: int(args["n"])]]
    elif op == "group_sum":
        sums = {}
        for row in rows:
            group = str(row.get(args["group_field"]))
            sums[group] = sums.get(group, 0) + int(row.get(args["value_field"], 0))
        total_key = f'total_{args["value_field"]}'
        rows = [
            {args["group_field"]: group, total_key: sums[group]}
            for group in sorted(sums)
        ]
    else:
        raise ValueError(f"unsupported operation: {op}")

output = root / "output/result.jsonl"
output.write_text(
    "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\\n"
        for row in rows
    ),
    encoding="utf-8",
)
PY
"""
    return script.replace("__SEQUENCE_JSON__", sequence_json)


def render_please_hack(expected_bytes: bytes) -> str:
    """Render a golden-output replay that is invalidated by an extra artifact."""
    expected = expected_bytes.decode("utf-8")
    delimiter = "SEQGEN_PLEASE_HACK_EXPECTED"
    if delimiter in expected:
        raise ValueError("please-hack delimiter occurs in expected output")
    return (
        '#!/bin/sh\nset -eu\nROOT="${SEQGEN_APP_ROOT:-/app}"\n'
        'mkdir -p "$ROOT/output"\n'
        f"cat <<'{delimiter}' > \"$ROOT/output/result.jsonl\"\n"
        f"{expected}{delimiter}\n"
        "printf 'reward-seeking replay must be rejected\\n' "
        '> "$ROOT/output/please-hack-proof.txt"\n'
    )


# --- Verifier Script Generation ---------------------------------------------
def render_verify_py(slug: str) -> str:
    """Generate tests/verify.py mirroring event-summary/tests/verify.py."""
    return f'''import json
from pathlib import Path

TRUSTED_INPUT = Path("/tests/fixtures/orders.jsonl")
EXPECTED = Path("/tests/fixtures/expected.jsonl")
AGENT_INPUT = Path("/app/data/orders.jsonl")
AGENT_OUTPUT = Path("/app/output/result.jsonl")
LOG_DIR = Path("/logs/verifier")


def load_jsonl(path: Path) -> tuple[list[dict] | None, str | None]:
    if not path.is_file():
        return None, f"file missing: {{path}}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = []
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                return None, f"line {{idx + 1}} is not a JSON object"
            rows.append(obj)
        return rows, None
    except Exception as exc:
        return None, f"failed to parse JSONL: {{exc}}"


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    expected_rows, _ = load_jsonl(EXPECTED)
    candidate_rows, parse_error = load_jsonl(AGENT_OUTPUT)

    preservation = (
        AGENT_INPUT.is_file() and AGENT_INPUT.read_bytes() == TRUSTED_INPUT.read_bytes()
    )
    parsed = candidate_rows is not None and parse_error is None
    correctness = parsed and candidate_rows == expected_rows
    single_output = (
        AGENT_OUTPUT.parent.is_dir()
        and sorted(p.name for p in AGENT_OUTPUT.parent.iterdir()) == ["result.jsonl"]
    )

    checks = {{
        "input_preservation": {{
            "passed": preservation,
            "message": "input bytes match trusted fixture" if preservation else "input changed",
        }},
        "parse": {{
            "passed": parsed,
            "message": "valid JSONL output" if parsed else (parse_error or "parse failed"),
        }},
        "correctness": {{
            "passed": correctness,
            "message": (
                "output matches expected rows exactly"
                if correctness
                else "wrong output rows"
            ),
        }},
        "output_hygiene": {{
            "passed": single_output,
            "message": (
                "result.jsonl is the only output"
                if single_output
                else "extra/missing output"
            ),
        }},
    }}

    overall = all(check["passed"] for check in checks.values())
    rewards = {{
        "reward": float(overall),
        "correctness": float(correctness),
        "input_preservation": float(preservation),
        "output_hygiene": float(single_output),
    }}

    ctrf_tests = [
        {{
            "name": name,
            "status": "passed" if check["passed"] else "failed",
            "duration": 0,
            "message": check["message"],
        }}
        for name, check in checks.items()
    ]
    ctrf = {{
        "results": {{
            "tool": {{"name": "{slug}-verifier"}},
            "summary": {{
                "tests": len(ctrf_tests),
                "passed": sum(test["status"] == "passed" for test in ctrf_tests),
                "failed": sum(test["status"] == "failed" for test in ctrf_tests),
                "skipped": 0,
                "pending": 0,
                "other": 0,
                "start": 0,
                "stop": 0,
            }},
            "tests": ctrf_tests,
        }}
    }}

    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\\n")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2, sort_keys=True) + "\\n")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, sort_keys=True) + "\\n")
    print(json.dumps({{"passed": overall, "checks": checks}}, sort_keys=True))


if __name__ == "__main__":
    main()
'''


# --- Package Emission -------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def compute_package_manifest_digest(task_dir: Path) -> str:
    """Compute the canonical tree manifest digest over all files except provenance.json.

    Recipe:
    - Collect relative paths for all files under `task_dir` except `provenance.json`.
    - Sort relative paths alphabetically.
    - Compute `sha256:<hex>` for each file's bytes.
    - Format manifest as `[{"digest": "sha256:...", "path": "relative/path"}, ...]`.
    - Compute sha256 of canonical JSON (`sort_keys=True, separators=(',', ':')`).
    """
    manifest_entries: list[dict[str, str]] = []
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(task_dir).as_posix()
        if rel == "provenance.json":
            continue
        manifest_entries.append({"digest": _sha256_file(path), "path": rel})

    manifest_bytes = json.dumps(manifest_entries, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _sha256_bytes(manifest_bytes)


def emit_task_package(
    *,
    out_dir: Path,
    batch_name: str,
    slug: str,
    candidate: Candidate,
    master_seed: int,
    contribution: dict[str, list[Any]],
    now: datetime,
    verifier_network: str = "no-network",
) -> dict[str, Any]:
    """Emit a complete, self-contained Harbor task package.

    ``verifier_network`` selects the separate verifier's network declaration:

    * ``"no-network"`` (default) — emits ``[verifier.environment]
      network_mode = "no-network"``, the isolation the workbench gate requires
      (``task_workbench._validate_network_and_isolation``). Harbor's Docker
      provider enforces it only where egress control is available
      (``docker.py:188-195``): Linux, or a Docker Desktop kernel that passes
      ``_egress_control_kernel_support()``. On this lab's macOS workstation it
      is rejected at trial start (``harbor/environments/base.py:777``).
    * ``"inherit"`` — omits the table, matching ``library/tasks/event-summary``;
      runnable on macOS Docker Desktop but refused by the workbench gate.

    The contradiction is the machine's, not the package's; both spellings are
    recorded in ``generation.json`` so a batch states which contract it serves.
    """
    if verifier_network not in {"no-network", "inherit"}:
        raise ValueError(f"unsupported verifier_network: {verifier_network!r}")
    task_dir = out_dir / slug
    task_dir.mkdir(parents=True, exist_ok=True)

    verifier_environment_block = (
        '\n[verifier.environment]\nnetwork_mode = "no-network"\n'
        if verifier_network == "no-network"
        else ""
    )

    # 1. task.toml
    task_toml_content = f"""schema_version = "1.4"
artifacts = [
    "/app/data/orders.jsonl",
    "/app/output/result.jsonl",
]

[task]
name = "local-lab/{slug}"
version = "1.0.0"
description = "Process structured JSONL orders using deterministic pipeline operations"
keywords = ["jsonl", "tool-sequence", "synthetic", "separate-verifier", "seqgen"]

[[task.authors]]
name = "Peter Makhnatch"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = "unknown"
category = "data-processing"
tags = ["deterministic", "synthetic", "seqgen"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []
{verifier_environment_block}

[agent]
timeout_sec = 120.0

[environment]
# Docker Desktop on macOS cannot enforce Harbor's no-network policy. This local
# control has no network dependency, but uses the supported public baseline.
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""
    (task_dir / "task.toml").write_text(task_toml_content, encoding="utf-8")

    # 2. instruction.md
    instruction_content = render_instruction(candidate.sequence)
    (task_dir / "instruction.md").write_text(instruction_content, encoding="utf-8")

    # 3. environment/
    env_dir = task_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_env = f"""\
FROM {BASE_IMAGE}

WORKDIR /app

COPY orders.jsonl /app/data/orders.jsonl
COPY rp /app/bin/rp

RUN chmod +x /app/bin/rp && mkdir -p /app/output
"""
    (env_dir / "Dockerfile").write_text(dockerfile_env, encoding="utf-8")

    # Format orders.jsonl canonical bytes
    orders_bytes = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in candidate.dataset
    ).encode("utf-8")
    (env_dir / "orders.jsonl").write_bytes(orders_bytes)

    rp_path = env_dir / "rp"
    rp_path.write_text(RP_SOURCE, encoding="utf-8")
    rp_path.chmod(rp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 4. solution/
    sol_dir = task_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    solve_sh_content = render_solve_sh(candidate.sequence)
    solve_sh_path = sol_dir / "solve.sh"
    solve_sh_path.write_text(solve_sh_content, encoding="utf-8")
    solve_sh_path.chmod(solve_sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 5. tests/
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_tests = f"""\
FROM {BASE_IMAGE}

COPY . /tests

RUN mkdir -p /app/data /app/output /logs/verifier \\
    && chmod +x /tests/test.sh

WORKDIR /app
"""
    (tests_dir / "Dockerfile").write_text(dockerfile_tests, encoding="utf-8")
    test_sh_content = """#!/bin/sh
set -eu
exec python /tests/verify.py
"""
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_sh_content, encoding="utf-8")
    test_sh_path.chmod(test_sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    verify_py_content = render_verify_py(slug)
    (tests_dir / "verify.py").write_text(verify_py_content, encoding="utf-8")

    fixtures_dir = tests_dir / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / "orders.jsonl").write_bytes(orders_bytes)

    expected_bytes = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in candidate.output_rows
    ).encode("utf-8")
    (fixtures_dir / "expected.jsonl").write_bytes(expected_bytes)

    # M049's fixed control set requires a genuinely independent valid solver
    # and a retained reward-hack replay in addition to the invalid probes.
    workbench_dir = task_dir / "workbench"
    workbench_dir.mkdir(parents=True, exist_ok=True)
    fair_sh = workbench_dir / "fair-alternative.sh"
    fair_sh.write_text(render_fair_alternative(candidate.sequence), encoding="utf-8")
    fair_sh.chmod(fair_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    please_hack_sh = workbench_dir / "please-hack.sh"
    please_hack_sh.write_text(render_please_hack(expected_bytes), encoding="utf-8")
    please_hack_sh.chmod(please_hack_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 6. workbench/adversarial/
    adv_dir = task_dir / "workbench/adversarial"
    adv_dir.mkdir(parents=True, exist_ok=True)

    empty_sh = adv_dir / "empty-output.sh"
    empty_sh.write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /app/output\n: > /app/output/result.jsonl\n",
        encoding="utf-8",
    )
    empty_sh.chmod(empty_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    copy_sh = adv_dir / "copy-input.sh"
    copy_sh.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "mkdir -p /app/output\n"
        "cp /app/data/orders.jsonl /app/output/result.jsonl\n",
        encoding="utf-8",
    )
    copy_sh.chmod(copy_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Plausible wrong payload
    if len(candidate.output_rows) >= 2:
        plausible_wrong_rows = list(reversed(candidate.output_rows))[1:]
    else:
        plausible_wrong_rows = [{"fabricated_id": 99999, "status": "synthetic_error"}]

    # Assert plausible wrong rows differ from expected
    assert plausible_wrong_rows != candidate.output_rows, (
        "Plausible wrong payload must not match expected rows"
    )

    plausible_wrong_bytes = "".join(
        json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in plausible_wrong_rows
    )

    plausible_sh = adv_dir / "plausible-wrong.sh"
    plausible_sh.write_text(
        f"""#!/bin/sh
set -eu
mkdir -p /app/output
cat << 'EOF' > /app/output/result.jsonl
{plausible_wrong_bytes}EOF
""",
        encoding="utf-8",
    )
    plausible_sh.chmod(plausible_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 7. generation.json
    sequence_bytes = json.dumps(candidate.sequence, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    seq_digest = _sha256_bytes(sequence_bytes)

    generator_code_digest = _sha256_file(Path(__file__))
    validator_code_digest = _sha256_file(tests_dir / "verify.py")
    input_digest = _sha256_file(env_dir / "orders.jsonl")
    output_digest = _sha256_file(fixtures_dir / "expected.jsonl")
    instruction_digest = _sha256_file(task_dir / "instruction.md")
    generation_record = {
        "schema_version": 1,
        "generator": TRANSFORM_ID,
        "generator_identity": {
            "code_digest": generator_code_digest,
            "model_id": None,
            "prompt_digest": None,
            "transform": TRANSFORM_ID,
        },
        "validator_identity": {
            "code_digest": validator_code_digest,
            "model_id": None,
            "prompt_digest": None,
            "runtime": "tests/verify.py",
        },
        "batch": batch_name,
        "slug": slug,
        "seed": candidate.seed,
        "master_seed": master_seed,
        "sequence": candidate.sequence,
        "instruction_style": "declarative",
        "verifier_network": verifier_network,
        "certification": {
            "state": "uncertified",
            "workbench_version": "m049-v1",
            "evidence_packet": None,
            "admission_state": "unadmitted",
        },
        "digests": {
            "input_jsonl": input_digest,
            "instruction_md": instruction_digest,
            "orders_jsonl": input_digest,
            "output_jsonl": output_digest,
            "expected_jsonl": output_digest,
            "rp": _sha256_file(rp_path),
            "sequence": seq_digest,
            "task_toml": _sha256_file(task_dir / "task.toml"),
            "validator": validator_code_digest,
        },
        "lineage": {
            "master_seed": master_seed,
            "candidate_seed": candidate.seed,
            "sequence_digest": seq_digest,
            "input_digest": input_digest,
            "output_digest": output_digest,
        },
        "coverage_contribution": contribution,
        "row_counts": {
            "input": len(candidate.dataset),
            "output": len(candidate.output_rows),
        },
    }
    (task_dir / "generation.json").write_text(
        json.dumps(generation_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 8. provenance.json
    material_digest = compute_package_manifest_digest(task_dir)

    rp_digest = _sha256_bytes(RP_SOURCE.encode("utf-8"))
    domain_spec_bytes = json.dumps(DOMAIN_SPEC, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    domain_spec_digest = _sha256_bytes(domain_spec_bytes)

    prov = ProvenanceMetadata(
        item_id=slug,
        zone="03-synthetic",
        source_uri=f"library/synthetic/{batch_name}/{slug}",
        revision=TRANSFORM_ID,
        material_digest=material_digest,
        license="NOASSERTION",
        created_at=now,
        created_by="evallab.seqgen",
        transform=TRANSFORM_ID,
        parent_digests=[
            generator_code_digest,
            validator_code_digest,
            rp_digest,
            domain_spec_digest,
            input_digest,
            output_digest,
        ],
        notes=(
            "Independent record-pipeline reimplementation from the paper-level "
            "description; no upstream code, prompt, output, or artifact was reused. "
            "A pinned restricted source snapshot was inspected for dependency and "
            "license assessment, so no implementation firewall is claimed. No model "
            "or prompt was used. License is not asserted pending repository policy."
        ),
    )
    (task_dir / "provenance.json").write_text(
        prov.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    return {
        "slug": slug,
        "sequence_digest": seq_digest,
        "package_digest": material_digest,
    }


# --- Batch Generation API & CLI ---------------------------------------------
def generate_batch(
    *,
    seed: int,
    count: int,
    pool: int,
    out_dir: Path,
    now: datetime,
    verifier_network: str = "no-network",
) -> dict[str, Any]:
    """Generate a deterministic batch of synthetic Harbor tasks."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(
            f"Output directory exists and is not empty: {out_dir}. "
            "Batch generation requires an empty or non-existent destination directory."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    batch_name = out_dir.name

    candidates = generate_candidate_pool(seed, pool)
    selected, coverage_summary = select_candidates_by_coverage(candidates, count)

    task_summaries: list[dict[str, Any]] = []
    for idx, (cand, contrib) in enumerate(
        zip(selected, coverage_summary["contributions"], strict=True)
    ):
        slug = f"seqgen-s{seed}-{idx:03d}"
        task_summary = emit_task_package(
            out_dir=out_dir,
            batch_name=batch_name,
            slug=slug,
            candidate=cand,
            master_seed=seed,
            contribution=contrib,
            now=now,
            verifier_network=verifier_network,
        )
        task_summaries.append(task_summary)

    batch_manifest = {
        "schema_version": 1,
        "generator": TRANSFORM_ID,
        "seed": seed,
        "pool": pool,
        "count": count,
        "verifier_network": verifier_network,
        "certification": {
            "state": "uncertified",
            "workbench_version": "m049-v1",
            "evidence_packets": [],
            "admission_state": "unadmitted",
        },
        "generator_identity": {
            "code_digest": _sha256_file(Path(__file__)),
            "model_id": None,
            "prompt_digest": None,
            "transform": TRANSFORM_ID,
        },
        "created_at": now.isoformat(),
        "tasks": task_summaries,
        "coverage": {
            "unigrams": coverage_summary["unigrams"],
            "bigrams": coverage_summary["bigrams"],
        },
        "notes": f"Synthetic Harbor-task batch generated by SEQGEN v{SEQGEN_VERSION}",
    }

    batch_json_path = out_dir / "BATCH.json"
    batch_json_path.write_text(
        json.dumps(batch_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return batch_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="SEQGEN v0 Synthetic Task Generator")
    parser.add_argument("--seed", type=int, default=7, help="Master RNG seed")
    parser.add_argument("--count", type=int, default=3, help="Number of tasks to select and emit")
    parser.add_argument("--pool", type=int, default=40, help="Candidate sequence pool size")
    parser.add_argument("--out", type=str, required=True, help="Output batch directory")
    parser.add_argument(
        "--verifier-network",
        choices=("no-network", "inherit"),
        default="no-network",
        help=(
            "Separate-verifier network declaration: 'no-network' satisfies the "
            "workbench isolation gate (default); 'inherit' omits the table so the "
            "package can execute on Docker hosts without egress-control support "
            "(for example this lab's macOS workstation)."
        ),
    )

    args = parser.parse_args()
    now = datetime.now(UTC)
    out_dir = Path(args.out)

    batch = generate_batch(
        seed=args.seed,
        count=args.count,
        pool=args.pool,
        out_dir=out_dir,
        now=now,
        verifier_network=args.verifier_network,
    )

    unigrams = batch["coverage"]["unigrams"]
    bigrams = batch["coverage"]["bigrams"]
    print(f"SEQGEN: Generated batch '{out_dir.name}' with {len(batch['tasks'])} tasks in {out_dir}")
    print(
        f"Coverage: unigrams {len(unigrams['covered'])}/{unigrams['total']}, "
        f"bigrams {len(bigrams['covered'])}/{bigrams['reachable']} reachable"
    )


if __name__ == "__main__":
    main()
