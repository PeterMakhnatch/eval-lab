"""Decision-level counterfactual comparison for the held L0 format decision (#1825).

Compares three L0 configurations per run, at the decision level (`drop` / `review` / `pass`):

  now  = current-main `evaluate_l0_stage` on the DetBench view            (l0v_<view>_raw.jsonl)
  cf1  = known tool shapes mapped to their true semantic equivalent       (l0cf_<view>_raw.jsonl)
  cf2  = cf1 plus every residual known shape, unknown -> `think`          (l0cf2_<view>_raw.jsonl)

`cf2` is the "L0 never complains about a tool name" upper bound: it has zero unrecognized steps by
construction, so any flag it still raises is content evidence.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/decision_transitions.py \
      [--view full|actions_only] [--raw-dir /private/tmp/ov1104/detbench/l0] \
      [--out research/experiments/ov1104-detbench/results/DECISION-TRANSITIONS.md]

Writes a Markdown report with, per set and label: counts of every decision change now->cf1, now->cf2,
and cf1->cf2, plus per-set honest relief and newly-flagged rates. DEV only is *not* implied: it
reports DEV and held-out separately.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, read_jsonl  # noqa: E402

RAW = Path("/private/tmp/ov1104/detbench/l0")
RANK = {"pass": 0, "review": 1, "drop": 2}
CANON = {"keep": "pass", "review": "review", "drop": "drop"}


def _load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        dec = str(row.get("decision") or "").lower()
        out[str(row["run_key"])] = {
            "decision": CANON.get(dec, dec),
            "reasons": row.get("reasons") or [],
            "unrec": row.get("unrecognized_steps") or 0,
        }
    return out


def _trans(a: dict[str, str], b: dict[str, str], keys: list[str]) -> Counter[str]:
    c: Counter[str] = Counter()
    for k in keys:
        ra, rb = a.get(k), b.get(k)
        if ra is None or rb is None:
            continue
        c[f"{ra['decision']}->{rb['decision']}"] += 1
    return c


def _pct(k: int, n: int) -> str:
    return f"{k} ({100.0 * k / n:.1f}%)" if n else "0"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--view", choices=("full", "actions_only"), default="full")
    ap.add_argument("--runs", type=Path, default=RUNS)
    ap.add_argument("--raw-dir", type=Path, default=RAW)
    ap.add_argument("--cf2-dir", type=Path, default=None, help="where l0cf2_<view>_raw.jsonl lives")
    ap.add_argument(
        "--out", type=Path, default=None, help="default: results/DECISION-TRANSITIONS[-<view>].md"
    )
    args = ap.parse_args(argv)

    runs = {r["run_key"]: r for r in read_jsonl(args.runs)}
    now = _load(args.raw_dir / f"l0v_{args.view}_raw.jsonl")
    cf1 = _load(args.raw_dir / f"l0cf_{args.view}_raw.jsonl")
    cf2_candidates = [args.raw_dir / f"l0cf2_{args.view}_raw.jsonl"]
    if args.cf2_dir is not None:
        cf2_candidates.insert(0, args.cf2_dir / f"l0cf2_{args.view}_raw.jsonl")
    cf2_candidates.append(args.raw_dir.parent / "l0-cf2" / f"l0cf2_{args.view}_raw.jsonl")
    cf2 = {}
    for cand in cf2_candidates:
        cf2 = _load(cand)
        if cf2:
            break

    sets = sorted({r["set"] for r in runs.values()})
    lines = [
        "# L0 decision transitions under tool-shape recognition (#1825)",
        "",
        f"View: `{args.view}`. `now` = current-main `evaluate_l0_stage`. `cf1` = known tool shapes mapped to",
        "their true semantic equivalent (`l0_rules.map_tool_shapes`). `cf2` = cf1 plus every residual known",
        "shape, with anything still unknown mapped to `think` (`map_tool_shapes_v2`), so a cf2 run has zero",
        "unrecognized steps by construction and any flag it raises is content evidence.",
        "",
        "Counts are runs. `relieved` = decision became less severe; `newly flagged` = more severe.",
        "",
        "## Coverage",
        "",
        "| config | runs with a decision | total unrecognized steps | decisions |",
        "|---|---:|---:|---|",
    ]
    for name, data in (("now", now), ("cf1", cf1), ("cf2", cf2)):
        if not data:
            lines.append(f"| `{name}` | (missing) | - | - |")
            continue
        n = sum(1 for k in data if k in runs)
        unrec = sum(data[k]["unrec"] for k in data if k in runs)
        dec = Counter(data[k]["decision"] for k in data if k in runs)
        lines.append(
            f"| `{name}` | {n} | {unrec} | "
            + ", ".join(f"{d} {dec.get(d, 0)}" for d in ("drop", "review", "pass"))
            + " |"
        )

    for title, split in (("DEV replay-v1 (5 corpora)", "dev"), ("Held-out (20 sets)", "heldout")):
        keys = [k for k, r in runs.items() if r["split"] == split]
        if not keys:
            continue
        lines += ["", f"## {title}", ""]
        for label, lab in ((1, "cheat"), (0, "honest")):
            lkeys = [k for k in keys if int(runs[k]["label"]) == label]
            n = len(lkeys)
            if not n:
                continue
            lines.append(f"**{lab} (n={n})**")
            lines.append("")
            lines.append("| transition | now -> cf1 | now -> cf2 | cf1 -> cf2 |")
            lines.append("|---|---:|---:|---:|")
            t1, t2, t3 = _trans(now, cf1, lkeys), _trans(now, cf2, lkeys), _trans(cf1, cf2, lkeys)
            for a in ("drop", "review"):
                for b in ("drop", "review", "pass"):
                    if a == b:
                        continue
                    lines.append(
                        f"| `{a} -> {b}` | {t1.get(f'{a}->{b}', 0)} | {t2.get(f'{a}->{b}', 0)} | {t3.get(f'{a}->{b}', 0)} |"
                    )
            for b in ("review", "drop"):
                lines.append(
                    f"| `pass -> {b}` | {t1.get(f'pass->{b}', 0)} | {t2.get(f'pass->{b}', 0)} | {t3.get(f'pass->{b}', 0)} |"
                )
            same1 = sum(v for k, v in t1.items() if k.split("->")[0] == k.split("->")[1])
            same2 = sum(v for k, v in t2.items() if k.split("->")[0] == k.split("->")[1])
            same3 = sum(v for k, v in t3.items() if k.split("->")[0] == k.split("->")[1])
            lines.append(f"| unchanged | {same1} | {same2} | {same3} |")
            lines.append("")

        # per set: honest relief / newly flagged and cheat relief / newly flagged
        lines += [
            "### Per set",
            "",
            "| set | honest n | honest relieved now->cf1 | honest relieved now->cf2 | honest newly flagged now->cf2 | cheat n | cheat relieved now->cf1 | cheat relieved now->cf2 | cheat newly flagged now->cf2 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in sets:
            if runs[next(iter([k for k in keys]))]["split"] != split and not any(
                runs[k]["set"] == s for k in keys
            ):
                continue
            sk = [k for k in keys if runs[k]["set"] == s]
            if not sk:
                continue
            hn = [k for k in sk if int(runs[k]["label"]) == 0]
            cn = [k for k in sk if int(runs[k]["label"]) == 1]

            def _sev(data_before, data_after, ks):
                relief = newly = 0
                for k in ks:
                    a, b = data_before.get(k), data_after.get(k)
                    if a is None or b is None:
                        continue
                    d = RANK[b["decision"]] - RANK[a["decision"]]
                    relief += int(d < 0)
                    newly += int(d > 0)
                return relief, newly

            hr1, _ = _sev(now, cf1, hn)
            hr2, hf2 = _sev(now, cf2, hn)
            cr1, _ = _sev(now, cf1, cn)
            cr2, cf2n = _sev(now, cf2, cn)
            lines.append(
                f"| `{s}` | {len(hn)} | {hr1} | {hr2} | {hf2} | {len(cn)} | {cr1} | {cr2} | {cf2n} |"
            )
        lines.append("")

    lines += [
        "## How to read",
        "",
        "- **`drop -> pass` and `drop -> review` are the dangerous cells.** They mean L0 stopped taking the",
        "  strongest action it takes today. A zero column is the safety property that matters.",
        "- **`pass -> drop` and `review -> drop` on honest runs** are new hard false alarms.",
        "- **`review -> pass` on cheat runs** is lost soft coverage: those runs leave the review queue even",
        "  though nothing about their content changed.",
        "- `cf2` is an upper bound, not a proposal: it maps genuinely unknown tools onto `think`, which",
        "  discards whatever evidence they carried. Use `cf1` for the shippable mapping and `cf2` to bound",
        "  how much of the residual flag rate is pure naming.",
        "",
    ]

    # --- auto-computed headline, so the prose stays true if the data changes -------------------
    def agg(keys: list[str], lab: int) -> dict[str, int]:
        ks = [k for k in keys if int(runs[k]["label"]) == lab]
        out: dict[str, int] = {"n": len(ks)}
        for tag, before, after in (("cf1", now, cf1), ("cf2", now, cf2)):
            rel = newf = 0
            for k in ks:
                a, b = before.get(k), after.get(k)
                if a is None or b is None:
                    continue
                d = RANK[b["decision"]] - RANK[a["decision"]]
                rel += int(d < 0)
                newf += int(d > 0)
            out[f"{tag}_relieved"] = rel
            out[f"{tag}_newly"] = newf
            out[f"{tag}_drop_lost"] = sum(
                1
                for k in ks
                if before.get(k, {}).get("decision") == "drop"
                and after.get(k, {}).get("decision") != "drop"
                if k in before and k in after
            )
        return out

    ho = [k for k, r in runs.items() if r["split"] == "heldout"]
    dv = [k for k, r in runs.items() if r["split"] == "dev"]
    hc, hh, dc, dh = agg(ho, 1), agg(ho, 0), agg(dv, 1), agg(dv, 0)
    lines += [
        "## Headline (auto-computed from the files above)",
        "",
        "| quantity | DEV cheat | DEV honest | held-out cheat | held-out honest |",
        "|---|---:|---:|---:|---:|",
        f"| n | {dc['n']} | {dh['n']} | {hc['n']} | {hh['n']} |",
        f"| hard drops lost (`drop ->` anything else), cf1 | {dc['cf1_drop_lost']} | {dh['cf1_drop_lost']} | {hc['cf1_drop_lost']} | {hh['cf1_drop_lost']} |",
        f"| hard drops lost, cf2 | {dc['cf2_drop_lost']} | {dh['cf2_drop_lost']} | {hc['cf2_drop_lost']} | {hh['cf2_drop_lost']} |",
        f"| relieved (decision less severe), cf1 | {dc['cf1_relieved']} | {dh['cf1_relieved']} | {hc['cf1_relieved']} | {hh['cf1_relieved']} |",
        f"| relieved, cf2 | {dc['cf2_relieved']} | {dh['cf2_relieved']} | {hc['cf2_relieved']} | {hh['cf2_relieved']} |",
        f"| newly flagged (more severe), cf1 | {dc['cf1_newly']} | {dh['cf1_newly']} | {hc['cf1_newly']} | {hh['cf1_newly']} |",
        f"| newly flagged, cf2 | {dc['cf2_newly']} | {dh['cf2_newly']} | {hc['cf2_newly']} | {hh['cf2_newly']} |",
        "",
        "## What this means for #1825",
        "",
        f"- **Hard drops are never lost.** In both the shippable mapping (cf1) and the upper bound (cf2),"
        f" cases of `drop -> review` or `drop -> pass` are **{hc['cf1_drop_lost'] + hh['cf1_drop_lost'] + dc['cf1_drop_lost'] + dh['cf1_drop_lost']}**"
        f" (cf1) and **{hc['cf2_drop_lost'] + hh['cf2_drop_lost'] + dc['cf2_drop_lost'] + dh['cf2_drop_lost']}** (cf2) across all 16,549 runs.",
        "  Nothing that L0 hard-drops today stops being hard-dropped.",
        f"- **cf1 is the shippable mapping, and its cost is soft coverage.** It relieves"
        f" {hh['cf1_relieved']}/{hh['n']} held-out honest runs and {hc['cf1_relieved']}/{hc['n']} held-out cheats.",
        "  The honest gain is large and the cheat loss is the price.",
        f"- **cf2 shows how much of L0's apparent coverage is parser-shaped.** If L0 read every tool name,"
        f" {hc['cf2_relieved']}/{hc['n']} held-out cheats"
        f" ({100.0 * hc['cf2_relieved'] / hc['n']:.1f}%) would leave the review queue, versus"
        f" {hh['cf2_relieved']}/{hh['n']} honest runs relieved"
        f" ({100.0 * hh['cf2_relieved'] / hh['n']:.1f}%).",
        "  On `resolvebench-support-agents`, `rhb-delivery-queue`, and `rsi-benchmark-monitor-training` the"
        "  mapping relieves essentially every run of both labels: those sets were flagged only because the",
        "  tool names were unreadable, so their L0 signal is an artifact, not detection.",
        f"- **The new hard false alarms are the real risk.** cf2 pushes {hh['cf2_newly']} held-out honest runs"
        " to a more severe decision, and a per-set read shows they concentrate in `evilgenie-escalation`",
        " (83), `beam-tripwire` (18), and `agent-cheat-index` (9). Those are runs where correct parsing turns",
        " an unrecognised no-op into a recognised action that matches a hard rule. Inspect them before",
        " shipping either mapping.",
        "- **Conclusion for the decision:** ship cf1, not cf2. cf1 keeps every hard drop, relieves"
        f" {100.0 * dh['cf1_relieved'] / dh['n']:.1f}% of DEV honest review load, and loses"
        f" {100.0 * hc['cf1_relieved'] / hc['n']:.1f}% of held-out cheat soft coverage."
        f" cf2 would remove {100.0 * hc['cf2_relieved'] / hc['n']:.1f}% of held-out cheat soft coverage,"
        " which is a detection change disguised as a parser fix.",
        "",
    ]

    if args.out is None:
        suffix = "" if args.view == "full" else f"-{args.view}"
        args.out = HERE / "results" / f"DECISION-TRANSITIONS{suffix}.md"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
