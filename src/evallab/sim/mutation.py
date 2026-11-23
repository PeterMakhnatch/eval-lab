from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from random import Random

from evallab.sim.engine import STEP_LIMIT, Simulation
from evallab.verifier_mutation import (
    MAX_FILE_BYTES,
    Mutant,
    generate_mutants,
    language_of,
    select_mutants,
)

_FILE_COUNT = 6
_LANGUAGES = ("python", "c-family", "json")
_PY_NAMES = ("values", "items", "scores", "marks", "numbers", "readings")
_JS_NAMES = ("items", "values", "rows", "entries", "samples", "records")
_JSON_LABELS = ("ok", "check", "verifier")
_JSON_FLAGS = ("alpha", "beta", "gamma", "delta")


@dataclass(frozen=True, slots=True)
class Snippet:
    path: str
    oracle: bytes
    base: bytes | None


def _python_oracle(rng: Random, index: int) -> str:
    name = _PY_NAMES[index]
    threshold = rng.randrange(1, 20)
    equal_to = rng.randrange(1, 20)
    step = rng.randrange(1, 5)
    cap = rng.randrange(1, 30)
    floor = rng.randrange(0, 10)
    start = rng.choice(("True", "False"))
    lines = [
        f"def check_{index}({name}):",
        "    total = 0",
        f"    allowed = {start}",
        f"    for value in {name}:",
        f"        if value < {threshold} and allowed:",
        f"            total = total + {step}",
        f"        elif value == {equal_to}:",
        "            allowed = not allowed",
        "        else:",
        "            total = total - 1",
        f"    result = min(total, {cap})",
        f"    return result > {floor} or allowed",
        "",
    ]
    return "\n".join(lines)


def _js_oracle(rng: Random, index: int) -> str:
    name = _JS_NAMES[index]
    threshold = rng.randrange(1, 20)
    equal_to = rng.randrange(1, 20)
    step = rng.randrange(1, 5)
    cap = rng.randrange(1, 30)
    floor = rng.randrange(0, 10)
    start = rng.choice(("true", "false"))
    lines = [
        f"function check_{index}({name}) {{",
        "  let total = 0;",
        f"  const allowed = {start};",
        f"  for (const value of {name}) {{",
        f"    if (value < {threshold} && allowed) {{",
        f"      total = total + {step};",
        f"    }} else if (value === {equal_to}) {{",
        "      allowed = !allowed;",
        "    }",
        "  }",
        f"  const result = Math.min(total, {cap});",
        f"  return result > {floor} || allowed;",
        "}",
        "",
    ]
    return "\n".join(lines)


def _json_texts(rng: Random, index: int) -> tuple[str, str]:
    body = {
        "passed": rng.choice((True, False)),
        "count": rng.randrange(0, 20),
        "ratio": rng.choice((0.5, 1.5, 2.25)),
        "label": rng.choice(_JSON_LABELS),
        "flags": [rng.choice(_JSON_FLAGS), rng.choice(_JSON_FLAGS)],
        "limits": {"max": rng.randrange(1, 10)},
    }
    oracle = json.dumps({f"check_{index}": body}, indent=2) + "\n"
    changed = dict(body)
    changed["count"] = body["count"] + 1
    base = json.dumps({f"check_{index}": changed}, indent=2) + "\n"
    return oracle, base


def _snippet(rng: Random, index: int, language: str) -> Snippet:
    with_base = rng.random() < 0.5
    if language == "python":
        oracle, path = _python_oracle(rng, index), f"tests/check_{index}.py"
        base = oracle.replace(" and allowed:", " or allowed:", 1)
    elif language == "c-family":
        oracle, path = _js_oracle(rng, index), f"tests/check_{index}.js"
        base = oracle.replace("&& allowed", "|| allowed", 1)
    else:
        oracle, base = _json_texts(rng, index)
        path = f"tests/check_{index}.json"
    oracle_bytes = oracle.encode("utf-8")
    assert len(oracle_bytes) <= MAX_FILE_BYTES
    if not with_base:
        return Snippet(path, oracle_bytes, None)
    base_bytes = base.encode("utf-8")
    assert base_bytes != oracle_bytes
    assert len(base_bytes) <= MAX_FILE_BYTES
    return Snippet(path, oracle_bytes, base_bytes)


def generated_files(rng: Random) -> list[Snippet]:
    languages = list(_LANGUAGES) * 2
    rng.shuffle(languages)
    assert len(languages) == _FILE_COUNT
    snippets = [_snippet(rng, index, language) for index, language in enumerate(languages)]
    assert {language_of(snippet.path) for snippet in snippets} == set(_LANGUAGES)
    return snippets


def _expected_id(path: str, operator: str, mutated: bytes) -> str:
    digest = hashlib.sha256(f"{path}\0{operator}\0".encode() + mutated).hexdigest()
    return digest[:10]


def _compiles(mutated: bytes) -> bool:
    try:
        compile(mutated, "<mutant>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def _oversize_oracle() -> bytes:
    return b"#" * (MAX_FILE_BYTES + 1)


class MutationSimulation:
    def __init__(self, sim: Simulation) -> None:
        assert sim.step <= STEP_LIMIT
        self.sim = sim

    def run(self) -> None:
        snippets = generated_files(self.sim.rng)
        generated = self.generate(snippets)
        self.select(snippets, generated)

    def generate(self, snippets: list[Snippet]) -> list[list[tuple[Mutant, bytes]]]:
        sim = self.sim
        sim.begin(
            "verifier-mutation-generate",
            files=len(snippets),
            with_base=sum(1 for snippet in snippets if snippet.base is not None),
        )
        generated: list[list[tuple[Mutant, bytes]]] = []
        for snippet in snippets:
            mutants = generate_mutants(snippet.path, snippet.oracle, snippet.base)
            sim.note(
                file=snippet.path,
                mutants=len(mutants),
                operators=sorted({mutant.operator for mutant, _ in mutants}),
            )
            sim.require(len(mutants) > 0, "mutation_mutants_present")
            self.check_mutants(snippet, mutants)
            repeat = generate_mutants(snippet.path, snippet.oracle, snippet.base)
            sim.require(
                [mutant.id for mutant, _ in repeat] == [mutant.id for mutant, _ in mutants],
                "mutation_repeat_ids",
            )
            generated.append(mutants)
        sim.require(
            generate_mutants("verifier.txt", b"print(1)\n") == [],
            "mutation_unknown_language_empty",
        )
        sim.require(
            generate_mutants("verifier.py", _oversize_oracle()) == [],
            "mutation_oversize_empty",
        )
        return generated

    def check_mutants(self, snippet: Snippet, mutants: list[tuple[Mutant, bytes]]) -> None:
        sim = self.sim
        seen: set[str] = set()
        for mutant, mutated in mutants:
            sim.require(mutant.file == snippet.path, "mutation_file_identity")
            sim.require(mutated != snippet.oracle, "mutation_bytes_differ")
            sim.require(
                hashlib.sha256(mutated).hexdigest() == mutant.mutated_sha256,
                "mutation_sha256_matches",
            )
            sim.require(
                _expected_id(snippet.path, mutant.operator, mutated) == mutant.id,
                "mutation_id_matches",
            )
            if snippet.path.endswith(".py"):
                sim.require(_compiles(mutated), "mutation_python_compiles")
            sim.require(mutant.id not in seen, "mutation_ids_unique")
            seen.add(mutant.id)

    def select(self, snippets: list[Snippet], generated: list[list[tuple[Mutant, bytes]]]) -> None:
        sim = self.sim
        combined = [mutant for mutants in generated for mutant, _ in mutants]
        seed = f"verifier-mutation-{sim.seed}"
        sim.begin(
            "verifier-mutation-select",
            mutants=len(combined),
            files=len(snippets),
            limit=len(snippets),
        )
        for limit in (0, 1, 2, len(snippets), len(combined) + 3):
            chosen = select_mutants(combined, limit=limit, seed=seed)
            sim.note(limit=limit, chosen=len(chosen))
            sim.require(len(chosen) <= limit, "mutation_selection_within_limit")
            sim.require(len(chosen) == min(limit, len(combined)), "mutation_selection_fills")
            sim.require(
                len({mutant.id for mutant in chosen}) == len(chosen),
                "mutation_selection_unique",
            )
            if limit == 0:
                sim.require(chosen == [], "mutation_selection_empty_zero")
            repeat = select_mutants(combined, limit=limit, seed=seed)
            sim.require(
                [mutant.id for mutant in repeat] == [mutant.id for mutant in chosen],
                "mutation_selection_deterministic",
            )
        chosen = select_mutants(combined, limit=len(snippets), seed=seed)
        sim.require(
            {mutant.file for mutant in chosen} == {snippet.path for snippet in snippets},
            "mutation_selection_one_per_file",
        )
