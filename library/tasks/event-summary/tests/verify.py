import json
import math
from collections import Counter
from pathlib import Path

TRUSTED_INPUT = Path("/tests/fixtures/events.jsonl")
AGENT_INPUT = Path("/app/input/events.jsonl")
AGENT_OUTPUT = Path("/app/output/summary.json")
LOG_DIR = Path("/logs/verifier")
EXPECTED_KEYS = {
    "schema_version",
    "total_events",
    "counts",
    "total_duration_ms",
    "p95_duration_ms",
}


def expected_summary() -> dict[str, object]:
    events = [json.loads(line) for line in TRUSTED_INPUT.read_text().splitlines() if line.strip()]
    durations = sorted(event["duration_ms"] for event in events)
    counts = Counter(event["kind"] for event in events)
    return {
        "schema_version": 1,
        "total_events": len(events),
        "counts": {name: counts[name] for name in sorted(counts)},
        "total_duration_ms": sum(durations),
        "p95_duration_ms": durations[math.ceil(0.95 * len(durations)) - 1],
    }


def load_candidate() -> tuple[object | None, str | None]:
    if not AGENT_OUTPUT.is_file():
        return None, "summary.json is missing"
    try:
        return json.loads(AGENT_OUTPUT.read_text()), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"summary.json is not valid UTF-8 JSON: {type(exc).__name__}"


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    candidate, parse_error = load_candidate()
    preservation = AGENT_INPUT.is_file() and AGENT_INPUT.read_bytes() == TRUSTED_INPUT.read_bytes()
    schema = isinstance(candidate, dict) and set(candidate) == EXPECTED_KEYS
    correctness = schema and candidate == expected_summary()
    single_output = AGENT_OUTPUT.parent.is_dir() and sorted(
        path.name for path in AGENT_OUTPUT.parent.iterdir()
    ) == ["summary.json"]
    checks = {
        "input_preservation": {
            "passed": preservation,
            "message": "input bytes match trusted fixture" if preservation else "input changed",
        },
        "schema": {
            "passed": schema,
            "message": parse_error or "exact output keys present",
        },
        "correctness": {
            "passed": correctness,
            "message": "summary matches independent computation"
            if correctness
            else "wrong summary",
        },
        "output_hygiene": {
            "passed": single_output,
            "message": "summary.json is the only output"
            if single_output
            else "extra/missing output",
        },
    }
    overall = all(check["passed"] for check in checks.values())
    rewards = {
        "reward": float(overall),
        "correctness": float(correctness),
        "input_preservation": float(preservation),
        "output_hygiene": float(single_output),
    }
    ctrf_tests = [
        {
            "name": name,
            "status": "passed" if check["passed"] else "failed",
            "duration": 0,
            "message": check["message"],
        }
        for name, check in checks.items()
    ]
    ctrf = {
        "results": {
            "tool": {"name": "event-summary-verifier"},
            "summary": {
                "tests": len(ctrf_tests),
                "passed": sum(test["status"] == "passed" for test in ctrf_tests),
                "failed": sum(test["status"] == "failed" for test in ctrf_tests),
                "skipped": 0,
                "pending": 0,
                "other": 0,
                "start": 0,
                "stop": 0,
            },
            "tests": ctrf_tests,
        }
    }
    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2, sort_keys=True) + "\n")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, sort_keys=True) + "\n")
    print(json.dumps({"passed": overall, "checks": checks}, sort_keys=True))


if __name__ == "__main__":
    main()
