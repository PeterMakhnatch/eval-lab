"""Score the HAR-112 checker against the frozen fresh-holdout labels, and apply the card's gate.

Final holdout label per task: the label raters A and B agree on; else the adjudicator's label (`hand_adj/`).
Every label set is verified against its sha256 manifest before scoring.

Gate (HAR-111/112):
- the checker flags 002259 and 002407. These are tuning-set tasks, read from the tuning output dir.
- On the fresh holdout, fewer than 1 in 4 of its "broken" calls are wrong. "Wrong" is reported two ways:
  the call lands on a task whose final label is sound (the card's reading), and strictly, any label other than broken.

Usage: python3 score_v3.py HOLDOUT_OUT_DIR TUNING_OUT_DIR
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = ("sound", "suspect", "broken")
MUST_CATCH = ("format-code-task-002259", "format-code-task-002407")


def frozen(label_set: str) -> dict[str, dict]:
    out = {}
    for line in (HERE / f"{label_set}.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        path = HERE / label_set / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{label_set}/{name} changed after the freeze")
        out[path.stem] = json.loads(path.read_text())
    return out


def final_labels() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    a = {t: r["label"] for t, r in frozen("hand_a").items()}
    b = {t: r["label"] for t, r in frozen("hand_b").items()}
    adj = {t: r["label"] for t, r in frozen("hand_adj").items()} if (HERE / "hand_adj.sha256").is_file() else {}
    final = {}
    for t in a:
        final[t] = a[t] if a[t] == b.get(t) else adj.get(t)
    return final, a, b


def _pred(out_dir: Path, task: str) -> str | None:
    path = out_dir / f"{task}.json"
    return json.loads(path.read_text()).get("label") if path.is_file() else None


def agreement(x: dict[str, str], y: dict[str, str]) -> str:
    both = [t for t in x if t in y and x[t] and y[t]]
    exact = sum(x[t] == y[t] for t in both)
    xb = [t for t in both if x[t] == "broken"]
    return f"exact {exact}/{len(both)}; of first's {len(xb)} broken calls, {sum(y[t] == 'sound' for t in xb)} are sound to the second"


def main(holdout_dir: Path, tuning_dir: Path) -> None:
    final, a, b = final_labels()
    pred = {t: _pred(holdout_dir, t) for t in final}
    rows = ["| final \\ checker | sound | suspect | broken | none |", "|---|---|---|---|---|"]
    for h in LABELS:
        counts = [sum(1 for t in final if final[t] == h and pred[t] == p) for p in (*LABELS, None)]
        rows.append(f"| {h} | " + " | ".join(map(str, counts)) + " |")
    broken_calls = [t for t in final if pred[t] == "broken"]
    on_sound = [t for t in broken_calls if final[t] == "sound"]
    not_broken = [t for t in broken_calls if final[t] != "broken"]
    caught = {t: _pred(tuning_dir, t) for t in MUST_CATCH}
    catch_ok = all(v in {"suspect", "broken"} for v in caught.values())
    precision_ok = bool(broken_calls) and len(on_sound) / len(broken_calls) < 0.25
    hand_broken = [t for t in final if final[t] == "broken"]
    print("\n".join([
        f"Holdout: {len(final)} tasks; final labels "
        + ", ".join(f"{sum(v == k for v in final.values())} {k}" for k in LABELS)
        + f"; unresolved {sum(v is None for v in final.values())}",
        "",
        *rows,
        "",
        f"- Exact agreement with final labels: {sum(pred[t] == final[t] for t in final)}/{len(final)}",
        f"- Must-catch (tuning set): {caught} -> {'ok' if catch_ok else 'MISSED'}",
        f"- 'broken' calls on the holdout: {len(broken_calls)}; on final-sound: {on_sound}; "
        f"strictly not broken: {not_broken}",
        f"- Hand-broken caught as broken: {sum(pred[t] == 'broken' for t in hand_broken)}/{len(hand_broken)} {hand_broken}",
        f"- **Gate: {'PASS' if catch_ok and precision_ok else 'FAIL'}**"
        + ("" if broken_calls else " (no broken calls on the holdout: precision undefined)"),
        "",
        "Rater agreement:",
        f"- A vs B: {agreement(a, b)}",
        f"- B vs A: {agreement(b, a)}",
        f"- checker vs A: {agreement(pred, a)}",
        f"- checker vs B: {agreement(pred, b)}",
        "",
        "Disagreements with final labels:",
        *[f"- {t}: final {final[t]} (A {a[t]}, B {b.get(t)}) / checker {pred[t]}" for t in final if pred[t] != final[t]],
    ]))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
