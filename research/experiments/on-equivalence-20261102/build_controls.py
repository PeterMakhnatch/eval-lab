#!/usr/bin/env python3
"""Constructs at least 40 known-equivalent negative controls from the reference code
of the 22 surviving mutants, proves their AST difference, and executes the reference grader
via Modal to verify reward == 1.0.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/on-a2b-equiv-20261102")
sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.mutant_auditor import (  # noqa: E402
    SandboxMeter,
    _apply_reference,
    _write,
    modal_cell_runner,
    parse_probe,
)
from evallab.regrade import CloudSpendGate  # noqa: E402

A2_TASKS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks")
CERT_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/FIX-A2B-SPECIFICITY/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/FIX-A2B-SPECIFICITY/receipts"
EXP_DIR = REPO_ROOT / "research/experiments/on-equivalence-20261102"
CENSUS_PATH = REPO_ROOT / "research/experiments/on-weak-grader-census-20261102/census.json"


def get_context(source: str, line_no: int) -> str:
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
        enclosing = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and hasattr(node, "lineno")
                and hasattr(node, "end_lineno")
                and node.lineno <= line_no <= node.end_lineno
                and (enclosing is None or (node.end_lineno - node.lineno < enclosing.end_lineno - enclosing.lineno))
            ):
                enclosing = node
        if enclosing:
            start = max(1, enclosing.lineno)
            end = min(len(lines), enclosing.end_lineno)
            if end - start > 100:
                start = max(1, line_no - 40)
                end = min(len(lines), line_no + 40)
            return "\n".join(f"{i:4d} | {lines[i-1]}" for i in range(start, end + 1))
    except Exception:
        pass
    start = max(1, line_no - 30)
    end = min(len(lines), line_no + 30)
    return "\n".join(f"{i:4d} | {lines[i-1]}" for i in range(start, end + 1))


def make_diff(path: str, orig: str, modified: str) -> str:
    orig_lines = orig.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def main() -> None:
    CERT_RECEIPTS.mkdir(parents=True, exist_ok=True)
    DURABLE_RECEIPTS.mkdir(parents=True, exist_ok=True)

    with open(EXP_DIR / "prereg.md") as f:
        pass  # Ensure prereg exists

    with open("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A2B-EQUIV/receipts/opaque_mapping.json") as f:
        mapping = json.load(f)

    with open(CENSUS_PATH) as f:
        census = {t["task_id"]: t for t in json.load(f)["tasks"]}

    survivors = [m for m in mapping if m["is_survivor"]]
    print(f"Loaded {len(survivors)} survivors from A2B mapping.")

    # We will build definitions for 44 controls (2 per survivor)
    # We load each task's probe source code
    task_sources: dict[str, dict[str, str]] = {}
    for s in survivors:
        tid = s["task_id"]
        if tid not in task_sources:
            probe_path = A2_TASKS_DIR / tid / "probe" / "result.json"
            pr = json.loads(probe_path.read_text())
            task_sources[tid] = parse_probe(pr.get("agent_log", ""))

    controls: list[dict[str, Any]] = []

    # Helper to validate and add control
    def add_ctrl(
        ctrl_id: str,
        surv: dict[str, Any],
        new_source: str,
        op_type: str,
        category: str,  # 'easy' or 'hard'
        description: str,
    ) -> None:
        tid = surv["task_id"]
        path = surv["path"]
        orig_source = task_sources[tid][path]

        # 1. Parse both to AST
        orig_ast = ast.parse(orig_source)
        new_ast = ast.parse(new_source)

        # 2. Check AST difference
        orig_dump = ast.dump(orig_ast)
        new_dump = ast.dump(new_ast)
        assert orig_dump != new_dump, f"AST did not change for {ctrl_id}!"

        # 3. Create diff
        diff_text = make_diff(path, orig_source, new_source).strip()
        assert diff_text, f"Empty diff for {ctrl_id}!"

        # 4. Context code
        context_code = get_context(new_source, surv["line"])

        # 5. Task instruction
        pkg_path = Path(census[tid]["package_path"])
        instruction = (pkg_path / "instruction.md").read_text().strip()

        controls.append({
            "control_id": ctrl_id,
            "source_survivor": surv["opaque_id"],
            "task_id": tid,
            "path": path,
            "line": surv["line"],
            "operator": op_type,
            "category": category,
            "description": description,
            "diff_text": diff_text,
            "context_code": context_code,
            "instruction": instruction,
            "new_source": new_source,
            "status": "known_equivalent",
        })

    for _idx, s in enumerate(survivors, 1):
        tid = s["task_id"]
        path = s["path"]
        line = s["line"]
        src = task_sources[tid][path]
        lines = src.splitlines(keepends=True)

        # Control A: Easy rewrite
        # Control B: Easy or Hard rewrite

        if s["opaque_id"] == "MUT-01":
            # localstack/services/sqs/provider.py:522
            # line 522: check_message_size(...)
            # Ctrl 1: wrap check_message_size in try/finally: pass (easy)
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            stmt = lines_a[l_idx].strip()
            lines_a[l_idx] = f"{indent}try:\n{indent}    {stmt}\n{indent}finally:\n{indent}    pass\n"
            add_ctrl("CTRL-01A", s, "".join(lines_a), "try_finally", "easy", "Wrap check_message_size in try/finally: pass")

            # Ctrl 2: insert dead branch `if False: pass` (easy)
            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-01B", s, "".join(lines_b), "dead_branch", "easy", "Add dead branch if False: pass before check_message_size")

        elif s["opaque_id"] == "MUT-06":
            # batch/batch/client.py:242
            # line 243: if parent_ids is None: parent_ids = []
            # Ctrl 1: symmetric equality `if None is parent_ids:` (easy)
            l_idx = 242  # line 243 is index 242
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("if parent_ids is None:", "if None is parent_ids:")
            add_ctrl("CTRL-06A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric equality None is parent_ids")

            # Ctrl 2: dead branch in create_job (easy)
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-06B", s, "".join(lines_b), "dead_branch", "easy", "Add dead branch in create_job")

        elif s["opaque_id"] == "MUT-09":
            # impedance/models/circuits/fitting.py:87
            # line 87: if raw_element == 'E' and i == 1:
            # Ctrl 1: commute pure and `if i == 1 and raw_element == 'E':` (easy)
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("if raw_element == 'E' and i == 1:", "if i == 1 and raw_element == 'E':")
            add_ctrl("CTRL-09A", s, "".join(lines_a), "commute_and", "easy", "Commute and operands on pure comparisons")

            # Ctrl 2: symmetric equality on comparison `if 'E' == raw_element and 1 == i:` (easy)
            lines_b = list(lines)
            lines_b[l_idx] = lines_b[l_idx].replace("if raw_element == 'E' and i == 1:", "if 'E' == raw_element and 1 == i:")
            add_ctrl("CTRL-09B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric equality on operands")

        elif s["opaque_id"] == "MUT-11":
            # lib/rucio/daemons/reaper/reaper.py:348
            # logger(...)
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            lines_a.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-11A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch before logger call")

            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}assert True\n")
            add_ctrl("CTRL-11B", s, "".join(lines_b), "redundant_assert", "easy", "Insert redundant assert True")

        elif s["opaque_id"] == "MUT-15":
            # bugbug/repository.py:270
            # line 270: if function["error"] or function["end_line"] < line:
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("function[\"end_line\"] < line", "line > function[\"end_line\"]")
            add_ctrl("CTRL-15A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric boundary comparison line > end_line")

            # Ctrl 2: local variable rename last_f -> _last_f_idx (hard: non-local lexical scope)
            lines_b = list(lines)
            src_b = "".join(lines_b)
            # rename last_f inside get_touched
            src_b = re.sub(r"\blast_f\b", "_last_f_idx", src_b)
            add_ctrl("CTRL-15B", s, src_b, "rename_var", "hard", "Consistent local variable rename last_f -> _last_f_idx")

        elif s["opaque_id"] == "MUT-29":
            # fastavro/_read_py.py:60
            # line 60: if isinstance(writer_type, dict) or isinstance(reader_type, dict):
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace(
                "if isinstance(writer_type, dict) or isinstance(reader_type, dict):",
                "if isinstance(reader_type, dict) or isinstance(writer_type, dict):"
            )
            add_ctrl("CTRL-29A", s, "".join(lines_a), "commute_or", "easy", "Commute boolean or operands in isinstance check")

            # line 62: if writer_type == reader_type:
            lines_b = list(lines)
            lines_b[61] = lines_b[61].replace("if writer_type == reader_type:", "if reader_type == writer_type:")
            add_ctrl("CTRL-29B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric equality reader_type == writer_type")

        elif s["opaque_id"] == "MUT-31":
            # c7n/resources/sns.py:316
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            lines_a.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-31A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch in SNS loop")

            # local var rename r -> _sns_res (hard)
            lines_b = list(lines)
            lines_b[314] = lines_b[314].replace("for r in resources:", "for _sns_res in resources:")
            lines_b[316] = lines_b[316].replace("TopicArn=r['TopicArn']", "TopicArn=_sns_res['TopicArn']")
            add_ctrl("CTRL-31B", s, "".join(lines_b), "rename_var", "hard", "Local loop variable rename r -> _sns_res")

        elif s["opaque_id"] == "MUT-35":
            # c7n/resources/sns.py:310
            # if self.data.get('enabled', True):
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            lines_a.insert(l_idx, f"{indent}assert True\n")
            add_ctrl("CTRL-35A", s, "".join(lines_a), "redundant_assert", "easy", "Insert redundant assert True")

            lines_b = list(lines)
            # wrap key assignment in try/finally
            lines_b[310] = f"{indent}    try:\n{indent}        key = self.data.get('key', 'alias/aws/sns')\n{indent}    finally:\n{indent}        pass\n"
            add_ctrl("CTRL-35B", s, "".join(lines_b), "try_finally", "easy", "Wrap key assignment in try/finally: pass")

        elif s["opaque_id"] == "MUT-40":
            # redash/controllers.py:335
            # api.add_resource(...)
            l_idx = line - 1
            lines_a = list(lines)
            lines_a.insert(l_idx, "if False:\n    pass\n")
            add_ctrl("CTRL-40A", s, "".join(lines_a), "dead_branch", "easy", "Add module-level dead branch if False: pass")

            lines_b = list(lines)
            lines_b.insert(l_idx, "assert True\n")
            add_ctrl("CTRL-40B", s, "".join(lines_b), "redundant_assert", "easy", "Add redundant assert True")

        elif s["opaque_id"] == "MUT-41":
            # xdoctest/runner.py:125
            # line 109: n_total = len(enabled_examples)
            lines_a = list(lines)
            # rename local variable n_total -> _total_examples (hard)
            src_a = "".join(lines_a)
            src_a = src_a.replace("n_total = len(enabled_examples)", "_total_examples = len(enabled_examples)")
            src_a = src_a.replace("print('running %d test(s)' % n_total)", "print('running %d test(s)' % _total_examples)")
            src_a = src_a.replace("print('%d / %d passed'  % (n_passed, n_total))", "print('%d / %d passed'  % (n_passed, _total_examples))")
            src_a = src_a.replace("'n_total': n_total", "'n_total': _total_examples")
            add_ctrl("CTRL-41A", s, src_a, "rename_var", "hard", "Local variable rename n_total -> _total_examples")

            # line 126: if len(enabled_examples) == 1: -> if 1 == len(enabled_examples): (easy)
            lines_b = list(lines)
            lines_b[125] = lines_b[125].replace("if len(enabled_examples) == 1:", "if 1 == len(enabled_examples):")
            add_ctrl("CTRL-41B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric equality 1 == len(enabled_examples)")

        elif s["opaque_id"] == "MUT-42":
            # llama_stack/core/library_client.py:206
            l_idx = line - 1
            lines_a = list(lines)
            indent = "        "
            lines_a.insert(207, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-42A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch in __init__")

            lines_b = list(lines)
            lines_b.insert(207, f"{indent}assert True\n")
            add_ctrl("CTRL-42B", s, "".join(lines_b), "redundant_assert", "easy", "Insert redundant assert True in __init__")

        elif s["opaque_id"] == "MUT-44":
            # lib/rucio/daemons/reaper/reaper.py:347
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            lines_a.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-44A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch before space check")

            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}pass\n")
            add_ctrl("CTRL-44B", s, "".join(lines_b), "redundant_pass", "easy", "Add redundant pass before space check")

        elif s["opaque_id"] == "MUT-47":
            # redash/controllers.py:335
            l_idx = line - 1
            lines_a = list(lines)
            lines_a.insert(l_idx, "pass\n")
            add_ctrl("CTRL-47A", s, "".join(lines_a), "redundant_pass", "easy", "Add module-level redundant pass")

            lines_b = list(lines)
            lines_b.insert(l_idx, "if True:\n    pass\n")
            add_ctrl("CTRL-47B", s, "".join(lines_b), "redundant_branch", "easy", "Add module-level if True: pass")

        elif s["opaque_id"] == "MUT-48":
            # can/io/asc.py:409
            lines_a = list(lines)
            indent = "        "
            lines_a.insert(410, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-48A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch in ASCReader.__init__")

            lines_b = list(lines)
            lines_b.insert(410, f"{indent}assert True\n")
            add_ctrl("CTRL-48B", s, "".join(lines_b), "redundant_assert", "easy", "Add assert True in ASCReader.__init__")

        elif s["opaque_id"] == "MUT-49":
            # python/ct/crypto/asn1/types.py:793
            # if ord(character) > 127:
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("if ord(character) > 127:", "if 127 < ord(character):")
            add_ctrl("CTRL-49A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric comparison 127 < ord(character)")

            # rename local character -> _ch (hard)
            lines_b = list(lines)
            lines_b[791] = lines_b[791].replace("for index, character in enumerate(buf):", "for index, _ch in enumerate(buf):")
            lines_b[792] = lines_b[792].replace("ord(character)", "ord(_ch)")
            lines_b[794] = lines_b[794].replace("character, index", "_ch, index")
            add_ctrl("CTRL-49B", s, "".join(lines_b), "rename_var", "hard", "Local loop variable rename character -> _ch")

        elif s["opaque_id"] == "MUT-50":
            # quantecon/gridtools.py:237
            # if X[t, i] <= nodes[i][0]:
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("if X[t, i] <= nodes[i][0]:", "if nodes[i][0] >= X[t, i]:")
            add_ctrl("CTRL-50A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric boundary comparison nodes[i][0] >= X[t, i]")

            # line 239: elif X[t, i] >= nodes[i][-1]:
            lines_b = list(lines)
            lines_b[238] = lines_b[238].replace("elif X[t, i] >= nodes[i][-1]:", "elif nodes[i][-1] <= X[t, i]:")
            add_ctrl("CTRL-50B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric boundary comparison nodes[i][-1] <= X[t, i]")

        elif s["opaque_id"] == "MUT-53":
            # dynaconf/default_settings.py:37
            # if value is not None
            lines_a = list(lines)
            lines_a[37] = lines_a[37].replace("if value is not None", "if None is not value")
            add_ctrl("CTRL-53A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric identity comparison None is not value")

            indent = "    "
            lines_b = list(lines)
            lines_b.insert(35, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-53B", s, "".join(lines_b), "dead_branch", "easy", "Add dead branch in get helper")

        elif s["opaque_id"] == "MUT-54":
            # rotkehlchen/chain/substrate/types.py:52
            # return self.value == 0
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace("return self.value == 0", "return 0 == self.value")
            add_ctrl("CTRL-54A", s, "".join(lines_a), "symmetric_comparison", "easy", "Symmetric equality 0 == self.value")

            indent = "        "
            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}assert True\n")
            add_ctrl("CTRL-54B", s, "".join(lines_b), "redundant_assert", "easy", "Insert redundant assert True")

        elif s["opaque_id"] == "MUT-58":
            # llama_stack/core/library_client.py:217
            # nest_asyncio.apply()
            l_idx = line - 1
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a = list(lines)
            stmt = lines_a[l_idx].strip()
            lines_a[l_idx] = f"{indent}try:\n{indent}    {stmt}\n{indent}finally:\n{indent}    pass\n"
            add_ctrl("CTRL-58A", s, "".join(lines_a), "try_finally", "easy", "Wrap nest_asyncio.apply() in try/finally: pass")

            lines_b = list(lines)
            lines_b.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-58B", s, "".join(lines_b), "dead_branch", "easy", "Insert dead branch before nest_asyncio.apply()")

        elif s["opaque_id"] == "MUT-63":
            # marimo/_runtime/runtime.py:715
            # except Exception as e:
            #     LOGGER.warning("Failed to drop table %s: %s", name, str(e))
            # rename e -> _err (hard)
            lines_a = list(lines)
            lines_a[714] = lines_a[714].replace("except Exception as e:", "except Exception as _err:")
            lines_a[715] = lines_a[715].replace("str(e)", "str(_err)")
            add_ctrl("CTRL-63A", s, "".join(lines_a), "rename_var", "hard", "Local exception var rename e -> _err")
            indent = "                    "
            lines_b = list(lines)
            lines_b.insert(713, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-63B", s, "".join(lines_b), "dead_branch", "easy", "Add dead branch before duckdb.execute")

        elif s["opaque_id"] == "MUT-64":
            # ntfy/cli.py:350
            # if getattr(args, 'func', None) == run_cmd and 'hide_command' in config:
            l_idx = line - 1
            lines_a = list(lines)
            lines_a[l_idx] = lines_a[l_idx].replace(
                "if getattr(args, 'func', None) == run_cmd and 'hide_command' in config:",
                "if 'hide_command' in config and getattr(args, 'func', None) == run_cmd:"
            )
            add_ctrl("CTRL-64A", s, "".join(lines_a), "commute_and", "easy", "Commute and operands on config check")

            # hard: redundant guard simplification: lines 346 already checks args.func == run_cmd
            lines_b = list(lines)
            lines_b[l_idx] = lines_b[l_idx].replace("getattr(args, 'func', None) == run_cmd", "run_cmd == getattr(args, 'func', None)")
            add_ctrl("CTRL-64B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric comparison run_cmd == getattr(...)")

        elif s["opaque_id"] == "MUT-70":
            # theano/sandbox/rng_mrg.py:1137
            # if self.use_cuda and dtype == 'float32':
            l_idx = line - 1
            lines_a = list(lines)
            indent = lines[l_idx][: len(lines[l_idx]) - len(lines[l_idx].lstrip())]
            lines_a.insert(l_idx, f"{indent}if False:\n{indent}    pass\n")
            add_ctrl("CTRL-70A", s, "".join(lines_a), "dead_branch", "easy", "Add dead branch before CUDA check")

            lines_b = list(lines)
            lines_b[l_idx] = lines_b[l_idx].replace("dtype == 'float32'", "'float32' == dtype")
            add_ctrl("CTRL-70B", s, "".join(lines_b), "symmetric_comparison", "easy", "Symmetric equality 'float32' == dtype")

    print(f"Successfully generated {len(controls)} known-equivalent controls.")
    easy_count = sum(1 for c in controls if c["category"] == "easy")
    hard_count = sum(1 for c in controls if c["category"] == "hard")
    print(f"  Easy: {easy_count}, Hard: {hard_count}")

    # Now verify all controls on Modal to prove equivalence (reward == 1.0)
    print("\nProving behavioral invariance by running reference grader on Modal...")
    gate = CloudSpendGate(
        approval="Peter 2026-11-02 overnight FIX-A2B-SPECIFICITY: Modal cap $5.00",
        cap_usd=5.00,
        usd_per_execution=0.008,
        spend_ledger=Path.home() / ".local/state/evallab-failure-library/on-fix-a2b-specificity-ledger.json",
        modal_app_name="fix-a2b-specificity",
        billing_tag="fix-a2b-specificity",
    )
    meter = SandboxMeter(5.00)
    runner = modal_cell_runner(
        repo_root=REPO_ROOT,
        spend_gate=gate,
        meter=meter,
        timeout_seconds=180,
    )

    def run_control(c: dict[str, Any]) -> dict[str, Any]:
        tid = c["task_id"]
        pkg = Path(census[tid]["package_path"])
        ref_patch = (REPO_ROOT / census[tid]["reference_patch_path"]).read_text()
        workdir = "/testbed"

        script = (
            f"set -u\ncd {workdir} || exit 1\n"
            + _apply_reference(ref_patch)
            + _write(c["path"], c["new_source"])
            + f"echo EQUIV_CONTROL_APPLIED {c['control_id']}\n"
        )
        cell_dir = DURABLE_RECEIPTS / "cells" / c["control_id"]
        res = runner(cell_dir, pkg, script)
        c["reward"] = res["reward"]
        c["status_run"] = res["status"]
        c["wall_s"] = res["wall_s"]
        c["est_usd"] = res["est_usd"]
        return res
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_control, controls))
    failed = [c for c in controls if c.get("reward") != 1.0]
    if failed:
        print(f"ERROR: {len(failed)} controls failed verification! Rewards: {[f['reward'] for f in failed]}")
        sys.exit(1)

    print(f"\nALL {len(controls)} controls VERIFIED! 100% scored reward == 1.0 on task grader.")
    print(f"Total Modal spent: ${meter.spent:.4f} across {meter.cells} cells in {meter.seconds:.1f}s.")

    # Save controls artifacts
    # Strip full new_source before committing to PR repo rules
    clean_controls = []
    for c in controls:
        item = dict(c)
        del item["new_source"]
        clean_controls.append(item)

    for target_dir in [CERT_RECEIPTS, DURABLE_RECEIPTS, EXP_DIR]:
        (target_dir / "known_equivalent_controls.json").write_text(json.dumps(clean_controls, indent=2))

    print(f"Saved known_equivalent_controls.json to {EXP_DIR}, {CERT_RECEIPTS}, {DURABLE_RECEIPTS}.")


if __name__ == "__main__":
    main()
