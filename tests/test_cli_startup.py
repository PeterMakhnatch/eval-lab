"""``evallab qa-data`` answers like the full CLI without loading it; package names resolve lazily."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

PROBE = """
import sys
from {module} import main
try:
    main({argv!r})
except SystemExit as stop:
    code = stop.code
else:
    code = 0
sys.stderr.write(json.dumps({{"code": code, "full_cli": "evallab.cli" in sys.modules,
                              "workers": "evallab.production.seeker_worker" in sys.modules}}))
"""


def run(module: str, argv: list[str]) -> tuple[str, dict[str, object]]:
    done = subprocess.run(
        [sys.executable, "-c", "import json\n" + PROBE.format(module=module, argv=argv)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    stderr = done.stderr.strip()
    state = json.loads(stderr[stderr.rindex("{"):])
    return done.stdout, state


@pytest.mark.parametrize("argv", [
    ["qa-data", "split", "task", "no-such-task"],
    ["qa-data", "task", "--help"],
    ["qa-data", "history"],  # usage error
])
def test_qa_data_replies_exactly_as_the_full_cli_without_loading_it(argv: list[str]) -> None:
    fast_out, fast = run("evallab.entry", argv)
    full_out, full = run("evallab.cli", argv)
    def steady(text: str) -> str:
        return re.sub(r'"latency_ms":[0-9.]+', '"latency_ms":0', text)

    assert steady(fast_out) == steady(full_out) and fast["code"] == full["code"]
    assert fast["full_cli"] is False and fast["workers"] is False
    assert full["full_cli"] is True


def test_other_commands_still_reach_the_full_cli() -> None:
    out, state = run("evallab.entry", ["--version"])
    assert out.strip() and state["full_cli"] is True


def test_production_names_resolve_on_first_use() -> None:
    import evallab.production as production
    from evallab.production.corpus_queue import CorpusRegistry

    assert production.CorpusRegistry is CorpusRegistry
    assert set(production.__all__) <= set(dir(production))
    with pytest.raises(AttributeError):
        production.no_such_name  # noqa: B018


def test_importing_one_production_module_does_not_load_the_workers() -> None:
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys, evallab.production.qa_data; print('evallab.production.seeker_worker' in sys.modules)"],
        capture_output=True, text=True, check=True, timeout=120,
    )
    assert done.stdout.strip() == "False"
