#!/bin/bash
# Oracle reference solution for format-code-task-002391 (Auditor dedup).
# Runs in the agent environment; stdout goes to the trial's agent/oracle.txt log.
set -u
cd /workspace/repo

echo "=== separate-verifier agent-env probe ==="
echo "--- ls /tests:"
ls -la /tests 2>&1
echo "--- marker grep (test_audit_collapses_two_records_and_merges_aliases) over workspace:"
grep -rl "test_audit_collapses_two_records_and_merges_aliases" "/workspace/repo" /tmp \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv \
  --exclude-dir=node_modules 2>/dev/null
echo "grep_rc=$?"

echo "=== oracle fix: Auditor collapses duplicate advisories ==="
python3 - <<'PYEOF'
from pathlib import Path

path = Path("/workspace/repo/pip_audit/_audit.py")
src = path.read_text(encoding="utf-8")

old_tail = """        else:
            yield from self._service.query_all(specs)
"""
new_tail = """        else:
            for dep, vulns in self._service.query_all(specs):
                yield dep, _deduplicate_vulnerabilities(vulns)


def _deduplicate_vulnerabilities(
    vulns: list[VulnerabilityResult],
) -> list[VulnerabilityResult]:
    \"\"\"Collapse duplicate records for the same advisory into one result.

    Two records belong together when their identifier sets (primary id plus
    aliases) intersect. The emitted record keeps the PYSEC result when the
    group has one (regardless of provider order), else the first record, and
    merges the full alias set of the group.
    \"\"\"
    groups: list[list[VulnerabilityResult]] = []
    for vuln in vulns:
        idents = {vuln.id} | set(vuln.aliases)
        target: list[VulnerabilityResult] | None = None
        for group in groups:
            known: set[str] = set()
            for member in group:
                known.add(member.id)
                known.update(member.aliases)
            if idents & known:
                target = group
                break
        if target is None:
            groups.append([vuln])
        else:
            target.append(vuln)
    deduped: list[VulnerabilityResult] = []
    for group in groups:
        if len(group) == 1:
            deduped.append(group[0])
            continue
        winner = next(
            (member for member in group if member.id.startswith("PYSEC")),
            group[0],
        )
        merged: set[str] = set()
        for member in group:
            merged.update(member.aliases)
        deduped.append(
            VulnerabilityResult(
                id=winner.id,
                description=winner.description,
                fix_versions=list(winner.fix_versions),
                aliases=merged,
                published=winner.published,
            )
        )
    return deduped
"""
assert src.count(old_tail) == 1, "audit tail not found exactly once"
src = src.replace(old_tail, new_tail)
path.write_text(src, encoding="utf-8")
print("patched pip_audit/_audit.py")
PYEOF

echo "=== oracle smoke check ==="
PYTHONPATH="/workspace/repo" python3 - <<'PYEOF'
from pip_audit._audit import AuditOptions, Auditor, _deduplicate_vulnerabilities
from pip_audit._service import VulnerabilityResult
from packaging.version import Version

v = _deduplicate_vulnerabilities(
    [
        VulnerabilityResult(
            id="PYSEC-0",
            description="d",
            fix_versions=[Version("2.0.0")],
            aliases={"CVE-2024-0001"},
        ),
        VulnerabilityResult(
            id="GHSA-aaaa",
            description="d",
            fix_versions=[Version("2.0.0")],
            aliases={"CVE-2024-0001", "GHSA-aaaa"},
        ),
    ]
)
assert [(r.id, sorted(r.aliases)) for r in v] == [
    ("PYSEC-0", ["CVE-2024-0001", "GHSA-aaaa"])
], v
w = _deduplicate_vulnerabilities(
    [
        VulnerabilityResult(
            id="GHSA-bbbb",
            description="d",
            fix_versions=[Version("2.0.0")],
            aliases={"CVE-2024-0002"},
        ),
        VulnerabilityResult(
            id="PYSEC-1",
            description="d",
            fix_versions=[Version("2.0.0")],
            aliases={"CVE-2024-0002"},
        ),
    ]
)
assert [(r.id, sorted(r.aliases)) for r in w] == [
    ("PYSEC-1", ["CVE-2024-0002"])
], w
print("oracle smoke check passed")
PYEOF
