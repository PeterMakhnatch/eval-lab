import json
from pathlib import Path

TRUSTED_INPUT = Path("/tests/fixtures/orders.jsonl")
EXPECTED = Path("/tests/fixtures/expected.jsonl")
AGENT_INPUT = Path("/app/data/orders.jsonl")
AGENT_OUTPUT = Path("/app/output/result.jsonl")
LOG_DIR = Path("/logs/verifier")


def load_jsonl(path: Path) -> tuple[list[dict] | None, str | None]:
    if not path.is_file():
        return None, f"file missing: {path}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = []
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                return None, f"line {idx + 1} is not a JSON object"
            rows.append(obj)
        return rows, None
    except Exception as exc:
        return None, f"failed to parse JSONL: {exc}"


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    expected_rows, _ = load_jsonl(EXPECTED)
    candidate_rows, parse_error = load_jsonl(AGENT_OUTPUT)

    preservation = (
        AGENT_INPUT.is_file() and AGENT_INPUT.read_bytes() == TRUSTED_INPUT.read_bytes()
    )
    parsed = candidate_rows is not None and parse_error is None
    correctness = parsed and candidate_rows == expected_rows
    single_output = (
        AGENT_OUTPUT.parent.is_dir()
        and sorted(p.name for p in AGENT_OUTPUT.parent.iterdir()) == ["result.jsonl"]
    )

    checks = {
        "input_preservation": {
            "passed": preservation,
            "message": "input bytes match trusted fixture" if preservation else "input changed",
        },
        "parse": {
            "passed": parsed,
            "message": "valid JSONL output" if parsed else (parse_error or "parse failed"),
        },
        "correctness": {
            "passed": correctness,
            "message": (
                "output matches expected rows exactly"
                if correctness
                else "wrong output rows"
            ),
        },
        "output_hygiene": {
            "passed": single_output,
            "message": (
                "result.jsonl is the only output"
                if single_output
                else "extra/missing output"
            ),
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
            "tool": {"name": "seqgen-s7-000-verifier"},
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
