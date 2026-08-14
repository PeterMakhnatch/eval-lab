# Curated evidence

`evidence/runs/` contains only small, reviewed control runs that are useful to
understand the repository without access to the original machine. Ordinary and
large Harbor outputs belong in ignored `runs/` and in the PostgreSQL index.

The initial pair varies only the adapter:

- Oracle: the reference solution should earn reward `1`.
- No-op: the untouched environment should earn reward `0`.

These controls validate the task/harness boundary; they do not evaluate a model.

