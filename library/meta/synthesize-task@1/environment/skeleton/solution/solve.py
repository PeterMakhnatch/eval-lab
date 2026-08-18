import json
from pathlib import Path

input_file = Path("/app/input/data.json")
if not input_file.is_file():
    input_file = Path("environment/data.json")

data = json.loads(input_file.read_text(encoding="utf-8"))

summary = {
    "schema_version": 1,
    "total_records": len(data),
    "status": "ok",
}

output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)

output_file = output_dir / "summary.json"
output_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
