"""Grader-owned child process: drives the submitted engine and records formula evaluations.

Runs as an unprivileged user in a scratch package that holds the grader's own
formula.py and the submitted engine.py. Reads one JSON request on stdin and
writes one JSON response to the original stdout, after a sentinel line.
"""

import json
import os
import sys

SENTINEL = "@@SHEET-RECALC-RESULT@@"


def main() -> None:
    request = json.loads(sys.stdin.read())
    sentinel = SENTINEL + request.pop("nonce")
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # anything the engine prints goes to stderr
    sys.stdout = sys.stderr

    sys.path.insert(0, request["root"])
    from sheet import formula

    log: list[str] = []
    original = formula.evaluate

    def counted(cell, parsed, lookup):
        log.append(cell)
        return original(cell, parsed, lookup)

    formula.evaluate = counted
    from sheet.engine import Sheet

    sheet = Sheet()
    results = []
    for op in request["ops"]:
        start = len(log)
        error = None
        try:
            if op["op"] == "set":
                sheet.set(op["cell"], op["text"])
            elif op["op"] == "set_many":
                sheet.set_many(dict(op["edits"]))
            elif op["op"] != "probe":
                raise AssertionError(f"unknown op {op['op']}")
        except ValueError:
            error = "ValueError"
        except Exception as exc:  # noqa: BLE001 - reported to the grader as a failure
            error = f"{type(exc).__name__}: {exc}"[:300]
        evals = log[start:]
        before_get = len(log)
        values, texts = {}, {}
        for cell in op.get("probe", ()):
            try:
                v = sheet.get(cell)
                values[cell] = v if isinstance(v, str) else float(v)
            except Exception as exc:  # noqa: BLE001
                values[cell] = f"<raised {type(exc).__name__}>"
        for cell in op.get("probe_text", ()):
            try:
                texts[cell] = sheet.text(cell)
            except Exception as exc:  # noqa: BLE001
                texts[cell] = f"<raised {type(exc).__name__}>"
        results.append(
            {
                "error": error,
                "evals": evals,
                "get_evals": len(log) - before_get,
                "values": values,
                "texts": texts,
            }
        )

    out.write(sentinel + "\n" + json.dumps(results) + "\n")


if __name__ == "__main__":
    main()
