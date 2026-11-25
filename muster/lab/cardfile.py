from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

TASKS = Path(__file__).resolve().parents[1] / "tasks"
_WIDTH = 160
_COLUMNS = 8
VERIFY_SCHEDULES = 2
VERIFY_WORST = 4
VERIFY_INSTANCES = 2
MAX_STALE_DISPLAY = 5


def canonical(card: dict) -> bytes:
    return (json.dumps(card, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _clip(value: Any, width: int) -> str:
    text = str(value).replace("\n", " ").replace("|", "/")
    return text if len(text) <= width else text[:width - 3] + "..."


def _blocks(conditions: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    block: list[str] = []
    width = len("| Policy |")
    for condition in conditions:
        extra = len(_clip(condition, 147)) + 3
        if block and (len(block) == _COLUMNS or width + extra > _WIDTH):
            blocks.append(block)
            block = []
            width = len("| Policy |")
        block.append(condition)
        width += extra
    if block:
        blocks.append(block)
    return blocks


def _matrix(card: dict) -> list[str]:
    lines: list[str] = []
    for block in _blocks(card["conditions"]):
        labels = [_clip(label, 147) for label in block]
        lines.extend(["| Policy | " + " | ".join(labels) + " |",
                      "| --- | " + " | ".join("---:" for _ in block) + " |"])
        for policy in card["policies"]:
            cells = []
            for condition in block:
                cell = card["matrix"][policy][condition]
                spread = "*" if cell["min"] != cell["max"] else ""
                mean = cell["mean"]
                text = f"{mean:.3f}" if abs(mean) < 1000 else f"{mean:.3g}"
                cells.append(text + spread)
            lines.append("| " + _clip(policy, 55) + " | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def _witness(row: dict) -> list[str]:
    witness = row["witness"]
    prefix = f"  - {witness['condition']}/{witness['instance']}: "
    order = ",".join(witness["order"])
    observed = f"observed={json.dumps(witness['observed'], sort_keys=True)}"
    if "expected" in witness:
        observed += f"; expected {witness['comparison']} {witness['expected']}"
    return [
        _clip(prefix + observed, _WIDTH),
        "    lineup: " + _clip(json.dumps(witness["lineup"], sort_keys=True), _WIDTH - 12),
        "    order: " + _clip(order, _WIDTH - 11),
    ]


def markdown(card: dict) -> str:
    lines = [f"# {_clip(card['family'], 100)} eval card", "", "Mean reward (* = varies):", ""]
    lines.extend(_matrix(card))
    lines.extend(["## Checks", ""])
    for row in [*card["claims"], *card["builtins"]]:
        label = f"- {row['status']}: {row['id']} ({row['episodes']} episodes)"
        if "note" in row:
            label += f" — {row['note']}"
        lines.append(_clip(label, _WIDTH))
        if "witness" in row:
            lines.extend(_witness(row))
    provenance = card["sources"]
    source = f"Source provenance: {provenance['status']}"
    if "note" in provenance:
        source += f" — {provenance['note']}"
    lines.extend(["", _clip(source, _WIDTH), "", "## Not checkable at build time", ""])
    lines.extend(f"- {item}" for item in card["not_checkable_at_build_time"])
    counts = card["summary"]
    lines.extend(["", f"Summary: {'PASS' if card['ok'] else 'FAIL'} — "
                  f"{counts['PASS']} PASS, {counts['FAIL']} FAIL, "
                  f"{counts['NOT_CHECKED']} NOT_CHECKED, {counts['episodes']} episodes."])
    assert all(len(line) <= _WIDTH for line in lines)
    return "\n".join(lines) + "\n"


def write(path: Path, card: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(card))


def compare(path: Path, card: dict) -> str | None:
    if not path.exists():
        return f"missing card: {path}"
    actual = canonical(card)
    saved = path.read_bytes()
    if saved == actual:
        return None if card["ok"] else "card matches but contains failed checks"
    try:
        old = json.loads(saved)
        if isinstance(old, dict):
            changed = sorted(key for key in old.keys() | card.keys()
                             if old.get(key) != card.get(key))
            return ("card differs: " + ", ".join(changed)) if changed else "card bytes differ"
    except (ValueError, UnicodeError):
        pass
    return "card differs: saved file is not a card object"


def _stale_sources(saved: dict, paths: dict[str, Path]) -> list[str]:
    stale = set(saved) - paths.keys()
    for name, path in paths.items():
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError:
            stale.add(name)
            continue
        if saved.get(name) != digest:
            stale.add(name)
    return sorted(stale)


def verify(path: Path, family: str, *, jobs: int) -> tuple[dict, int]:
    from muster.lab import card
    from muster.lab.finding import adapter_for
    from muster.lab.load import LabError

    result: dict = {"family": family, "ok": False, "stale": [], "episodes": 0, "reason": None}
    adapter = None
    load_error = None
    try:
        adapter = adapter_for(family)
    except LabError as error:
        load_error = str(error)
    if adapter is not None and not hasattr(adapter, "sources"):
        result["reason"] = "adapter has no sources hook"
        return result, 0
    try:
        saved = json.loads(path.read_bytes())
        assert isinstance(saved, dict), "saved file is not a card object"
        sources = saved["sources"]
        assert isinstance(sources, dict) and isinstance(sources.get("files"), dict), (
            "committed card has invalid sources"
        )
        files = sources["files"]
        paths = (card.source_paths(adapter) if adapter is not None else
                 {name: TASKS.parents[1] / name for name in files})
        result["stale"] = _stale_sources(files, paths)
        if result["stale"]:
            result["reason"] = "source files differ"
            return result, len(paths)
        assert adapter is not None, load_error
        assert sources.get("status") == "PASS" and files, "committed sources not checked"
        assert saved.get("ok") is True, "committed card is not ok"
        seed = saved["parameters"]["seed"]
        assert type(seed) is int, "invalid committed seed"
        reduced = card.build(adapter, schedules=VERIFY_SCHEDULES, worst=VERIFY_WORST,
                             instances=VERIFY_INSTANCES, seed=seed, jobs=jobs, family=family)
        result["episodes"] = reduced["summary"]["episodes"]
        current = reduced["sources"]["files"]
        result["stale"] = sorted(name for name in files.keys() | current.keys()
                                 if files.get(name) != current.get(name))
        if result["stale"]:
            result["reason"] = "source files differ"
            return result, len(current)
        for key in ("family", "conditions", "policies"):
            assert saved.get(key) == reduced[key], f"committed {key} differs"
        assert reduced["ok"], "reduced card FAIL"
        result["ok"] = True
        return result, len(current)
    except FileNotFoundError as error:
        result["reason"] = f"missing file: {error.filename or path}"
    except (LabError, AssertionError, OSError, ValueError, TypeError, KeyError) as error:
        result["reason"] = str(error) or "invalid card"
    return result, 0


def verification_text(result: dict, source_count: int) -> str:
    family = result["family"]
    if result["ok"]:
        return (f"{family}: committed card matches {source_count} source files; "
                f"reduced card PASS ({result['episodes']} episodes)")
    stale = result["stale"][:MAX_STALE_DISPLAY]
    changed = f"; changed or missing: {', '.join(stale)}" if stale else ""
    reason = str(result["reason"]).replace("\n", " ").replace("\r", " ")
    return f"{family}: {reason}{changed}; rebuild with: python -m muster card {family} --write"
