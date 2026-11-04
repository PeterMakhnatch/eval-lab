"""Read-only lane audit. Unknown evidence never becomes a successful attestation."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evallab.insights.models import Recipe
from evallab.insights.recompute import run_recipe
from evallab.insights.stats import mcnemar_exact, wilson

NUMBER = re.compile(
    r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?(?![\w])"
)
PR = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)|(?:\bPR\s*)?#(\d+)\b", re.I)
PATH = re.compile(r"`([^`\n]+)`|\[[^\]]*\]\(([^)]+)\)")
ROUTE = re.compile(
    r"\b(openai-codex|anthropic|google-antigravity|deepseek|openrouter|meta)/([\w.*-]+)"
)
Command = Callable[[list[str], Path], tuple[int, str]]


def command(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def issue(kind: str, evidence: str, exact: Any, severity: str = "error") -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "evidence": evidence, "recomputation": exact}


def _tokens(line: str) -> list[dict[str, Any]]:
    # Code identifiers, paths, URLs, hashes and section ordinals are not measurements.
    clean = list(line)
    for match in re.finditer(r"`[^`]+`|https?://\S+|\b[0-9a-f]{7,64}\b|^\s*#+.*$|^\s*\d+\.", line):
        if match[0].startswith("`") and re.fullmatch(r"[`0-9,./% =()\[\]–−+\-]+", match[0]):
            continue
        clean[match.start() : match.end()] = " " * (match.end() - match.start())
    return [
        {"text": m.group(), "start": m.start(), "end": m.end()}
        for m in NUMBER.finditer("".join(clean))
    ]


def _value(text: str) -> float:
    return float(text.rstrip("%").replace(",", ""))


def _tolerance(text: str) -> float:
    value = text.rstrip("%").replace(",", "")
    if "e" in value.lower():
        return abs(float(value)) * 0.005
    return 0.55 * 10 ** (-len(value.partition(".")[2])) if "." in value else 0.0


def numbers(
    report: Path, recipes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims, discrepancies = [], []
    fenced = False
    used: set[int] = set()
    for lineno, line in enumerate(report.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        tokens = _tokens(line)
        if not tokens:
            continue
        evidence = f"{report}:{lineno}"
        claim: dict[str, Any] = {"evidence": evidence, "quote": line, "numbers": tokens}
        for token in tokens:
            token["status"] = "not_recomputable"
        for index, spec in enumerate(recipes):
            if spec.get("report", "RESULT.md") != report.name or spec.get("quote") != line:
                continue
            used.add(index)
            try:
                if type(spec.get("number")) is not int or spec["number"] < 0:
                    raise ValueError("number must be a nonnegative token index")
                token = tokens[spec["number"]]
                root = Path(spec.get("root", report.parent)).expanduser().resolve(strict=True)
                if "stat" in spec:
                    raw_counts = {
                        key: run_recipe(Recipe.model_validate(value), root)
                        for key, value in spec["inputs"].items()
                    }
                    if any(not value.is_integer() for value in raw_counts.values()):
                        raise ValueError("statistical inputs must be integer counts")
                    args = {key: int(value) for key, value in raw_counts.items()}
                    if spec["stat"] == "wilson":
                        result = wilson(**args)[spec["field"]]
                    elif spec["stat"] == "mcnemar_exact":
                        result = mcnemar_exact(**args)["p_value"]
                    else:
                        raise ValueError("unknown statistic")
                    if result is None:
                        raise ValueError("statistic has no estimate")
                else:
                    result = run_recipe(Recipe.model_validate(spec["recipe"]), root)
                result *= spec.get("scale", 1)
                if not math.isfinite(result):
                    raise ValueError("nonfinite recomputation")
                token.update(
                    actual=result,
                    recipe=spec,
                    status="reproduced"
                    if abs(result - _value(token["text"])) <= _tolerance(token["text"])
                    else "differs",
                )
                if token["status"] == "differs":
                    discrepancies.append(issue("number_differs", evidence, token.copy()))
            except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
                discrepancies.append(issue("recipe_invalid", evidence, str(exc)))
        unknown = [t for t in tokens if t["status"] == "not_recomputable"]
        if unknown:
            discrepancies.append(
                issue(
                    "numbers_unverified", evidence, {"quote": line, "numbers": unknown}, "warning"
                )
            )
        # Arithmetic is an additional check, never a replacement for receipt recomputation.
        plain = line.replace("**", "").replace(",", "")
        for match in re.finditer(r"\b(\d+)\s*/\s*(\d+)\s*(?:=|\()\s*(\d+(?:\.\d+)?)%", plain):
            k, n = int(match[1]), int(match[2])
            if n and abs(100 * k / n - float(match[3])) > _tolerance(match[3]):
                discrepancies.append(
                    issue(
                        "arithmetic_differs",
                        evidence,
                        {"quote": match[0], "actual_percent": 100 * k / n},
                    )
                )
        claims.append(claim)
    for index, spec in enumerate(recipes):
        if spec.get("report", "RESULT.md") == report.name and index not in used:
            discrepancies.append(issue("recipe_quote_missing", str(report), spec))
    return claims, discrepancies


def _expand(value: str) -> list[str]:
    brace = re.search(r"\{([^{}]+)\}", value)
    if not brace:
        return [value]
    return [
        expanded
        for part in brace[1].split(",")
        for expanded in _expand(value[: brace.start()] + part + value[brace.end() :])
    ]


def claim_time(report: Path, citation: str, line: str) -> tuple[float, str]:
    """Prefer a directly dated claim, then a matching STATUS citation, then report mtime."""
    candidates = [(line, str(report))]
    status = report.parent / "STATUS.md"
    if status.is_file():
        candidates.extend(
            (text, f"{status}:{n}")
            for n, text in enumerate(status.read_text().splitlines(), 1)
            if citation in text
        )
    day = datetime.fromtimestamp(report.stat().st_mtime, ZoneInfo("America/New_York"))
    for text, source in candidates:
        clock = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)\s*ET\b", text, re.I)
        if clock:
            date = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
            year, month, date_day = (
                map(int, date.groups()) if date else (day.year, day.month, day.day)
            )
            hour = int(clock[1]) % 12 + (12 if clock[4].upper() == "PM" else 0)
            try:
                stamp = day.replace(
                    year=year,
                    month=month,
                    day=date_day,
                    hour=hour,
                    minute=int(clock[2]),
                    second=int(clock[3] or 0),
                    microsecond=0,
                )
            except ValueError:
                continue
            return stamp.timestamp(), source + (" (report-date assumption)" if not date else "")
    return report.stat().st_mtime, str(report) + " mtime"


def artifacts(report: Path, roots: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records, discrepancies = [], []
    for lineno, line in enumerate(report.read_text().splitlines(), 1):
        for match in PATH.finditer(line):
            citation = match[1] or match[2]
            if ROUTE.fullmatch(citation) or re.fullmatch(r"[\d,]+/[\d,]+", citation):
                continue
            if citation.startswith(("http:", "https:")) or not re.search(
                r"(?:^[/~]|/$|[*{]|\.(?:jsonl?|md|csv|log|txt|py|sh|yaml|yml|parquet)\b)", citation
            ):
                continue
            if " " in citation or "(" in citation or "→" in citation:
                continue
            evidence = f"{report}:{lineno}"
            for value in _expand(citation):
                value = re.sub(r":\d+(?:-\d+)?$", "", value)
                path = Path(value).expanduser()
                patterns = [path] if path.is_absolute() else [root / path for root in roots]
                claimed_at, time_basis = claim_time(report, citation, line)
                matches = sorted(
                    {Path(p).resolve() for pattern in patterns for p in glob.glob(str(pattern))}
                )
                record: dict[str, Any] = {"evidence": evidence, "citation": value, "paths": []}
                if "<" in value or "..." in value:
                    record["status"] = "pattern_unresolved"
                    discrepancies.append(issue("artifact_unresolved", evidence, value, "warning"))
                elif not matches:
                    record["status"] = "missing"
                    discrepancies.append(
                        issue(
                            "artifact_missing",
                            evidence,
                            {"citation": value, "searched": list(map(str, patterns))},
                        )
                    )
                else:
                    record["status"] = "exists_now"
                    if len(matches) > 1 and not glob.has_magic(value):
                        discrepancies.append(
                            issue(
                                "artifact_ambiguous",
                                evidence,
                                {"citation": value, "candidates": list(map(str, matches))},
                                "warning",
                            )
                        )
                    for found in matches:
                        mtime = found.stat().st_mtime
                        row = {
                            "path": str(found),
                            "mtime": mtime,
                            "sha256": digest(found) if found.is_file() else None,
                            "claim_time": claimed_at,
                            "claim_time_basis": time_basis,
                            "chronology": "mtime_before_report_not_historical_proof",
                        }
                        if mtime > claimed_at:
                            row["chronology"] = "modified_after_report"
                            discrepancies.append(
                                issue("artifact_newer_than_claim", evidence, row.copy(), "warning")
                            )
                        record["paths"].append(row)
                records.append(record)
    return records, discrepancies


def sessions(lane: Path, allowed: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records, discrepancies = [], []
    for path in sorted(lane.rglob("*.jsonl")):
        if path.is_symlink():
            discrepancies.append(
                issue("session_symlink_unverified", str(path), "not followed", "warning")
            )
            continue
        counts: Counter[str] = Counter()
        fallbacks = 0
        is_session = False
        invalid: list[int] = []
        with path.open(errors="replace") as stream:
            for lineno, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except ValueError:
                    invalid.append(lineno)
                    continue
                if not isinstance(row, dict):
                    continue
                is_session |= row.get("type") == "session"
                message = row.get("message", {})
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                is_session = True
                provider, model = message.get("provider"), message.get("model")
                route = f"{provider}/{model}" if provider and model else "unknown"
                counts[route] += 1
                fallbacks += int(bool(message.get("resolvedModelIsFallback")))
        if not is_session:
            if invalid and any("session" in part for part in path.parts):
                discrepancies.append(issue("session_unreadable", str(path), invalid))
            continue
        record = {
            "path": str(path),
            "sha256": digest(path),
            "turns": dict(counts),
            "fallback_turns": fallbacks,
            "invalid_lines": invalid,
        }
        records.append(record)
        if invalid:
            discrepancies.append(issue("session_invalid_json", str(path), invalid))
        if not counts:
            discrepancies.append(
                issue("session_no_served_turns", str(path), "no assistant records", "warning")
            )
        for route, count in counts.items():
            if route == "unknown":
                discrepancies.append(issue("served_route_unknown", str(path), {route: count}))
            elif route.split("/")[0] not in {"openai-codex", "google-antigravity", "anthropic"}:
                discrepancies.append(
                    issue("paid_route", str(path), {"route": route, "turns": count})
                )
            if allowed and not any(
                route == a or (a.endswith("/*") and route.startswith(a[:-1])) for a in allowed
            ):
                discrepancies.append(
                    issue(
                        "off_route", str(path), {"route": route, "turns": count, "allowed": allowed}
                    )
                )
        if fallbacks:
            discrepancies.append(issue("fallback_turns", str(path), fallbacks))
    return records, discrepancies


def pull_requests(
    reports: list[Path], repo: Path, repository: str, run: Command, out: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records, discrepancies = [], []
    cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for report in reports:
        lines = report.read_text().splitlines()
        for lineno, line in enumerate(lines, 1):
            references = {(m[1] or repository, m[2] or m[3]) for m in PR.finditer(line)}
            seen: set[tuple[str, str]] = set()
            for match in PR.finditer(line):
                owner, number = match[1] or repository, match[2] or match[3]
                key = (owner, number)
                if key in seen:
                    continue
                seen.add(key)
                evidence = f"{report}:{lineno}"
                if key not in cache:
                    argv = [
                        "gh",
                        "pr",
                        "view",
                        number,
                        "--repo",
                        owner,
                        "--json",
                        "number,state,headRefOid,baseRefName,mergeCommit,mergedAt,statusCheckRollup,url",
                    ]
                    code, text = run(argv, repo)
                    receipt = out / f"pr-{owner.replace('/', '-')}-{number}.json"
                    receipt.write_text(
                        json.dumps({"command": argv, "exit_code": code, "output": text}, indent=2)
                        + "\n"
                    )
                    try:
                        data = json.loads(text) if code == 0 else None
                        if not isinstance(data, dict):
                            data = None
                    except ValueError:
                        data = None
                    cache[key] = data
                    records.append(
                        {
                            "repository": owner,
                            "number": number,
                            "receipt": str(receipt),
                            "observed": data,
                        }
                    )
                data = cache[key]
                if data is None:
                    discrepancies.append(
                        issue("pr_unverified", evidence, {"repository": owner, "number": number})
                    )
                    continue
                following = []
                for text in lines[lineno : lineno + 3]:
                    if PR.search(text):
                        break
                    following.append(text)
                context = " ".join([line, *following])
                if len(references) > 1:
                    discrepancies.append(
                        issue(
                            "pr_claim_ambiguous",
                            evidence,
                            {"quote": line, "references": sorted(references)},
                            "warning",
                        )
                    )
                    continue
                # Limit state claims to the citation line; following prose may discuss another PR.
                claimed = re.search(r"\b(merged|unmerged|open|closed)\b", line, re.I)
                negated = bool(
                    claimed
                    and (
                        claimed[1].lower() == "unmerged"
                        or re.search(r"\bnot\s+(?:yet\s+)?$", line[: claimed.start()], re.I)
                    )
                )
                expected = (
                    "MERGED"
                    if claimed and claimed[1].lower() == "unmerged"
                    else (claimed[1].upper() if claimed else None)
                )
                differs = (
                    (data.get("state") == expected) if negated else (data.get("state") != expected)
                )
                if claimed and differs:
                    discrepancies.append(
                        issue(
                            "pr_state_differs",
                            evidence,
                            {"claimed": claimed[1], "actual": data.get("state"), "number": number},
                        )
                    )
                head = re.search(
                    r"\b(?:exact\s+)?head(?:\s+SHA)?[\s:`*]+([0-9a-f]{7,40})\b", context, re.I
                )
                if head and not str(data.get("headRefOid", "")).startswith(head[1]):
                    discrepancies.append(
                        issue(
                            "pr_head_differs",
                            evidence,
                            {"claimed": head[1], "actual": data.get("headRefOid")},
                        )
                    )
                if re.search(
                    r"(?:checks|CI).*pass|(?:green|passed).*\b(?:checks|CI)\b", context, re.I
                ):
                    checks = data.get("statusCheckRollup") or []
                    names = {c.get("name") for c in checks}
                    bad = [
                        c
                        for c in checks
                        if c.get("conclusion", c.get("state")) != "SUCCESS"
                        or c.get("status", "COMPLETED") != "COMPLETED"
                    ]
                    if not {"quality-required", "typecheck-required"} <= names or bad:
                        discrepancies.append(
                            issue(
                                "pr_ci_not_green",
                                evidence,
                                {"head": data.get("headRefOid"), "checks": checks},
                            )
                        )
                if (
                    claimed
                    and not negated
                    and claimed[1].lower() == "merged"
                    and data.get("state") == "MERGED"
                ):
                    merge = (data.get("mergeCommit") or {}).get("oid")
                    stated_merge = re.search(
                        r"\bmerge commit[\s:`*]+([0-9a-f]{7,40})\b"
                        r"|\bmerged(?:\s+to\s+`?main`?)?\s+(?:as|at)[\s:`*]+([0-9a-f]{7,40})\b",
                        context,
                        re.I,
                    )
                    if stated_merge and not str(merge or "").startswith(
                        stated_merge[1] or stated_merge[2]
                    ):
                        discrepancies.append(
                            issue(
                                "pr_merge_differs",
                                evidence,
                                {"claimed": stated_merge[1] or stated_merge[2], "actual": merge},
                            )
                        )
                    argv = ["git", "merge-base", "--is-ancestor", merge or "missing", "origin/main"]
                    code, text = run(argv, repo)
                    if code != 0:
                        discrepancies.append(
                            issue(
                                "merge_not_on_origin_main",
                                evidence,
                                {"command": argv, "exit_code": code, "output": text},
                            )
                        )
    return records, discrepancies


def verify(
    lane: Path,
    out: Path,
    repo: Path,
    *,
    recipes: list[dict[str, Any]] | None = None,
    roots: list[Path] | None = None,
    allowed: list[str] | None = None,
    repository: str = "PeterMakhnatch/eval-lab",
    run: Command = command,
) -> dict[str, Any]:
    if recipes is not None and (
        not isinstance(recipes, list) or any(not isinstance(row, dict) for row in recipes)
    ):
        raise ValueError("claims must be a JSON array of recipe objects")
    lane, out, repo = lane.resolve(strict=True), out.resolve(), repo.resolve(strict=True)
    if not lane.is_dir() or out.is_relative_to(lane):
        raise ValueError("output must be outside the checked lane directory")
    if out.exists() and any(out.iterdir()):
        raise ValueError("output already contains files; choose a fresh directory")
    out.mkdir(parents=True, exist_ok=True)
    reports = [lane / name for name in ("RESULT.md", "EARLY.md") if (lane / name).is_file()]
    discrepancies: list[dict[str, Any]] = []
    if not reports:
        discrepancies.append(
            issue("report_missing", str(lane), "neither RESULT.md nor EARLY.md exists")
        )
    claims, paths = [], []
    for report in reports:
        rows, errors = numbers(report, recipes or [])
        claims.extend(rows)
        discrepancies.extend(errors)
        rows, errors = artifacts(report, [lane, repo, *(roots or [])])
        paths.extend(rows)
        discrepancies.extend(errors)
    session_rows, errors = sessions(lane, allowed or [])
    discrepancies.extend(errors)
    if not session_rows:
        discrepancies.append(
            issue(
                "session_evidence_missing",
                str(lane),
                "no recorded session turns to attest",
                "warning",
            )
        )
    if not allowed:
        discrepancies.append(
            issue(
                "route_policy_unverified",
                str(lane),
                "supply --allowed-route; report route mentions are not a policy",
                "warning",
            )
        )
    report_text = "\n".join(p.read_text() for p in reports)
    totals: Counter[str] = Counter()
    for session in session_rows:
        totals.update(session["turns"])
    for lineno, line in enumerate(report_text.splitlines(), 1):
        for provider in ("anthropic", "deepseek", "openrouter", "meta"):
            if not re.search(rf"\b0\s+`?{provider}/\*", line):
                continue
            actual = sum(n for route, n in totals.items() if route.startswith(provider + "/"))
            if actual:
                discrepancies.append(
                    issue(
                        "provider_claim_scope_unverified",
                        f"{lane} report line {lineno}",
                        {
                            "quote": line,
                            "provider": provider,
                            "claimed": 0,
                            "all_session_turns": actual,
                            "reason": "a worker or time-scoped zero cannot be compared with whole-lane totals",
                        },
                        "warning",
                    )
                )
    code, text = run(["git", "fetch", "origin", "main"], repo)
    (out / "fetch.json").write_text(
        json.dumps(
            {"command": ["git", "fetch", "origin", "main"], "exit_code": code, "output": text},
            indent=2,
        )
        + "\n"
    )
    if code:
        discrepancies.append(issue("origin_main_unverified", str(repo), text))
    prs, errors = pull_requests(reports, repo, repository, run, out)
    discrepancies.extend(errors)
    result = {
        "schema_version": 1,
        "lane": str(lane),
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "discrepancies"
        if any(d["severity"] == "error" for d in discrepancies)
        else "incomplete"
        if discrepancies
        else "verified",
        "reports": [
            {"path": str(p), "sha256": digest(p), "mtime": p.stat().st_mtime} for p in reports
        ],
        "status_source": {"path": str(lane / "STATUS.md"), "sha256": digest(lane / "STATUS.md")}
        if (lane / "STATUS.md").is_file()
        else None,
        "implementation_sha256": digest(Path(__file__)),
        "claims": claims,
        "artifacts": paths,
        "sessions": session_rows,
        "served_turns": dict(totals),
        "pull_requests": prs,
        "discrepancies": discrepancies,
        "limits": [
            "mtime before a report does not prove historical existence; later mtime means changed, not necessarily absent",
            "unmapped prose numbers remain unverified; supply quote-bound receipt recipes",
            "current GitHub state does not prove past CI or PR state",
            "session records attest recorded assistant messages, not provider billing",
        ],
    }
    (out / "VERIFY.json").write_text(json.dumps(result, indent=2) + "\n")
    md = [
        f"# Lane verification: {lane.name}",
        "",
        f"Status: **{result['status']}**",
        "",
        "## Served assistant messages",
        "",
        *[f"- `{r}`: {n}" for r, n in sorted(totals.items())],
        "",
        "## Discrepancies",
        "",
    ]
    for index, row in enumerate(discrepancies, 1):
        md.extend(
            [
                f"### V{index}: {row['kind']} ({row['severity']})",
                f"Evidence: `{row['evidence']}`",
                "",
                "```json",
                json.dumps(row["recomputation"], indent=2),
                "```",
                "",
            ]
        )
    md.extend(["## Limits", "", *[f"- {s}" for s in result["limits"]], ""])
    (out / "VERIFY.md").write_text("\n".join(md))
    return result


def _cli(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        recipes = json.loads(args.claims.read_text()) if args.claims else []
        result = verify(
            args.lane_dir,
            args.output_dir,
            args.repo,
            recipes=recipes,
            roots=args.receipt_root,
            allowed=args.allowed_route,
            repository=args.repository,
        )
    except (ValueError, OSError) as exc:
        print(f"verify-lane: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(args.output_dir.resolve()),
                "discrepancies": len(result["discrepancies"]),
                "served_turns": result["served_turns"],
            }
        )
    )
    return 0 if result["status"] == "verified" else 1


def build_verify_lane_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "verify-lane",
        help="Recompute lane claims and attest artifacts, PRs and nested sessions ($0)",
    )
    parser.add_argument("lane_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--repository", default="PeterMakhnatch/eval-lab")
    parser.add_argument(
        "--claims", type=Path, help="JSON array of verbatim quotes with numeric receipt recipes"
    )
    parser.add_argument("--receipt-root", type=Path, action="append", default=[])
    parser.add_argument("--allowed-route", action="append", default=[])
    parser.set_defaults(func=_cli)
