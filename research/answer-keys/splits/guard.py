"""Split guard for every `qa.answer_key/v1` key under research/answer-keys/.

Publishes the dev/reported split of every item (`assignments.csv`) and a summary
(`report.md`), and checks the rule "never tune on the split you report":

1. Within a key, items that share a task or a pair form one unit; a unit must not
   straddle splits.
2. Across keys, the same task (bare name, pin and benchmark prefix dropped) may be
   `dev` in one key and reported (`test`/`heldout`) in another; those are listed.
3. `exposure.jsonl` records which systems were tuned on (or read outputs of) which
   items. A scoreboard row on a reported split whose agent/system was tuned on items
   inside that split is a violation unless the row leaves them unmeasured
   (n_measured <= n_items - exposed) or the exposure acknowledges the row.

    uv run python research/answer-keys/splits/guard.py           # rewrite outputs
    uv run python research/answer-keys/splits/guard.py --check   # CI: fail on drift,
                                                                 # straddles, violations
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = HERE.parent
REPO = KEYS.parents[1]
SCOREBOARD = REPO / "research/qa-scoreboard/scoreboard.csv"
EXPOSURE = HERE / "exposure.jsonl"
ASSIGNMENTS = HERE / "assignments.csv"
REPORT = HERE / "report.md"
REPORTED = {"test", "heldout"}
SPLITS = {"dev", *REPORTED}


def _jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def task_name(task_id: str) -> str:
    """Bare task name shared across keys: drop `@pin` and any benchmark prefix."""
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def load_keys() -> dict[str, dict]:
    keys = {}
    for items_path in sorted(KEYS.glob("*/items.jsonl")):
        key_id = items_path.parent.name
        items = _jsonl(items_path)
        pairs_path = items_path.parent / "pairs.jsonl"
        pairs = _jsonl(pairs_path) if pairs_path.exists() else []
        keys[key_id] = {"items": items, "pairs": pairs}
    return keys


def units(items: list[dict], pairs: list[dict]) -> dict[str, str]:
    """item_id -> unit id: items linked by subject.task_id or by a pair share a unit."""
    parent = {i["item_id"]: i["item_id"] for i in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    by_task: dict[str, str] = {}
    by_pair: dict[str, str] = {}
    for item in items:
        first = by_task.setdefault(item["subject"]["task_id"], item["item_id"])
        union(item["item_id"], first)
        for pair_id in item.get("pair_ids") or []:
            union(item["item_id"], by_pair.setdefault(pair_id, item["item_id"]))
    for pair in pairs:
        if pair.get("parent") in parent and pair.get("child") in parent:
            union(pair["parent"], pair["child"])
    roots = {i["item_id"]: find(i["item_id"]) for i in items}
    # Name each unit by its smallest member task id, so the label is stable.
    label: dict[str, str] = {}
    for item in sorted(items, key=lambda i: i["subject"]["task_id"]):
        label.setdefault(roots[item["item_id"]], item["subject"]["task_id"])
    return {item_id: label[root] for item_id, root in roots.items()}


def exposed_items(record: dict, keys: dict[str, dict]) -> dict[str, set[str]]:
    """key_id -> item_ids an exposure record touches."""
    out: dict[str, set[str]] = defaultdict(set)
    tasks = set(record.get("tasks") or [])
    for key_id, key in keys.items():
        for item in key["items"]:
            if task_name(item["subject"]["task_id"]) in tasks:
                out[key_id].add(item["item_id"])
    for key_id, ids in (record.get("items") or {}).items():
        out[key_id].update(ids)
    for key_id, rel in (record.get("items_file") or {}).items():
        wanted = {line.strip() for line in (REPO / rel).read_text().splitlines() if line.strip()}
        for item in keys[key_id]["items"]:
            if item["item_id"] in wanted or item["subject"].get("trial_id") in wanted:
                out[key_id].add(item["item_id"])
    for key_id, where in (record.get("where") or {}).items():
        for item in keys[key_id]["items"]:
            if all(_matches(item.get(field), value) for field, value in where.items()):
                out[key_id].add(item["item_id"])
    return out


def _matches(actual: object, wanted: object) -> bool:
    """Equality, or membership when the item field is a list (e.g. `selected_by`)."""
    return wanted in actual if isinstance(actual, list) else actual == wanted


def validate_exposures(exposures: list[dict], keys: dict[str, dict]) -> list[str]:
    errors = []
    known = {k: {i["item_id"] for i in v["items"]} for k, v in keys.items()}
    names = {task_name(i["subject"]["task_id"]) for v in keys.values() for i in v["items"]}
    for record in exposures:
        rid = record["exposure_id"]
        if record.get("kind") not in {"tuned", "selected", "inspected"}:
            errors.append(f"{rid}: kind must be tuned|selected|inspected")
        for field in ("items", "items_file", "where"):
            for key_id in record.get(field) or {}:
                if key_id not in keys:
                    errors.append(f"{rid}: {field} names unknown key {key_id!r}")
        for key_id, ids in (record.get("items") or {}).items():
            missing = sorted(set(ids) - known.get(key_id, set()))
            if missing:
                errors.append(f"{rid}: unknown items in {key_id}: {missing[:3]}")
        for key_id, rel in (record.get("items_file") or {}).items():
            if not (REPO / rel).is_file():
                errors.append(f"{rid}: items_file {rel} missing")
            elif key_id in keys:
                wanted = {
                    line.strip() for line in (REPO / rel).read_text().splitlines() if line.strip()
                }
                found = {
                    x
                    for i in keys[key_id]["items"]
                    for x in (i["item_id"], i["subject"].get("trial_id"))
                    if x in wanted
                }
                if wanted - found:
                    errors.append(f"{rid}: {len(wanted - found)} ids in {rel} not in {key_id}")
        unused = [t for t in record.get("tasks") or [] if t not in names]
        record["_tasks_outside_keys"] = unused
    return errors


def build(keys: dict[str, dict], exposures: list[dict]) -> tuple[str, str, list[str]]:
    """Return (assignments.csv text, report.md text, straddle errors)."""
    touched: dict[tuple[str, str], list[str]] = defaultdict(list)
    per_exposure: dict[str, dict[str, set[str]]] = {}
    for record in exposures:
        hits = exposed_items(record, keys)
        per_exposure[record["exposure_id"]] = hits
        for key_id, ids in hits.items():
            for item_id in ids:
                touched[(key_id, item_id)].append(record["exposure_id"])

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["key_id", "item_id", "task", "unit", "split", "expected_verdict", "exposures"])
    errors: list[str] = []
    lines = [
        "# Answer-key splits (generated by guard.py; do not edit)",
        "",
        "`dev` may be used for tuning; `test`/`heldout` are reported splits. Item-level",
        "assignments are in `assignments.csv`; exposures come from `exposure.jsonl`.",
        "",
        "## Per key",
        "",
        "| key | items dev (fail/pass) | items reported (fail/pass) | pairs dev / reported | units | straddling units |",
        "|---|---|---|---|---|---|",
    ]
    for key_id, key in keys.items():
        items = key["items"]
        unit_of = units(items, key["pairs"])
        for item in sorted(items, key=lambda i: i["item_id"]):
            if item["split"] not in SPLITS:
                errors.append(
                    f"{key_id}:{item['item_id']}: split {item['split']!r} not in {sorted(SPLITS)}"
                )
            writer.writerow(
                [
                    key_id,
                    item["item_id"],
                    task_name(item["subject"]["task_id"]),
                    unit_of[item["item_id"]],
                    item["split"],
                    item["expected_verdict"],
                    ";".join(sorted(touched.get((key_id, item["item_id"]), []))),
                ]
            )
        unit_splits: dict[str, set[str]] = defaultdict(set)
        for item in items:
            unit_splits[unit_of[item["item_id"]]].add(item["split"])
        straddle = sorted(u for u, s in unit_splits.items() if len(s) > 1)
        errors += [f"{key_id}: unit {u} straddles {sorted(unit_splits[u])}" for u in straddle]

        def count(split_set: set[str], items: list[dict] = items) -> str:
            c = Counter(i["expected_verdict"] for i in items if i["split"] in split_set)
            return f"{sum(c.values())} ({c['fail']}/{c['pass']})"

        by_item = {i["item_id"]: i["split"] for i in items}
        pair_splits = Counter(by_item.get(p.get("child"), "?") in REPORTED for p in key["pairs"])
        reported_names = sorted({i["split"] for i in items if i["split"] in REPORTED}) or ["-"]
        lines.append(
            f"| {key_id} | {count({'dev'})} | {count(REPORTED)} {'/'.join(reported_names)} | "
            f"{pair_splits[False]} / {pair_splits[True]} | {len(unit_splits)} | {len(straddle)} |"
        )

    lines += [
        "",
        "## Same task name in dev and in a reported split",
        "",
        "A task name that is `dev` somewhere and reported somewhere else (another key, or",
        "another unit of the same key). Tuning on the dev side touches the reported side",
        "unless the systems differ. Names are compared without pin or benchmark prefix.",
        "",
        "| task | dev in | reported in |",
        "|---|---|---|",
    ]
    where: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for key_id, key in keys.items():
        for item in key["items"]:
            where[task_name(item["subject"]["task_id"])][key_id].add(item["split"])
    for task in sorted(where):
        dev_in = sorted(k for k, s in where[task].items() if "dev" in s)
        rep_in = sorted(k for k, s in where[task].items() if s & REPORTED)
        if dev_in and rep_in:
            lines.append(f"| {task} | {', '.join(dev_in)} | {', '.join(rep_in)} |")

    lines += [
        "",
        "## Same package bytes, different verdicts",
        "",
        "Task items whose `subject.task_digest` appears in more than one key with different",
        "`expected_verdict`. One label is newer evidence against the other.",
        "",
        "| task | digest | verdicts |",
        "|---|---|---|",
    ]
    by_digest: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    names: dict[str, str] = {}
    for key_id, key in keys.items():
        for item in key["items"]:
            digest = item["subject"].get("task_digest")
            if item["subject"]["kind"] == "task" and digest:
                by_digest[digest][item["expected_verdict"]].add(key_id)
                names[digest] = task_name(item["subject"]["task_id"])
    for digest in sorted(by_digest, key=lambda d: names[d]):
        verdicts = by_digest[digest]
        if len(verdicts) > 1:
            text = "; ".join(f"{v}: {', '.join(sorted(k))}" for v, k in sorted(verdicts.items()))
            lines.append(f"| {names[digest]} | {digest[:19]} | {text} |")

    lines += [
        "",
        "## Exposures (who was tuned on or read what)",
        "",
        "| exposure | kind | agents | key | dev items | reported items |",
        "|---|---|---|---|---|---|",
    ]
    for record in exposures:
        hits = per_exposure[record["exposure_id"]]
        agents = ", ".join(
            a["agent"]
            + (f" ({a['system_prefix']}*)" if a.get("system_prefix") else "")
            + (f" from {a['from']}" if a.get("from") else "")
            for a in record["applies_to"]
        )
        if not hits:
            lines.append(
                f"| {record['exposure_id']} | {record['kind']} | {agents} | (no key items) | 0 | 0 |"
            )
        for key_id in sorted(hits):
            splits = Counter(
                ("rep" if i["split"] in REPORTED else "dev")
                for i in keys[key_id]["items"]
                if i["item_id"] in hits[key_id]
            )
            lines.append(
                f"| {record['exposure_id']} | {record['kind']} | {agents} | {key_id} | {splits['dev']} | {splits['rep']} |"
            )
        outside = record.get("_tasks_outside_keys") or []
        if outside:
            lines.append(f"| {record['exposure_id']} | | | tasks in no key | {len(outside)} | |")
    return buf.getvalue(), "\n".join(lines) + "\n", errors


def _applies(record: dict, row: dict) -> bool:
    return any(
        a["agent"] == row["agent"]
        and row["system"].startswith(a.get("system_prefix", ""))
        and row["date_utc"][:10] >= a.get("from", "")
        for a in record["applies_to"]
    )


def scoreboard_rows() -> list[dict]:
    if not SCOREBOARD.exists():
        return []
    with SCOREBOARD.open(newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("category", "all") == "all"]


def audit_scoreboard(
    keys: dict[str, dict], exposures: list[dict], rows: list[dict]
) -> tuple[list[str], list[str]]:
    """Return (violations, warnings) for scoreboard rows on reported splits.

    The CSV cannot show which items a row measured, so a row on a reported split that
    contains exposed items is a violation unless an `acknowledged_rows` entry says how
    it was handled. `how: excluded` is checked for feasibility: the row must leave at
    least as many items unmeasured as there are exposed items in it.
    """
    violations, warnings = [], []
    split_of = {k: {i["item_id"]: i["split"] for i in v["items"]} for k, v in keys.items()}
    for record in exposures:
        hits = exposed_items(record, keys)
        for row in rows:
            key_id, split = row["key_id"], row["split"]
            if split not in REPORTED or key_id not in hits or not _applies(record, row):
                continue
            inside = [i for i in hits[key_id] if split_of[key_id].get(i) == split]
            if not inside:
                continue
            where = f"{key_id}/{split} {row['system'][:60]!r} ({record['exposure_id']}: {len(inside)} items)"
            ack = next(
                (
                    a
                    for a in record.get("acknowledged_rows") or []
                    if a["key_id"] == key_id
                    and row["system"].startswith(a.get("system_prefix", ""))
                ),
                None,
            )
            if ack and ack["how"] == "excluded":
                if int(row["n_measured"]) > int(row["n_items"]) - len(inside):
                    violations.append(
                        f"claimed exclusion impossible (n_measured {row['n_measured']}): {where}"
                    )
            elif ack:
                warnings.append(f"disclosed: {where}")
            elif record["kind"] == "inspected":
                warnings.append(f"outputs were read before this row: {where}")
            else:
                violations.append(
                    f"{record['kind']} items on a reported split: {where}. Exclude them, or add an "
                    f"acknowledged_rows entry to {EXPOSURE.relative_to(REPO)} saying how they were handled"
                )
    return violations, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args()
    keys = load_keys()
    exposures = _jsonl(EXPOSURE)
    errors = validate_exposures(exposures, keys)
    assignments, report, straddles = build(keys, exposures)
    errors += straddles
    violations, warnings = audit_scoreboard(keys, exposures, scoreboard_rows())
    for line in warnings:
        print(f"warning: {line}")
    if args.check:
        for path, text in ((ASSIGNMENTS, assignments), (REPORT, report)):
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                errors.append(
                    f"{path.relative_to(REPO)} is stale: run `uv run python {Path(__file__).relative_to(REPO)}`"
                )
    else:
        ASSIGNMENTS.write_text(assignments, encoding="utf-8")
        REPORT.write_text(report, encoding="utf-8")
    errors += violations
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    print(
        f"{len(keys)} keys, {assignments.count(chr(10)) - 1} items, {len(exposures)} exposures, {len(errors)} errors"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
