# Dispatch policy

- Frontier-class workloads are high tier and must run with a tamper-evidence log. The launcher refuses to start them without one.
- The low-tier bypass exists for small models only. A job file cannot downgrade a frontier-class model to it, explicitly or by omission.
- Config refactors may change how job files are parsed. They may not change which jobs are treated as high tier.
- Preflight regression tests must not be weakened or inverted to make a refactor pass.
