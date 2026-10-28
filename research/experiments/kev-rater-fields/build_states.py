"""Cohort and Kev states for the rater-field pilot.

Cohort: every run with frozen two-rater blind labels under
``research/explorations/trace-lab`` (HAR-119, HAR-128 g2/g6, HAR-116 loopfix):
154 MiMo-V2.6-Distill-Qwen-9B / Terminus-2 Python bug-fix runs. Labels are read
from the committed files; run bytes come from the published results home
(``~/Developer/eval-lab-results``) or, for the blinded G6 packs, from
``derived/trace-lab/har128-g6/packs`` (what the G6 raters read).

Writes (``derived/kev-rater-fields/``, ignored):

- ``cohort.jsonl``  one row per run: ids, source dir, both raters' field values
  and the consensus (``null`` where raters disagree).
- ``digest.jsonl``  one trace-level state per run (≤ ~6k Qwen tokens): task text,
  the run facts a rater sees in ``result.json``, a one-line-per-step action
  outline and the last steps in full.
- ``windows.jsonl`` ~5k-token sliding windows with the 800-token task prefix,
  the exact shape of ``kev-hack-detector/build_chunks.py``, each carrying the
  ordered step refs (``head#N``) it covers, so a rater's ``first_failure`` ref
  maps to windows.

Usage (worktree root):
  uv run --with transformers python research/experiments/kev-rater-fields/build_states.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
sys.path.insert(0, str(WORKTREE / "research/experiments/kev-hack-detector"))

from build_chunks import (  # noqa: E402
    PREFIX_TOKENS,
    chunk_units,
    get_counter,
    render_atif_step,
    unit_char_spans,
)

TRACE_LAB = WORKTREE / "research/explorations/trace-lab"
LABEL_SETS = {
    "har119": TRACE_LAB / "har119/labels",
    "g6": TRACE_LAB / "har128/g6/labels",
    "g2_a1": TRACE_LAB / "har128/labels_g2_a1",
    "g2_r2": TRACE_LAB / "har128/labels_g2_r2",
    "g2_tail": TRACE_LAB / "har128/labels_g2_tail",
    "har116": TRACE_LAB / "har128/labels_har116",
}
RESULTS_HOME = Path.home() / "Developer/eval-lab-results"
G6_PACKS = Path.home() / "Developer/eval-lab/derived/trace-lab/har128-g6/packs"
OUT = WORKTREE / "derived/kev-rater-fields"

DIGEST_TASK_TOKENS = 600
DIGEST_OUTLINE_TOKENS = 3600
DIGEST_TAIL_TOKENS = 1500
OUTLINE_CHARS = 140
HARNESS_FACTS = (
    "Terminus-2 harness: about 120 model calls and 2.5M input tokens per run; "
    "some runs have a loop detector that nudges and then stops a run that keeps repeating itself."
)


def _sft_clean(label: dict) -> bool | None:
    cut = label.get("sft_cut")
    return None if cut is None else bool(cut.get("clean"))


FIELDS = {
    "stop_reason": lambda lab: lab.get("stop_reason"),
    "blame": lambda lab: lab.get("blame"),
    "loop_kind": lambda lab: lab.get("loop_kind"),
    "pass_copied": lambda lab: lab.get("pass_copied"),
    "sft_clean": _sft_clean,
    "first_failure_ref": lambda lab: (lab.get("first_failure") or {}).get("ref"),
}


def load_labels() -> dict[tuple[str, str], dict[str, dict]]:
    out: dict[tuple[str, str], dict[str, dict]] = {}
    for name, root in LABEL_SETS.items():
        for rater in ("rater_a", "rater_b"):
            for f in sorted((root / rater).glob("*.json")):
                out.setdefault((name, f.stem), {})[rater] = json.loads(f.read_text())
    bad = [k for k, v in out.items() if set(v) != {"rater_a", "rater_b"}]
    if bad:
        raise SystemExit(f"runs without two raters: {bad}")
    return out


def index_results(names: set[str]) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for date in sorted(p for p in RESULTS_HOME.iterdir() if p.is_dir()):
        for job in sorted(p for p in date.iterdir() if p.is_dir()):
            for kid in job.iterdir():
                if kid.name in names:
                    found.setdefault(kid.name, []).append(kid)
    return found


def trajectory_files(run: Path) -> list[tuple[str, Path]]:
    base = run / "agent" if (run / "agent").is_dir() else run
    head = base / "trajectory.json"
    if not head.is_file():
        return []
    conts = sorted(
        base.glob("trajectory.cont-*.json"),
        key=lambda p: int(re.search(r"cont-(\d+)", p.name).group(1)),
    )
    return [("head", head)] + [
        (f"cont-{re.search(r'cont-(\d+)', p.name).group(1)}", p) for p in conts
    ]


def run_facts(run: Path) -> dict:
    """The result.json facts a rater reads (reward, exception, model calls)."""
    summary = run / "result_summary.json"
    if summary.is_file():  # blinded G6 pack
        r = json.loads(summary.read_text())
        facts = {
            "reward": r.get("reward"),
            "exception_type": r.get("exception_type"),
            "exception_message": r.get("exception_message") or "",
            "model_calls": r.get("n_episodes"),
        }
    else:
        r = json.loads((run / "result.json").read_text())
        rewards = (r.get("verifier_result") or {}).get("rewards") or {}
        exc = r.get("exception_info") or {}
        meta = (r.get("agent_result") or {}).get("metadata") or {}
        facts = {
            "reward": rewards.get("reward"),
            "exception_type": exc.get("exception_type"),
            "exception_message": exc.get("exception_message") or "",
            "model_calls": meta.get("n_episodes"),
        }
    diff = run / "verifier/agent.diff"
    if diff.is_file():
        lines = diff.read_text(errors="replace").splitlines()
        facts["diff_changed_lines"] = sum(
            1
            for x in lines
            if x[:1] in "+-" and not x.startswith(("+++", "---"))
        )
    else:
        facts["diff_changed_lines"] = None
    return facts


def task_text(first_user_message: str) -> str:
    """The task statement inside the Terminus-2 prompt (the rest is harness boilerplate)."""
    m = re.search(
        r"Task Description:\s*(.*?)(?:\n\s*Current terminal state:|\Z)", first_user_message, re.S
    )
    return m.group(1).strip() if m else first_user_message


def _outline_line(ref: str, step: dict, text: str) -> str:
    """One line per agent step: the start of its message, then each command it sent."""
    said = re.sub(r"^\s*Analysis:\s*", "", text or "")
    cmds = []
    for c in step.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        args = c.get("arguments", c.get("input", ""))
        fn = c.get("function_name", "?")
        if isinstance(args, dict) and "keystrokes" in args:
            cmds.append("$ " + str(args["keystrokes"]))
        else:
            cmds.append(f"{fn}({json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args})")
    line = " ".join(said.split())[:70]
    if cmds:
        line += " || " + " ; ".join(" ".join(x.split()) for x in cmds)
    return f"[{ref}] {line[:OUTLINE_CHARS]}"


def step_units(files: list[tuple[str, Path]]) -> tuple[str, list[str], list[str], list[str]]:
    """(task text, units, refs, outline lines) over head + continuation files in order."""
    task, units, refs, outline = "", [], [], []
    for tag, path in files:
        traj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        for step in traj.get("steps", []):
            src = step.get("source", "?")
            msg = step.get("message")
            text = msg if isinstance(msg, str) else " ".join(
                p.get("text", "") for p in msg or [] if isinstance(p, dict)
            )
            ref = f"{tag}#{step.get('step_id')}"
            if not task and src == "user" and text:
                # the harness prompt is boilerplate; keep only the task statement
                task = task_text(text)
                units.append(f"[{ref} user]\n{task}")
            else:
                body = render_atif_step(step).split("\n", 1)
                units.append(f"[{ref} {src}]" + ("\n" + body[1] if len(body) > 1 else ""))
            refs.append(ref)
            if src == "agent":
                outline.append(_outline_line(ref, step, text))
    return task, units, refs, outline


def _clip(counter, text: str, n: int) -> str:
    ids = counter.encode(text)
    if len(ids) <= n:
        return text
    try:
        return counter.tok.decode(ids[:n]) + "\n[...]"
    except AttributeError:
        return text[: int(n * 3.5)] + "\n[...]"


def _fit_outline(counter, lines: list[str], budget: int) -> str:
    """Drop middle steps until the outline fits, so the start and the end survive."""
    keep = list(range(len(lines)))
    while len(keep) > 2 and len(counter.encode("\n".join(lines[i] for i in keep))) > budget:
        keep.pop(len(keep) // 2)
    if len(keep) == len(lines):
        return "\n".join(lines)
    mid = len(keep) // 2
    return "\n".join(
        [lines[i] for i in keep[:mid]]
        + [f"[... {len(lines) - len(keep)} middle steps omitted ...]"]
        + [lines[i] for i in keep[mid:]]
    )


def digest_state(counter, task: str, facts: dict, outline: list[str], units: list[str]) -> str:
    fact_lines = [
        f"verifier reward: {facts['reward']}",
        f"model calls used: {facts['model_calls']}",
        f"agent steps in trajectory: {len(outline)}",
        f"lines changed in the final diff: {facts['diff_changed_lines']}",
        "run ended with exception: "
        + (
            f"{facts['exception_type']}: {facts['exception_message'][:200]}"
            if facts["exception_type"]
            else "none"
        ),
        HARNESS_FACTS,
    ]
    tail = _clip(counter, "\n\n---\n\n".join(units[-3:]), DIGEST_TAIL_TOKENS) if units else ""
    return (
        f"[task]\n{_clip(counter, task, DIGEST_TASK_TOKENS)}\n\n"
        f"[run facts]\n" + "\n".join(fact_lines) + "\n\n"
        f"[action outline: one line per agent step]\n"
        f"{_fit_outline(counter, outline, DIGEST_OUTLINE_TOKENS) if outline else '(no agent steps recorded)'}\n\n"
        f"[last steps in full]\n{tail or '(none)'}"
    )


def main() -> None:
    labels = load_labels()
    found = index_results({tid for (s, tid) in labels if s != "g6"})
    counter = get_counter()
    OUT.mkdir(parents=True, exist_ok=True)
    n_windows = 0
    with (
        (OUT / "cohort.jsonl").open("w") as fc,
        (OUT / "digest.jsonl").open("w") as fd,
        (OUT / "windows.jsonl").open("w") as fw,
    ):
        for (lset, tid), raters in sorted(labels.items()):
            if lset == "g6":
                run = G6_PACKS / tid
            else:
                hits = found.get(tid) or []
                if len(hits) != 1:
                    raise SystemExit(f"{tid}: expected one published run, found {hits}")
                run = hits[0]
            rid = f"{lset}:{tid}"
            files = trajectory_files(run)
            task, units, refs, outline = step_units(files)
            facts = run_facts(run)
            if not task:
                task = (run / "instruction.md").read_text() if (run / "instruction.md").is_file() else ""
            vals = {
                f: {r: fn(raters[r]) for r in ("rater_a", "rater_b")} for f, fn in FIELDS.items()
            }
            row = {
                "id": rid,
                "label_set": lset,
                "trial": tid,
                "run_dir": str(run),
                "has_trajectory": bool(files),
                "n_steps": len(refs),
                "facts": facts,
                "raters": vals,
                "consensus": {
                    f: (v["rater_a"] if v["rater_a"] == v["rater_b"] else None)
                    for f, v in vals.items()
                    if f != "first_failure_ref"
                },
                "first_failure_refs": sorted(
                    {v for v in vals["first_failure_ref"].values() if v}
                ),
                "step_refs": refs,
            }
            fc.write(json.dumps(row, ensure_ascii=False) + "\n")
            fd.write(
                json.dumps(
                    {"chunk_id": rid, "state": digest_state(counter, task, facts, outline, units)},
                    ensure_ascii=False,
                )
                + "\n"
            )
            if not units:
                continue
            states, spans = chunk_units(task, units, counter)
            uspans = unit_char_spans(units)
            for i, (state, (c0, c1)) in enumerate(zip(states, spans, strict=True)):
                covered = [refs[j] for j, (u0, u1) in enumerate(uspans) if u0 < c1 and u1 > c0]
                fw.write(
                    json.dumps(
                        {
                            "chunk_id": f"{rid}:w{i:03d}",
                            "run_id": rid,
                            "window_index": i,
                            "n_windows": len(states),
                            "refs": covered,
                            "state": state,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                n_windows += 1
    print(f"{len(labels)} runs, {n_windows} windows, prefix {PREFIX_TOKENS} tokens -> {OUT}")


if __name__ == "__main__":
    main()
