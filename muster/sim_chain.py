from __future__ import annotations

import json
from pathlib import Path
from random import Random
from typing import Any

from muster import events, load, regrade
from muster.sim_fixture import Run

KEY = "sim-world-hmac-key"
SEATS = ("p1", "p2", "p3")
KINDS = ("open", "msg", "msgs", "claim", "rules")


def _events(rng: Random, count: int) -> list[dict[str, Any]]:
    assert 2 <= count <= 64
    return [
        {
            "seq": index,
            "t": round(1000.0 + index * rng.uniform(0.1, 2.0), 3),
            "seat": rng.choice(SEATS),
            "kind": rng.choice(KINDS),
            "method": rng.choice(("GET", "POST")),
            "ok": rng.random() < 0.8,
            "status": rng.choice((200, 400, 404, 409)),
            "request": {"box": rng.randrange(30)} if rng.random() < 0.5 else None,
            "response": {"text": f"note-{rng.randrange(999)}", "unicode": "\u00e9\u2713"},
        }
        for index in range(1, count + 1)
    ]


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = []
    for row in rows:
        line = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert json.loads(line) == row
        lines.append(line)
    return lines


def _verify(
    path: Path, lines: list[str], key: str | None = None, head: str | None = None
) -> tuple[bool, str | None]:
    path.write_bytes(("".join(line + "\n" for line in lines)).encode("utf-8"))
    return regrade.verify_event_chain(path, key, expected_head=head)


def _mutate(rng: Random, lines: list[str]) -> list[str]:
    index = rng.randrange(len(lines))
    line = lines[index]
    position = rng.randrange(len(line))
    replacement = rng.choice([c for c in "abcdef0123456789xyz{}[]:," if c != line[position]])
    return [
        *lines[:index],
        line[:position] + replacement + line[position + 1 :],
        *lines[index + 1 :],
    ]


def check_chain(run: Run, rng: Random, root: Path) -> None:
    path = root / "chain-events.jsonl"
    plain = _events(rng, rng.randint(3, 24))
    run.require("chain/v1", "legacy-log-is-accepted", _verify(path, _lines(plain)) == (True, None))
    run.require(
        "chain/v1",
        "legacy-log-has-no-chain-status",
        regrade.check_event_chain("\n".join(_lines(plain)))[0] is None,
    )
    sealed = events.chain(plain)
    signed = events.chain(plain, KEY)
    lines, signed_lines = _lines(sealed), _lines(signed)
    run.require("chain/v2", "clean-chain-verifies", _verify(path, lines) == (True, None))
    run.require(
        "chain/v2", "clean-signed-chain-verifies", _verify(path, signed_lines, KEY) == (True, None)
    )
    run.require(
        "chain/v2",
        "kit-rows-are-canonical",
        all(
            line
            == json.dumps(
                json.loads(line), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            for line in signed_lines
        ),
    )
    ok, problem = _verify(path, signed_lines, KEY + "x")
    run.require(
        "chain/hmac", "wrong-key-is-rejected", not ok and "hmac" in (problem or ""), str(problem)
    )
    ok, _ = _verify(path, lines, KEY)
    run.require("chain/hmac", "missing-hmac-is-rejected-when-key-given", not ok)
    _check_unicode_separators(run, path)
    _tamper(run, rng, path, sealed, signed)


def _check_unicode_separators(run: Run, path: Path) -> None:
    sample = "line\u2028sep\u2029para\u0085nel"
    rows = [
        {"seq": 1, "t": 1.0, "seat": "p1", "kind": "msg", "response": {"text": sample}},
        {"seq": 2, "t": 2.0, "seat": "p2", "kind": "msgs", "response": {"text": "ok"}},
    ]
    signed_lines = _lines(events.chain(rows, KEY))
    for ending, tag in (("\n", "lf"), ("\r\n", "crlf")):
        path.write_bytes(("".join(line + ending for line in signed_lines)).encode("utf-8"))
        ok, problem = regrade.verify_event_chain(path, KEY)
        parsed, error = load.read_events(path)
        run.require(
            "chain/u2028",
            f"unicode-separators-preserved-{tag}",
            ok
            and problem is None
            and error is None
            and parsed is not None
            and len(parsed) == 2
            and parsed[0]["response"]["text"] == sample,
            f"verify={(ok, problem)} load={error}",
        )


def _tamper(run: Run, rng: Random, path: Path, sealed: list[dict], signed: list[dict]) -> None:
    index = rng.randrange(len(sealed))
    changed = [dict(row) for row in sealed]
    changed[index]["response"] = {"text": "forged"}
    ok, problem = _verify(path, _lines(changed))
    run.require(
        "chain/tamper",
        "changed-payload-is-flagged-at-its-line",
        not ok and (problem or "").startswith(f"line {index + 1} "),
        str(problem),
    )
    resealed = [dict(row) for row in signed]
    resealed[index]["response"] = {"text": "forged"}
    resealed[index]["entry_hash"] = events.entry_hash(resealed[index])
    ok, _ = _verify(path, _lines(resealed), KEY)
    run.require("chain/tamper", "rehashed-forgery-fails-hmac", not ok)
    middle = rng.randrange(len(sealed) - 1)
    ok, problem = _verify(path, _lines([*sealed[:middle], *sealed[middle + 1 :]]))
    run.require(
        "chain/delete",
        "deleted-line-is-flagged",
        not ok and (problem or "").startswith(f"line {middle + 1} "),
        str(problem),
    )
    swapped = list(sealed)
    swapped[middle], swapped[middle + 1] = swapped[middle + 1], swapped[middle]
    ok, _ = _verify(path, _lines(swapped))
    run.require("chain/reorder", "swapped-lines-are-flagged", not ok)
    head = sealed[-1]["entry_hash"]
    run.require(
        "chain/truncate",
        "tail-cut-without-anchor-is-a-valid-prefix",
        _verify(path, _lines(sealed[:-1])) == (True, None),
    )
    ok, _ = _verify(path, _lines(sealed[:-1]), head=head)
    run.require("chain/truncate", "tail-cut-is-flagged-against-recorded-head", not ok)
    lines = _lines(sealed)
    torn = [*lines[:-1], lines[-1][: rng.randrange(1, len(lines[-1]))]]
    ok, problem = _verify(path, torn)
    run.require("chain/truncate", "torn-last-line-is-flagged", not ok, str(problem))
    for _ in range(4):
        ok, problem = _verify(path, _mutate(rng, _lines(signed)), KEY)
        run.require("chain/mutate", "any-one-byte-change-is-flagged", not ok, str(problem))
    path.write_bytes(b"\xff\n")
    run.require(
        "chain/encoding", "non-utf8-log-is-flagged", not regrade.verify_event_chain(path)[0]
    )
    stripped = [
        {k: v for k, v in row.items() if k not in ("prev_hash", "entry_hash", "hmac")}
        for row in signed
    ]
    ok_key, _ = _verify(path, _lines(stripped), KEY)
    ok_head, _ = _verify(path, _lines(stripped), head=head)
    ok_empty_key, _ = _verify(path, [], KEY)
    ok_empty_head, _ = _verify(path, [], head=head)
    run.require(
        "chain/downgrade",
        "stripped-or-empty-log-fails-when-key-or-head-expected",
        not ok_key and not ok_head and not ok_empty_key and not ok_empty_head,
    )
