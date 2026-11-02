"""Preserve stored Stage-2 prompts, readings, outputs, and sessions for Cohort A."""
import hashlib
import json
import shutil
from pathlib import Path

DEST = Path.home() / "Developer/eval-lab-results/2026-11-02/D8/stored"
DEST.mkdir(parents=True, exist_ok=True)
(DEST / "prompts").mkdir(exist_ok=True)
(DEST / "sessions").mkdir(exist_ok=True)
(DEST / "out").mkdir(exist_ok=True)
(DEST / "readings").mkdir(exist_ok=True)

BENCH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
COHORT_PATH = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2/cohort.jsonl")
BRIDGE31 = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight")

def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in open(p) if line.strip()]

manifest = {r["case_id"]: r for r in read_jsonl(BENCH / "manifest.jsonl")}
cohort = read_jsonl(COHORT_PATH)

tw_rows = {r["run_id"]: r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-AUTOREVIEW-V2-TW/receipts/readings-tw.jsonl")}
cb_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-cheatbench.jsonl")}
gf_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-goodfire.jsonl")}
ds_rows = {r.get("blind_id") or r.get("run_id"): r for r in read_jsonl(BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts/readings-arm2.jsonl")}

stored_map = {"tw": tw_rows, "cb": cb_rows, "gf": gf_rows, "ds": ds_rows}

s2_cases = []
for c in cohort:
    cid = c["case_id"]
    fam = c["family"]
    sid = manifest[cid]["source_id"]
    bid = manifest[cid]["blind_id"]
    st_row = None
    if fam == "terminal_wrench":
        st_row = stored_map["tw"].get(sid)
    elif fam == "cheatbench":
        st_row = stored_map["cb"].get(bid) or stored_map["cb"].get(sid)
    elif fam == "goodfire":
        st_row = stored_map["gf"].get(bid) or stored_map["gf"].get(sid)
    elif fam == "deep_swe":
        st_row = stored_map["ds"].get(bid) or stored_map["ds"].get(sid)
    if st_row and st_row.get("stage") == 2:
        s2_cases.append({
            "case_id": cid,
            "family": fam,
            "split": c["split"],
            "source_id": sid,
            "blind_id": bid,
            "stored_row": st_row,
        })

print(f"Loaded {len(s2_cases)} cohort A cases.")

readings_srcs = [
    BRIDGE31 / "g38-flash/raw/W3-AUTOREVIEW-V2-TW/receipts/readings-tw.jsonl",
    BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-cheatbench.jsonl",
    BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts/readings-goodfire.jsonl",
    BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts/readings-arm2.jsonl",
    Path("/tmp/w3ar-g38/readings-tw.jsonl"),
    Path("/tmp/w3ar-g38/readings-cheatbench-p2.jsonl"),
    Path("/tmp/w3deepswe/readings-arm2.jsonl"),
]
for rsrc in readings_srcs:
    if rsrc.exists():
        shutil.copy2(rsrc, DEST / "readings" / rsrc.name)

case_map = {}
for item in s2_cases:
    cid = item["case_id"]
    fam = item["family"]
    sid = item["source_id"]
    bid = item["blind_id"]

    p_src = None
    if fam == "terminal_wrench":
        p_src = Path(f"/tmp/w3ar-g38/prompts/s2-tw-{sid}.txt")
    elif fam == "cheatbench":
        p_src = Path(f"/tmp/w3ar-g38/prompts/s2-cb-p2-{bid}.txt")
    elif fam == "goodfire":
        p_src = Path(f"/tmp/w2ar-g38/prompts/s2-goodfire-{bid}.txt")
    elif fam == "deep_swe":
        p_src = Path(f"/tmp/w3deepswe/prompts/arm2-s2-{bid}.txt")

    if p_src and p_src.exists():
        p_dst = DEST / "prompts" / f"stage2-stored-{cid}.txt"
        shutil.copy2(p_src, p_dst)
        item["stored_prompt_path"] = str(p_dst)
        item["stored_prompt_src"] = str(p_src)
    else:
        print(f"WARNING: missing prompt for {cid} ({fam}) from {p_src}")

    out_src = None
    if fam == "terminal_wrench":
        matches = list(Path("/tmp/w3ar-g38/out").glob(f"*{sid}*.json"))
        if matches:
            out_src = matches[0]
    elif fam == "cheatbench":
        matches = list(Path("/tmp/w3ar-g38/out").glob(f"*{bid}*.json"))
        if matches:
            out_src = matches[0]
    elif fam == "goodfire":
        matches = list(Path("/tmp/w2ar-g38/out").glob(f"*{bid}*.json"))
        if matches:
            out_src = matches[0]
    elif fam == "deep_swe":
        matches = list(Path("/tmp/w3deepswe/out").glob(f"*{bid}*.json"))
        if matches:
            out_src = matches[0]

    if out_src and out_src.exists():
        out_dst = DEST / "out" / f"stage2-stored-{cid}.json"
        shutil.copy2(out_src, out_dst)
        item["stored_out_path"] = str(out_dst)

    sess_src = None
    sess_dirs = [
        Path(f"/tmp/w3ar-g38/sessions/s2-tw-{sid}"),
        Path(f"/tmp/w3ar-g38/sessions/s2-cb-p2-{bid}"),
        Path(f"/tmp/w2ar-g38/sessions/s2-goodfire-{bid}"),
        Path(f"/tmp/w3deepswe/sessions/arm2-s2-{bid}"),
    ]
    for sdir in sess_dirs:
        if sdir.exists() and sdir.is_dir():
            sess_src = sdir
            break
    if sess_src:
        sess_dst = DEST / "sessions" / f"stored-{cid}"
        if sess_dst.exists():
            shutil.rmtree(sess_dst)
        shutil.copytree(sess_src, sess_dst)
        item["stored_session_dir"] = str(sess_dst)

    case_map[cid] = item

print(f"Copied prompts for {sum(1 for c in case_map.values() if 'stored_prompt_path' in c)} / 48 cases.")
print(f"Copied out files for {sum(1 for c in case_map.values() if 'stored_out_path' in c)} / 48 cases.")
print(f"Copied session dirs for {sum(1 for c in case_map.values() if 'stored_session_dir' in c)} / 48 cases.")

(DEST / "cohort_a_stored_map.json").write_text(json.dumps(case_map, indent=2))

manifest_lines = []
for p in sorted(DEST.rglob("*")):
    if p.is_file() and p.name != "manifest-sha256.txt":
        rel = p.relative_to(DEST)
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        manifest_lines.append(f"{sha}  {rel}")

(DEST / "manifest-sha256.txt").write_text("\n".join(manifest_lines) + "\n")
print(f"Wrote manifest-sha256.txt with {len(manifest_lines)} entries.")
