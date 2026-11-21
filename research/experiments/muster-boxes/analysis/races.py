import json
import sys
from collections import Counter
from itertools import pairwise
from pathlib import Path
from statistics import fmean

ROOM_SIZE = 10
BATCH_GAP_SECONDS = 2.0
MAX_TRIALS = 1000
MAX_EVENTS = 100_000
COLUMNS = (
    "trials",
    "reward",
    "new_flag",
    "new_empty",
    "repeats",
    "repeats_unseen",
    "repeats_seen",
    "max_seat_opens",
    "messages",
    "opens_per_batch",
    "obvious_share",
    "repeat_obvious_share",
)


def played(trial: Path) -> list[dict]:
    lines = (trial / "verifier" / "events.jsonl").read_text().splitlines()
    assert len(lines) <= MAX_EVENTS, trial
    events = [json.loads(line) for line in lines if line.strip()]
    return [e for e in events if e.get("ok") and e.get("seat") and not e.get("late")]


def batches(events: list[dict]) -> list[int]:
    times: dict[str, list[float]] = {}
    for e in events:
        if e["kind"] == "open":
            times.setdefault(e["seat"], []).append(e["t"])
    sizes = []
    for seat_times in times.values():
        size = 1
        for before, after in pairwise(seat_times):
            if after - before < BATCH_GAP_SECONDS:
                size += 1
            else:
                sizes.append(size)
                size = 1
        sizes.append(size)
    return sizes


def learn(known: dict[str, set[int]], e: dict) -> None:
    seen = known.setdefault(e["seat"], set())
    if e["kind"] == "open":
        seen.add(e["response"]["box"])
    for other in e["response"].get("team_opens", []):
        seen.add(other["box"])


def obvious(known: set[int], box: int) -> bool:
    room = (box - 1) // ROOM_SIZE
    free = [b for b in range(room * ROOM_SIZE + 1, (room + 1) * ROOM_SIZE + 1) if b not in known]
    return bool(free) and box == free[0]


def trial_row(trial: Path) -> dict:
    events = played(trial)
    first: dict[int, str] = {}
    known: dict[str, set[int]] = {}
    per_seat: Counter = Counter()
    c: Counter = Counter()
    for e in events:
        if e["kind"] == "opened":
            learn(known, e)
        if e["kind"] == "msg" and e["method"] == "POST":
            c["messages"] += 1
        if e["kind"] != "open":
            continue
        seat, box = e["seat"], e["response"]["box"]
        per_seat[seat] += 1
        c["opens"] += 1
        is_obvious = obvious(known.get(seat, set()), box)
        c["obvious"] += is_obvious
        if box in first:
            c["repeats"] += 1
            c["repeat_obvious"] += is_obvious
            if first[box] != seat:
                c["repeats_seen" if box in known.get(seat, set()) else "repeats_unseen"] += 1
        else:
            first[box] = seat
            c["new_flag" if e["response"]["flag"] else "new_empty"] += 1
        learn(known, e)
    sizes = batches(events)
    reward = json.loads((trial / "verifier" / "reward.json").read_text())["reward"]
    assert 0.0 <= reward <= 1.0, trial
    return {
        "reward": reward,
        "new_flag": c["new_flag"],
        "new_empty": c["new_empty"],
        "repeats": c["repeats"],
        "repeats_unseen": c["repeats_unseen"],
        "repeats_seen": c["repeats_seen"],
        "max_seat_opens": max(per_seat.values(), default=0),
        "messages": c["messages"],
        "batch_sizes": sizes,
        "obvious": c["obvious"],
        "opens": c["opens"],
        "repeat_obvious": c["repeat_obvious"],
    }


def arm_row(job: Path) -> dict:
    trials = sorted(p for p in job.iterdir() if (p / "verifier" / "events.jsonl").is_file())
    assert 0 < len(trials) <= MAX_TRIALS, job
    rows = [trial_row(t) for t in trials]
    sizes = [s for r in rows for s in r.pop("batch_sizes")]
    mean = {k: fmean(r[k] for r in rows) for k in rows[0]}
    opens = sum(r["opens"] for r in rows)
    repeats = sum(r["repeats"] for r in rows)
    return {
        **mean,
        "trials": len(rows),
        "opens_per_batch": sum(sizes) / len(sizes) if sizes else 0.0,
        "obvious_share": sum(r["obvious"] for r in rows) / opens if opens else 0.0,
        "repeat_obvious_share": sum(r["repeat_obvious"] for r in rows) / repeats if repeats else 0.0,
    }


def main(argv: list[str]) -> int:
    assert argv, "usage: races.py JOB_DIR [JOB_DIR ...]"
    print("arm".ljust(24) + "".join(c[:14].rjust(15) for c in COLUMNS))
    for path in argv:
        row = arm_row(Path(path))
        cells = "".join(
            (f"{row[c]:.0f}" if c == "trials" else f"{row[c]:.2f}").rjust(15) for c in COLUMNS
        )
        print(Path(path).name[:23].ljust(24) + cells)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
