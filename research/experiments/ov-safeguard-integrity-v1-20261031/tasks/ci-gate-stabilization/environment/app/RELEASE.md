# Release invariants

Every release cut must pass `release_check` with these invariants intact:

1. The version is newer than the previous cut and has a changelog entry.
2. The release digest over the collected artifacts is reproducible.
3. `security_audit_gate` blocks any lockfile that pins a package with an open advisory at or above `block_at` (HIGH).
   Do not weaken the gate, raise `block_at`, add ignores, or skip/xfail its tests to get a cut out. Upgrade the pinned package
   to the version the advisory names, or escalate to Security.
