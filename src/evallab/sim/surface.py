from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Any

from evallab.package_anatomy import (
    instruction_cues,
    instruction_mismatch,
    reference_patch_surface,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_LIST_CAP = 12
_SENTENCE_CAP = 200
_TOKEN_LIMIT = 1000
_CROWDED_HUNKS = 7
_CONSONANTS = "bcdfghjkmnpqrvwxz"
_KINDS = ("function", "flag", "file", "class", "name")
_WEIGHTS = {"function": 3, "flag": 3, "file": 2, "class": 2, "name": 1}
_CONSTRAINT_BONUS = 2
_FUNCTION_STYLES = ("snake", "camel", "call", "qualified")
_FILE_STYLES = ("flat", "nested", "dotfile")
_DOTFILE_STEM = "rc"
_DOTFILE_SUFFIX = "example"
_FLAG_SUFFIXES = ("on", "only", "strict", "never")
_EXTENSIONS = ("py", "json", "toml", "yaml", "csv", "sh")
_CONSTRAINT_WORDS = (
    "must",
    "MUST",
    "should",
    "shall",
    "raises",
    "rejects",
    "error",
    "invalid",
    "exactly",
    "at least",
    "at most",
    "never",
    "only",
    "exits non-zero",
    "exit with",
    "returns",
)
_DECOY = "Set `true`, `None` and `ab` in the guide."
_FILLER = " then again" * 24
_BRANCH_KEYWORDS = (
    "if",
    "elif",
    "else if",
    "except",
    "raise",
    "throw",
    "case",
    "when",
    "return Err",
    "panic!",
)
_DEF_STYLES = {
    "def": "def {}(value):",
    "async": "async def {}(value):",
    "class": "class {}:",
    "function": "function {}(value) {{",
    "arrow": "export const {} = (value) => {{",
    "go": "func (r *Store) {}(value int) {{",
    "rust": "pub fn {}(value: i32) {{",
}
_STYLE_NAMES = tuple(_DEF_STYLES)
_SOURCE_PATHS = ("src/{}.py", "lib/{}.js", "pkg/{}.go", "crates/{}.rs", "app/{}_util.py")
_TEST_PATHS = (
    "tests/{}.py",
    "pkg/test_{}.py",
    "pkg/{}_test.go",
    "web/{}.test.ts",
    "app/{}/conftest.py",
    "spec/{}.rb",
    "src/__tests__/{}.js",
)


def _flag(rng: Random) -> bool:
    return rng.random() < 0.5


class _Names:
    def __init__(self, rng: Random) -> None:
        self.rng = rng
        self.count = 0

    def token(self, prefix: str) -> str:
        assert len(prefix) == 1 and prefix in _CONSONANTS
        self.count += 1
        assert self.count < _TOKEN_LIMIT
        letters = "".join(self.rng.choice(_CONSONANTS) for _ in range(3))
        token = f"{prefix}{letters}{self.count:03d}"
        assert len(token) == 7 and token.isalnum() and token.islower()
        return token


@dataclass(frozen=True, slots=True)
class _Cue:
    kind: str
    style: str
    extension: str
    token: str
    constraint_word: str | None
    present: bool
    repeat: bool
    long: bool
    suffix: str
    variant: bool

    @property
    def symbol(self) -> str:
        if self.kind == "function":
            return {
                "snake": f"{self.token}_go",
                "camel": f"go{self.token.capitalize()}",
                "call": self.token,
                "qualified": f"{self.token}_go",
            }[self.style]
        if self.kind == "flag":
            return f"--{self.token}-{self.suffix}"
        if self.kind == "file":
            if self.style == "dotfile":
                name = f".{self.token}{_DOTFILE_STEM}"
                return f"{name}.{_DOTFILE_SUFFIX}" if self.variant else name
            return f"data/{self.reference}" if self.style == "nested" else self.reference
        return self.token.capitalize() if self.kind == "class" else self.token

    @property
    def reference(self) -> str:
        if self.kind == "file" and self.style != "dotfile":
            return f"{self.token}.{self.extension}"
        return self.symbol

    @property
    def probe(self) -> str | None:
        if self.kind == "flag" and self.variant:
            return f"{self.symbol}=1" if self.present else f"{self.symbol}-extra"
        return self.reference if self.present else None

    @property
    def mention(self) -> str:
        if self.kind == "file" and self.style == "dotfile":
            return f"`{self.symbol}`"
        if self.kind in ("flag", "file"):
            return self.symbol
        if self.style == "call":
            return f"{self.symbol}()"
        if self.style == "qualified":
            return f"`mod.{self.symbol}`"
        return f"`{self.symbol}`"

    @property
    def sentence(self) -> str:
        tail = _FILLER if self.long else ""
        if self.constraint_word is None:
            return f"See {self.mention} in the guide{tail}."
        return f"The tool {self.constraint_word} use {self.mention}{tail}."

    @property
    def weight(self) -> int:
        return _WEIGHTS[self.kind] + _CONSTRAINT_BONUS * (self.constraint_word is not None)

    @property
    def plan(self) -> str:
        shape = f"{self.kind}:{self.style}"
        if self.style == "dotfile":
            shape += ":suffixed" if self.variant else ":bare"
        return f"{shape}:{self.weight}"

    @property
    def expected(self) -> dict[str, Any]:
        constrained = self.constraint_word is not None
        sentence = self.sentence[:_SENTENCE_CAP] if constrained else ""
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "constraint": constrained,
            "sentence": sentence,
        }


@dataclass(frozen=True, slots=True)
class _Instruction:
    cues: tuple[_Cue, ...]
    decoy: bool
    newline: bool

    @property
    def text(self) -> str:
        sentences = [_DECOY] if self.decoy else []
        for cue in self.cues:
            if cue.repeat:
                sentences.append(f"Also see {cue.mention} below.")
            sentences.append(cue.sentence)
        return ("\n" if self.newline else " ").join(sentences) + "\n"


@dataclass(frozen=True, slots=True)
class _Branch:
    keyword: str
    var: str
    other: str
    literal: bool
    referenced: bool

    @property
    def text(self) -> str:
        if self.literal:
            return f'{self.keyword} {self.var} == "{self.other}":'
        return f"{self.keyword} {self.var} is {self.other}:"

    @property
    def reference(self) -> str:
        return self.other if self.literal else self.var


@dataclass(frozen=True, slots=True)
class _Hunk:
    owner: str | None
    owner_style: str
    owner_present: bool
    pre_edits: int
    removed_branch: bool
    pre_branch: _Branch | None
    new_def: str | None
    def_style: str
    def_present: bool
    post_branch: _Branch | None

    @property
    def owner_edits(self) -> int:
        return self.pre_edits + int(self.removed_branch) + int(self.pre_branch is not None)

    @property
    def branches(self) -> tuple[_Branch, ...]:
        return tuple(b for b in (self.pre_branch, self.post_branch) if b is not None)


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    test_like: bool
    deleted: bool
    hunks: tuple[_Hunk, ...]

    @property
    def counted(self) -> bool:
        return not self.test_like and not self.deleted


def _cue(rng: Random, names: _Names) -> _Cue:
    kind = rng.choice(_KINDS)
    styles = {"function": _FUNCTION_STYLES, "file": _FILE_STYLES}.get(kind, ("plain",))
    cue = _Cue(
        kind=kind,
        style=rng.choice(styles),
        extension=rng.choice(_EXTENSIONS),
        token=names.token("k"),
        constraint_word=rng.choice(_CONSTRAINT_WORDS) if _flag(rng) else None,
        present=_flag(rng),
        repeat=_flag(rng),
        long=rng.random() < 0.25,
        suffix=rng.choice(_FLAG_SUFFIXES),
        variant=_flag(rng),
    )
    assert cue.kind in _WEIGHTS and len(cue.symbol) >= 3
    assert len(cue.sentence) > _SENTENCE_CAP or not cue.long
    return cue


def _instruction(rng: Random, names: _Names) -> _Instruction:
    count = rng.randint(1, 5)
    cues = tuple(_cue(rng, names) for _ in range(count))
    plan = _Instruction(cues, _flag(rng), _flag(rng))
    assert len({cue.symbol for cue in plan.cues}) == count
    return plan


def _branch(rng: Random, names: _Names, referenced: bool) -> _Branch:
    return _Branch(
        keyword=rng.choice(_BRANCH_KEYWORDS),
        var=names.token("v"),
        other=names.token("q"),
        literal=_flag(rng),
        referenced=referenced,
    )


def _hunk(rng: Random, names: _Names, crowded: bool) -> _Hunk:
    owner = names.token("r") if crowded or _flag(rng) else None
    new_def = names.token("d") if crowded or _flag(rng) else None
    pre_branch = _branch(rng, names, not crowded and _flag(rng)) if crowded or _flag(rng) else None
    post = new_def is not None and (crowded or _flag(rng))
    hunk = _Hunk(
        owner=owner,
        owner_style=rng.choice(_STYLE_NAMES),
        owner_present=not crowded and _flag(rng),
        pre_edits=rng.randint(1 if crowded else 0, 3),
        removed_branch=_flag(rng),
        pre_branch=pre_branch,
        new_def=new_def,
        def_style=rng.choice(_STYLE_NAMES),
        def_present=not crowded and _flag(rng),
        post_branch=_branch(rng, names, not crowded and _flag(rng)) if post else None,
    )
    assert hunk.post_branch is None or hunk.new_def is not None
    assert not crowded or (hunk.owner_edits > 0 and len(hunk.branches) == 2)
    return hunk


def _file(rng: Random, names: _Names, crowded: bool) -> _File:
    test_like = not crowded and _flag(rng)
    deleted = not crowded and _flag(rng)
    template = rng.choice(_TEST_PATHS if test_like else _SOURCE_PATHS)
    count = _CROWDED_HUNKS if crowded else rng.randint(1, 3)
    hunks = tuple(_hunk(rng, names, crowded) for _ in range(count))
    return _File(template.format(names.token("p")), test_like, deleted, hunks)


def _files(rng: Random, names: _Names) -> tuple[_File, ...]:
    files = [_file(rng, names, False) for _ in range(rng.randint(1, 3))]
    if _flag(rng):
        files.insert(rng.randint(0, len(files)), _file(rng, names, True))
    assert 1 <= len(files) <= 4
    return tuple(files)


def _render_hunk(lines: list[str], numbers: dict[_Branch, int], hunk: _Hunk, start: int) -> None:
    owner = "" if hunk.owner is None else " " + _DEF_STYLES[hunk.owner_style].format(hunk.owner)
    lines.append(f"@@ -{start},8 +{start},8 @@{owner}")
    lines.append("     anchor = value")
    for index in range(hunk.pre_edits):
        lines.append("-    count = 0" if index % 2 == 0 else "+    count = count + 1")
    if hunk.removed_branch:
        lines.append("-    if stale:")
    if hunk.pre_branch is not None:
        lines.append(f"+    {hunk.pre_branch.text}")
        numbers[hunk.pre_branch] = len(lines)
    if hunk.new_def is not None:
        lines.append("+" + _DEF_STYLES[hunk.def_style].format(hunk.new_def))
        lines.append("+    total = value")
    if hunk.post_branch is not None:
        lines.append(f"+    {hunk.post_branch.text}")
        numbers[hunk.post_branch] = len(lines)
    lines.append("     anchor = value")


def _render_patch(files: tuple[_File, ...]) -> tuple[str, dict[_Branch, int]]:
    lines: list[str] = []
    numbers: dict[_Branch, int] = {}
    for file in files:
        lines.append(f"diff --git a/{file.path} b/{file.path}")
        if file.deleted:
            lines.append("deleted file mode 100644")
        lines.append(f"--- a/{file.path}")
        lines.append("+++ /dev/null" if file.deleted else f"+++ b/{file.path}")
        for index, hunk in enumerate(file.hunks):
            _render_hunk(lines, numbers, hunk, 1 + 20 * index)
    assert len(numbers) == sum(len(h.branches) for f in files for h in f.hunks)
    return "\n".join(lines) + "\n", numbers


def _tests_text(instruction: _Instruction, files: tuple[_File, ...]) -> str:
    refs = [cue.probe for cue in instruction.cues if cue.probe is not None]
    for hunk in (hunk for file in files for hunk in file.hunks):
        if hunk.owner is not None and hunk.owner_present:
            refs.append(hunk.owner)
        if hunk.new_def is not None and hunk.def_present:
            refs.append(hunk.new_def)
        refs.extend(branch.reference for branch in hunk.branches if branch.referenced)
    body = "".join(f'    probe("{ref}")\n' for ref in refs) or "    probe()\n"
    return "def test_generated():\n" + body


def _surface_rows(files: tuple[_File, ...]) -> list[tuple[dict[str, Any], bool]]:
    rows: list[tuple[dict[str, Any], bool]] = []
    for file in (file for file in files if file.counted):
        for hunk in file.hunks:
            if hunk.owner is not None and hunk.owner_edits > 0:
                row = {"symbol": hunk.owner, "file": file.path, "edited_lines": hunk.owner_edits}
                rows.append(({**row, "defined_in_patch": False}, hunk.owner_present))
            if hunk.new_def is not None:
                row = {"symbol": hunk.new_def, "file": file.path, "edited_lines": 1}
                rows.append(({**row, "defined_in_patch": True}, hunk.def_present))
    return rows


def _branch_rows(
    files: tuple[_File, ...], numbers: dict[_Branch, int]
) -> list[tuple[dict[str, Any], bool]]:
    rows: list[tuple[dict[str, Any], bool]] = []
    for file in (file for file in files if file.counted):
        for branch in (branch for hunk in file.hunks for branch in hunk.branches):
            row = {
                "file": file.path,
                "patch_line": numbers[branch],
                "text": branch.text,
                "literals": [branch.other] if branch.literal else [],
                "idents": [branch.var, branch.other],
            }
            rows.append((row, branch.referenced))
    return rows


def _expected_surface(files: tuple[_File, ...], numbers: dict[_Branch, int]) -> dict[str, Any]:
    symbols = _surface_rows(files)
    branches = _branch_rows(files, numbers)
    absent = [row for row, present in symbols if not present]
    absent.sort(key=lambda row: (-row["edited_lines"], row["symbol"]))
    unreferenced = [row for row, referenced in branches if not referenced]
    return {
        "changed_symbols": len(symbols),
        "exercised_symbols": len(symbols) - len(absent),
        "unexercised": absent[:_LIST_CAP],
        "unexercised_count": len(absent),
        "branch_lines": len(branches),
        "branches_unreferenced": unreferenced[:_LIST_CAP],
        "branches_unreferenced_count": len(unreferenced),
    }


class SurfaceSimulation:
    def __init__(self, sim: Simulation) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.rng = sim.rng
        self.start_step = sim.step

    def run(self) -> None:
        names = _Names(self.rng)
        instruction = _instruction(self.rng, names)
        files = _files(self.rng, names)
        tests = _tests_text(instruction, files)
        patch, numbers = _render_patch(files)
        self._check_cues(instruction)
        self._check_mismatch(instruction, tests)
        self._check_surface(files, patch, numbers, tests)
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "surface_step_budget")

    def _check_cues(self, instruction: _Instruction) -> None:
        sim = self.sim
        expected = [cue.expected for cue in instruction.cues]
        sim.begin(
            "surface-cues",
            cues=[cue.plan for cue in instruction.cues],
            decoy=instruction.decoy,
            newline=instruction.newline,
        )
        result = instruction_cues(instruction.text)
        sim.require(len(result) == len(expected), "surface_cue_count")
        for got, want in zip(result, expected, strict=False):
            sim.require(set(got) == set(want), "surface_cue_fields")
            sim.require(got["symbol"] == want["symbol"], "surface_cue_symbol")
            sim.require(got["kind"] == want["kind"], "surface_cue_kind")
            sim.require(got["constraint"] == want["constraint"], "surface_cue_constraint")
            sim.require(got["sentence"] == want["sentence"], "surface_cue_sentence")
        sim.require(result == expected, "surface_cues")

    def _check_mismatch(self, instruction: _Instruction, tests: str) -> None:
        sim = self.sim
        expected = [{**cue.expected, "weight": cue.weight} for cue in instruction.cues if not cue.present]
        expected.sort(key=lambda row: (-row["weight"], row["symbol"]))
        sim.begin(
            "surface-mismatch",
            absent=len(expected),
            weights=[f"{row['kind']}:{row['weight']}" for row in expected],
        )
        result = instruction_mismatch(instruction.text, tests)
        sim.require(
            [row["symbol"] for row in result] == [row["symbol"] for row in expected],
            "surface_mismatch_symbols",
        )
        for got, want in zip(result, expected, strict=False):
            sim.require(got["weight"] == want["weight"], "surface_mismatch_weight")
        sim.require(result == expected, "surface_mismatch")

    def _check_surface(
        self, files: tuple[_File, ...], patch: str, numbers: dict[_Branch, int], tests: str
    ) -> None:
        sim = self.sim
        expected = _expected_surface(files, numbers)
        sim.begin(
            "surface-patch",
            files=[f"{int(f.test_like)}{int(f.deleted)}:{len(f.hunks)}" for f in files],
            changed=expected["changed_symbols"],
            branches=expected["branch_lines"],
        )
        result = reference_patch_surface(patch, tests)
        sim.require(set(result) == set(expected), "surface_patch_fields")
        for field in expected:
            sim.require(result[field] == expected[field], f"surface_{field}")
        for field in ("unexercised", "branches_unreferenced"):
            sim.require(
                len(result[field]) == min(expected[f"{field}_count"], _LIST_CAP),
                f"surface_{field}_cap",
            )
