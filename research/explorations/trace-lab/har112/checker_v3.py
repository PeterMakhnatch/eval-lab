"""HAR-112 repo-aware checker: the HAR-111 model pass, plus a lookup of every flagged name in the task's own code.

Per task:
1. Sample 1: GLM-5.3-Flash lists unstated test requirements. Each item carries `kind` and the exact `names`
   (identifiers or paths) or `strings` it depends on.
2. Repo check: an item whose names all already exist in the repository at the base state
   (`repo_extract.py` output), or whose exact strings already appear there, is not an unstated requirement.
   It is dropped, and the repo evidence is kept.
3. Cascade: if sample 1 still says broken after the repo check, two more samples run through the same repo
   check and the label is the median of the three (sound < suspect < broken); otherwise sample 1 stands.

Usage (under `keys run --`):
    python3 checker_v3.py --out DIR --repos CACHE [--list FILE] [TASK_DIR ...]
Spend goes to har112/spend.jsonl, capped at $2 total by HAR112_CAP_USD.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "har111" / "checker"))
import checker as v2  # noqa: E402
import rules  # noqa: E402
import zai  # noqa: E402

zai.LEDGER = HERE / "spend.jsonl"
zai.CAP_USD = float(os.environ.get("HAR112_CAP_USD", "2.0"))
TEMPERATURE = 0.7
SAMPLES = 3
# Pre-registered before the fresh holdout: only a sample-1 "broken" call buys two more samples (the gate is about
# broken-call precision); sound and suspect stand on one sample. Keeps the 1,180-task pool under the $2 cap.
CASCADE_ON = {"broken"}

# The severity rules mirror RUBRIC_v2.md (R1-R5 and the minimal-fix tie-break), so the checker and the hand
# labels answer the same question. R1 (pre-existing names) is enforced by repo_filter, not trusted to the model.
SYSTEM_V3 = """You audit coding tasks used to train and evaluate AI agents. The agent sees ONLY the instruction \
and can explore the repository; hidden tests then grade its fix. List every concrete thing the hidden tests require \
that the instruction does not state, and grade each one.

NOT an item (skip it):
- exactly the reported bug or requested feature, or a direct consequence of it; anything clearly implied;
- a check that guards behaviour already working at the base commit, which any correct fix keeps working;
- library, stdlib or test-helper calls, fixtures and scaffolding; answer leaks, difficulty, environment problems.
Names that may already exist in the repository (you cannot see it) are checked automatically afterwards: \
still list them as new_name items with the exact names, and they will be dropped if the repo has them.

Severity:
- New names: a function, method, class, parameter, keyword, field, dict key, alias, module, file path or CLI \
flag the tests use but the instruction never gives -> not_inferable.
- Siblings: a function, method, option value or variant the instruction never names. guessable only if it is \
the same family as the named entity AND the identical change fixes it with no new semantics. not_inferable if it \
needs new semantics (index subsetting, new constructor parameters, new filtering or dedup criteria, new APIs or flags).
- Quoted or linked specs cover only what they literally say. A format, code value or spelling that isn't in \
that text is an item: not_inferable if the quoted text admits two or more reasonable implementations, guessable \
if it fixes the answer up to one convention.
- Exact strings and output channels: required substrings beyond the instruction's words are guessable only if \
each substring is stated in the instruction or produced by the tool the instruction names; otherwise \
not_inferable. The output channel alone (stdout vs stderr) is at most guessable.
- Extra features or aliases beyond the ones requested, an extra error or lifecycle contract, or a test that \
contradicts the instruction -> not_inferable.
- Tie-break: write down the minimal correct fix of exactly what is asked, plus mechanical propagation to siblings. \
If it would pass the test, the item is guessable; if it would fail, not_inferable.

Quote the test line for every item. Do not invent items; return an empty list if nothing qualifies. For every item \
also give:
- "kind": one of new_name (a function/method/class/parameter/keyword/field/key/alias/module/file the fix must \
provide), exact_string (an exact message/format/value), extra_behavior, contradiction, other;
- "names": the exact identifiers or paths the item depends on, as the tests spell them (e.g. "resolve_max_length", \
"src/DownloadModels.py", "sort_by"); empty if none;
- "strings": exact string literals the tests require (for exact_string items); empty if none.

Reply with JSON only:
{"unstated": [{"what": "...", "kind": "...", "names": ["..."], "strings": ["..."], "test_ref": "file:line or test name", \
"quote": "<=200 chars", "severity": "guessable|not_inferable"}],
 "instruction_covers": ["..."],
 "reason": "one or two sentences"}"""

CODE_SUFFIXES = {".py", ".pyi", ".pyx", ".pxd", ".cfg", ".toml", ".ini", ".json", ".yaml", ".yml", ".txt", ".rst",
                 ".md", ".js", ".ts", ".c", ".h", ".cpp", ".rs", ".go", ".java", ".sh", ".html", ".j2", ".jinja"}
_DEF_PATTERNS = [
    r"\b(?:def|class)\s+(\w+)",
    r"^\s*(\w+)\s*(?::[^=\n]*)?=(?!=)",
    r"\bimport\s+([\w., ]+)",
    r"\bfrom\s+\.*([\w.]+)\s+import",
    r"""["'](\w+)["']""",
    r"(?<=[(,])\s*\**(\w+)\s*(?::[^,)=\n]*)?(?=[=,)])",
    r"\bas\s+(\w+)",
    r"\bself\.(\w+)\s*=",
    r"--([\w-]+)",
]
_DEF_RE = re.compile("|".join(f"(?:{p})" for p in _DEF_PATTERNS), re.M)


def repo_root(repos: Path, task_dir: Path) -> Path | None:
    """The extracted repository (/workspace/repo for code tasks, /app for terminal tasks), once extraction finished."""
    extracted = repos / task_dir.name
    return extracted / "repo" if (extracted / "extract.json").is_file() else None


@lru_cache(maxsize=8)
def repo_index(root: Path) -> tuple[dict[str, str], set[str], list[Path]]:
    """(identifier -> first 'file:line' where it is defined or used as a key/param, all relative paths, text files)."""
    defs: dict[str, str] = {}
    paths: set[str] = set()
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        paths.add(rel)
        if path.suffix not in CODE_SUFFIXES:
            continue
        files.append(path)
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for m in _DEF_RE.finditer(text):
            for group in m.groups():
                if not group:
                    continue
                for ident in re.split(r"[,\s]+", group):
                    # a dotted import also makes its last component known (src.DownloadModels -> DownloadModels)
                    for key in {ident, ident.rsplit(".", 1)[-1]} if ident else ():
                        if key and key not in defs:
                            defs[key] = f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"
    return defs, paths, files


def _name_evidence(name: str, root: Path) -> str | None:
    defs, paths, _ = repo_index(root)
    name = name.strip().strip("`'\"").removesuffix("()")
    if not name:
        return None
    if "/" in name or name.endswith(".py"):
        rel = name.lstrip("/").removeprefix("testbed/").removeprefix("workspace/repo/")
        hits = [p for p in paths if p == rel or p.endswith("/" + rel)]
        if hits:
            return f"path {hits[0]}"
        # a module file the existing code already imports (src/DownloadModels.py <- `from src.DownloadModels import`)
        dotted = rel.removesuffix(".py").replace("/", ".")
        ev = defs.get(dotted) if rel.endswith(".py") else None
        return f"imported as {dotted} at {ev}" if ev else None
    if "." in name:
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            mod = "/".join(parts[:i])
            if any(p.endswith(mod + ".py") or p.endswith(mod + "/__init__.py") for p in paths):
                rest = parts[i:]
                if not rest:
                    return f"module {mod}"
                ev = defs.get(rest[-1])
                return f"{name} -> {ev}" if ev else None
        name = parts[-1]
    name = name.lstrip("-")
    ev = defs.get(name) or defs.get(name.replace("-", "_"))
    return ev


def _string_evidence(s: str, root: Path) -> str | None:
    if re.fullmatch(r"[A-Za-z_][\w.]*", s):  # an identifier given as a "string" (e.g. a keyword name)
        return _name_evidence(s, root)
    if len(s) < 6:
        return None
    _, _, files = repo_index(root)
    for path in files:
        try:
            if s in path.read_text(errors="replace"):
                return f"string in {path.relative_to(root).as_posix()}"
        except OSError:
            continue
    return None


def repo_filter(items: list[dict], root: Path | None) -> tuple[list[dict], list[dict]]:
    """Split items into (kept, dropped_as_existing). Items without names or strings are kept."""
    if root is None:
        return items, []
    kept, dropped = [], []
    for item in items:
        names = [n for n in item.get("names") or [] if isinstance(n, str) and n.strip()]
        strings = [s for s in item.get("strings") or [] if isinstance(s, str) and s.strip()]
        evidence = {}
        if item.get("kind") in {"new_name", "exact_string"}:
            evidence = {n: _name_evidence(n, root) for n in names} | {s: _string_evidence(s, root) for s in strings}
        if evidence and all(evidence.values()):
            dropped.append({**item, "repo_evidence": evidence})
        else:
            kept.append({**item, "repo_evidence": evidence} if evidence else item)
    return kept, dropped


class NeedsRepo(RuntimeError):
    """A sample flagged a name or string as not inferable, and the task's repository isn't extracted yet."""


def _raw_sample(r: dict, task: str, k: int, raw_dir: Path) -> dict:
    """One model sample, cached on disk so repo-filter changes and reruns never re-spend."""
    path = raw_dir / f"{task}.s{k}.json"
    if path.is_file():
        return json.loads(path.read_text())
    os.environ["HAR111_TEMPERATURE"] = str(TEMPERATURE)
    content, usage = zai.chat(
        [{"role": "system", "content": SYSTEM_V3}, {"role": "user", "content": v2._prompt(r)}],
        tag=f"har112:{task}:s{k}",
        max_tokens=4000,
    )
    try:
        verdict = json.loads(content)
        items = verdict.get("unstated") if isinstance(verdict.get("unstated"), list) else []
        raw = {"items": items, "reason": verdict.get("reason"), "usage": usage}
    except json.JSONDecodeError:
        raw = {"items": None, "parse_error": content[:300], "usage": usage}
    path.write_text(json.dumps(raw, indent=2) + "\n")
    return raw


def _needs_repo(raw: dict) -> bool:
    return any(
        i.get("severity") == "not_inferable" and i.get("kind") in {"new_name", "exact_string"}
        for i in raw.get("items") or []
    )


def _judge(raw: dict, root: Path | None) -> dict:
    if raw.get("items") is None:
        return {"label": None, "label_without_repo": None, "unstated": [], "dropped_existing": []}
    kept, dropped = repo_filter(raw["items"], root) if _needs_repo(raw) else (raw["items"], [])
    return {
        "label": v2.label_from_items(kept),
        "label_without_repo": v2.label_from_items(raw["items"]),
        "unstated": kept,
        "dropped_existing": dropped,
    }


def _median(labels: list[str | None]) -> str | None:
    order = ["sound", "suspect", "broken"]
    ranks = sorted(order.index(x) for x in labels if x in order)
    return order[ranks[(len(ranks) - 1) // 2]] if ranks else None


def check(task_dir: Path, out_dir: Path, repos: Path) -> dict:
    """Cascade: sample 1; if it says broken after the repo check, samples 2-3 and label = median; else sample 1.

    The repo check runs only on samples that flag a not-inferable name or string. If such a sample exists
    and the repository isn't extracted, raises NeedsRepo; the model samples stay cached for the rerun.
    """
    out = out_dir / f"{task_dir.name}.json"
    if out.is_file():
        return json.loads(out.read_text())
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    r = rules.extract(task_dir)
    root = repo_root(repos, task_dir)
    raws = [_raw_sample(r, task_dir.name, 1, raw_dir)]
    if _needs_repo(raws[0]) and root is None:
        raise NeedsRepo(task_dir.name)
    if _judge(raws[0], root)["label"] in CASCADE_ON:
        raws += [_raw_sample(r, task_dir.name, k, raw_dir) for k in range(2, SAMPLES + 1)]
        if any(_needs_repo(x) for x in raws) and root is None:
            raise NeedsRepo(task_dir.name)
    judged = [_judge(x, root) for x in raws]
    record = {
        "task_id": task_dir.name,
        "label": _median([j["label"] for j in judged]),
        "label_without_repo": _median([j["label_without_repo"] for j in judged]),
        "sample_labels": [j["label"] for j in judged],
        "repo_used": any(_needs_repo(x) for x in raws),
        "unstated": [i for j in judged for i in j["unstated"]],
        "dropped_existing": [i for j in judged for i in j["dropped_existing"]],
        "reasons": [x.get("reason") for x in raws],
        "usage": [x["usage"] for x in raws],
        "model": zai.MODEL,
        "prompt_version": "v3",
    }
    out.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--repos", required=True, type=Path)
    ap.add_argument("--list", type=Path)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("tasks", nargs="*", type=Path)
    a = ap.parse_args()
    tasks = list(a.tasks) + ([Path(x.strip()) for x in a.list.read_text().splitlines() if x.strip()] if a.list else [])
    a.out.mkdir(parents=True, exist_ok=True)
    done, needs = 0, []
    with ThreadPoolExecutor(a.workers) as pool:
        futs = {pool.submit(check, t, a.out, a.repos): t for t in tasks}
        for fut in as_completed(futs):
            t = futs[fut]
            done += 1
            try:
                rec = fut.result()
                if done % 50 == 0 or len(tasks) <= 60:
                    print(f"{done}/{len(tasks)} {t.name} {rec['sample_labels']} -> {rec['label']} "
                          f"(no repo: {rec['label_without_repo']}) spent=${zai.spent_usd():.4f}", flush=True)
            except NeedsRepo:
                needs.append(str(t))
            except zai.SpendCapReached as exc:
                print(f"STOP spend cap: {exc}", flush=True)
                pool.shutdown(cancel_futures=True)
                break
            except Exception as exc:
                print(f"ERROR {t.name}: {exc}", flush=True)
    if needs:
        (a.out / "needs_repo.txt").write_text("".join(n + "\n" for n in sorted(needs)))
        print(f"{len(needs)} tasks need a repo extract: {a.out / 'needs_repo.txt'}")
    print(f"total spent ${zai.spent_usd():.4f}")


if __name__ == "__main__":
    main()
