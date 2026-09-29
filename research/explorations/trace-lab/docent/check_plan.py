
from docent import Docent

c = Docent()
cid = "69be1862-004b-43c2-bd49-e20688d3f965"
plans = c.list_reading_plans(cid)
print("plans:", len(plans))
for p in plans:
    print("-", p.get("name"), p.get("id"), p.get("created_at"))
    steps = (p.get("steps_json") or p.get("steps") or [])
    print("  steps:", len(steps) if isinstance(steps, list) else type(steps))
