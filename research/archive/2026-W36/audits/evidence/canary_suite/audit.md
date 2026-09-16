# Audit Evidence: Canary Suite Paths vs library/tasks/ Truth

Handoff: `policy/canary-suite.yaml`
Subject: `src/evallab/canary.py`, `library/tasks/`

## 1. Test Suite Verification
Command: `uv run pytest tests/test_canary.py -v`
Output:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 9 items

tests/test_canary.py .........                                           [100%]

============================== 9 passed in 0.40s ===============================
```

## 2. Member Task Path & Directory Digest Verification
Command:
```python
from pathlib import Path
from evallab.canary import task_directory_digest, load_canary_suite

suite = load_canary_suite(Path("policy/canary-suite.yaml"))
for member in suite.members:
    p = Path(member.task_path)
    actual_digest = task_directory_digest(p)
    print(f"{member.name}: exists={p.exists()} matches={actual_digest == member.task_digest}")
    print(f"  claimed: {member.task_digest}")
    print(f"  actual:  {actual_digest}")
```
Output:
```
transaction-reconciliation: exists=True matches=True
  claimed: sha256:f2bb698dbcb990ce1be2a6319efc1c4264da4f7394637d33f103ebb053262820
  actual:  sha256:f2bb698dbcb990ce1be2a6319efc1c4264da4f7394637d33f103ebb053262820
terminal-bench-html-js-filter: exists=True matches=True
  claimed: sha256:36bef48eb1f5a2ed2211705ddd23dab1f98cb0158196e892fad8d3dd3a4aa956
  actual:  sha256:36bef48eb1f5a2ed2211705ddd23dab1f98cb0158196e892fad8d3dd3a4aa956
event-summary: exists=True matches=True
  claimed: sha256:bee722a27298eb06f5010b18da7c27295b1ff6236aa03ce58c5e5b1df4d0d61d
  actual:  sha256:bee722a27298eb06f5010b18da7c27295b1ff6236aa03ce58c5e5b1df4d0d61d
```

## Verdict
CONFIRMED.
All 3 members configured in `policy/canary-suite.yaml` exist on disk under `library/tasks/` and their recursive directory sha256 digests match byte-for-byte with the suite definition.
