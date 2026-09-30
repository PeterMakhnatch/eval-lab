"""Instruction-vs-hidden-test checker: rule pass plus one GLM-5.3-Flash pass per task.

Usage (under `keys run --` for ZAI_API_KEY):
    python3 checker.py --out DIR TASK_DIR [TASK_DIR ...]   # resumable: skips tasks with an output file
    python3 checker.py --out DIR --list FILE               # one task dir per line

Writes DIR/<task_id>.json with rule_label, model label, unstated items and usage. Spend is capped
and logged by zai.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rules  # noqa: E402
import zai  # noqa: E402

MAX_TEST_CHARS = 40_000
MAX_INSTRUCTION_CHARS = 16_000

SYSTEM = """You audit coding tasks used to train and evaluate AI agents. An agent sees ONLY the instruction \
(plus the repository). Hidden tests then grade its fix. Your job: find every behaviour the hidden tests \
require that the instruction does not state or clearly imply.

Labels:
- sound: everything the tests check is stated, clearly implied, or already fixed by the existing code or public \
API. That covers names, signatures, exception types, messages, return types and paths. A careful engineer would pass.
- suspect: at least one checked behaviour is unstated but plausibly guessable (a natural exception type, a \
conventional return type, a sibling function the wording could cover). A careful engineer might miss it.
- broken: at least one checked behaviour cannot reasonably be inferred. Examples: an exact error-message string, \
an extra API or contract never mentioned, a specific output or line format, or tests that contradict the \
instruction. A correct fix to the stated problem would still fail.

Guidance:
- You cannot see the repository. Names in the diff's unchanged context lines already exist. Library, stdlib and \
test-helper calls are not requirements. Only count things the fix itself must provide or behave like.
- Tests that re-check existing behaviour, or that exercise exactly the reported bug, are covered.
- Do not flag answer leaks, difficulty, or environment problems.
- Severity per item: "guessable" or "not_inferable". Any not_inferable item makes the task broken; else any \
guessable item makes it suspect; else it is sound.
- Be precise and conservative. Flag only concrete test requirements, each quoting the test line.

Reply with JSON only:
{"label": "sound|suspect|broken",
 "unstated": [{"what": "...", "test_ref": "file:line or test name", "quote": "<=200 chars", "severity": "guessable|not_inferable"}],
 "instruction_covers": ["..."],
 "reason": "one or two sentences"}"""

# v2 (after v1 under-graded severity on the 30 validation tasks): concrete severity tests, and the label is
# derived from item severities in code rather than taken from the model.
SYSTEM_V2 = """You audit coding tasks used to train and evaluate AI agents. An agent sees ONLY the instruction \
(plus the repository). Hidden tests then grade its fix. List every concrete thing the hidden tests require that the \
instruction does not state, then grade each one.

An item is NOT an item (skip it) when:
- it is exactly the reported bug or requested feature, or a direct consequence of it;
- the name or behaviour already exists in the code: it appears in the diff's unchanged context lines, or the \
instruction describes a regression or repair of existing code that plainly uses it;
- it is a library, stdlib or test-helper call, or test scaffolding;
- it is an answer leak, difficulty or environment problem.

severity "not_inferable": the fix must provide something the instruction never asks for and that nobody could pick \
without seeing the tests:
- a NEW name the tests import, call or read (function, method, class, parameter, keyword, dict key, field, alias, \
CLI flag, file path) that is absent from the instruction and not plausibly pre-existing;
- an exact string, message, number or output format that is not given in the instruction or in a spec it quotes;
- extra features, variants or aliases beyond the ones requested, or an extra error or lifecycle contract;
- a test that contradicts the instruction.

severity "guessable": a choice among a few natural options that a careful engineer would often get right, such as:
- the conventional exception type when the instruction says "raise an error";
- a conventional return type or container;
- a sibling case that the instruction's own wording clearly covers.

Quote the test line for every item. Do not invent items. If nothing qualifies, return an empty list.

Reply with JSON only:
{"unstated": [{"what": "...", "test_ref": "file:line or test name", "quote": "<=200 chars", "severity": "guessable|not_inferable"}],
 "instruction_covers": ["..."],
 "reason": "one or two sentences"}"""

PROMPT_VERSION = os.environ.get("HAR111_PROMPT", "v2")


def label_from_items(items: list[dict]) -> str:
    severities = {i.get("severity") for i in items}
    if "not_inferable" in severities:
        return "broken"
    return "suspect" if items else "sound"


def _prompt(r: dict) -> str:
    instruction = r["instruction"][:MAX_INSTRUCTION_CHARS]
    tests = r["tests_text"]
    if len(tests) > MAX_TEST_CHARS:
        tests = tests[:MAX_TEST_CHARS] + f"\n<... {len(r['tests_text']) - MAX_TEST_CHARS} more characters elided>"
    unstated = [s for s in r["specifics"] if not s["stated"]][:40]
    hints = "\n".join(f"- {s['kind']}: {s['value'][:120]} ({s['file']}:{s['line']})" for s in unstated) or "- none"
    cmd = (r["test_command"] or "").strip()[:1500] or "(not recoverable)"
    return (
        f"## Instruction\n{instruction}\n\n## Hidden tests\n{tests}\n\n## Test command\n{cmd}\n\n"
        f"## Rule pass: test specifics whose text does not appear in the instruction (noisy; many are library calls)\n{hints}\n"
    )


def check(task_dir: Path, out_dir: Path) -> dict:
    out = out_dir / f"{task_dir.name}.json"
    if out.is_file():
        return json.loads(out.read_text())
    r = rules.extract(task_dir)
    system = SYSTEM if PROMPT_VERSION == "v1" else SYSTEM_V2
    content, usage = zai.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": _prompt(r)}],
        tag=f"check:{task_dir.name}",
        max_tokens=4000,
    )
    try:
        verdict = json.loads(content)
    except json.JSONDecodeError:
        verdict = {"parse_error": content[:500]}
    items = verdict.get("unstated", []) if isinstance(verdict.get("unstated"), list) else []
    if "parse_error" in verdict:
        label = None
    elif PROMPT_VERSION == "v1":
        label = verdict.get("label")
    else:
        label = label_from_items(items)
    record = {
        "task_id": task_dir.name,
        "rule_label": r["rule_label"],
        "rule_unstated": [s for s in r["specifics"] if not s["stated"]],
        "label": label,
        "unstated": items,
        "instruction_covers": verdict.get("instruction_covers", []),
        "reason": verdict.get("reason"),
        "parse_error": verdict.get("parse_error"),
        "usage": usage,
        "model": zai.MODEL,
        "prompt_version": PROMPT_VERSION,
        "reasoning_effort": os.environ.get("HAR111_REASONING", "low"),
        "temperature": float(os.environ.get("HAR111_TEMPERATURE", "0")),
    }
    out.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--list", type=Path)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("tasks", nargs="*", type=Path)
    args = ap.parse_args()
    tasks = list(args.tasks)
    if args.list:
        tasks += [Path(line.strip()) for line in args.list.read_text().splitlines() if line.strip()]
    args.out.mkdir(parents=True, exist_ok=True)
    done = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(check, t, args.out): t for t in tasks}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rec = fut.result()
                done += 1
                if done % 50 == 0 or len(tasks) <= 40:
                    print(f"{done}/{len(tasks)} {t.name} {rec['rule_label']}->{rec['label']} spent=${zai.spent_usd():.4f}", flush=True)
            except zai.SpendCapReached as exc:
                print(f"STOP spend cap: {exc}", flush=True)
                pool.shutdown(cancel_futures=True)
                break
            except Exception as exc:  # keep going; the task stays unchecked and a rerun retries it
                print(f"ERROR {t.name}: {exc}", flush=True)
    print(f"total spent ${zai.spent_usd():.4f}")


if __name__ == "__main__":
    main()
