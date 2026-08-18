import json
from pathlib import Path

AGENT_OUTPUT = Path("/app/output/summary.json")
if not AGENT_OUTPUT.is_file():
    AGENT_OUTPUT = Path("output/summary.json")

LOG_DIR = Path("/logs/verifier")
if not LOG_DIR.exists():
    LOG_DIR = Path("logs/verifier")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENT_OUTPUT.is_file():
        passed = False
        message = "summary.json is missing"
    else:
        try:
            data = json.loads(AGENT_OUTPUT.read_text(encoding="utf-8"))
            passed = (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("total_records") == 3
                and data.get("status") == "ok"
            )
            message = "summary.json is valid" if passed else "summary.json content mismatch"
        except Exception as exc:
            passed = False
            message = f"error parsing json: {exc}"

    checks = {"correctness": {"passed": passed, "message": message}}
    rewards = {"reward": 1.0 if passed else 0.0}
    ctrf = {
        "report": {
            "summary": {
                "tests": 1,
                "passed": 1 if passed else 0,
                "failed": 0 if passed else 1,
            }
        }
    }

    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, indent=2) + "\n", encoding="utf-8")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": checks}))


if __name__ == "__main__":
    main()
