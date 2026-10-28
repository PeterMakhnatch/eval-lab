# TS-9: Cartopy Natural Earth acquisition directories

- Task/case: `format-code-task-002609` / `D0093`; DEV only.
- QA keys: `mimo-weak-tests-v2.1:format-code-task-002609:line-18554c7fb7` and `mimo-weak-tests-v2.1:format-code-task-002609:line-2619dfa374`.
- Original package: `sha256:3ce64e24f78fad2b3260501178402f81b21a15016637a4aaa98aeeedf31b4e13`.
- Original source SHA256: `c8d4d45f3aaf6b7bf295ae3840b13c5b7dcacd3285f680f1381499f1f4208a77`.
- Original `tests/test.patch` SHA256: `64337053d86b0e32556f64be8cb2a78742a971ad751465ebe04dbb0a37a48759`.
- Assignment SHA256: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`.
- Variant: `sha256:6d9563e42b9e1343a98f0b94282adb5efa4329cc8340169a17a64fe269401a24`.
- Record: `library/task-variants/mimo-v2.6-rl__format-code-task-002609/6d9563e42b9e.json`.
- Transform/status: `verifier-repair@ts9-1` / `candidate`; native evidence remains empty.
- `validation_mode=static-only`.

Instruction lines 3–7 require Natural Earth ZIP acquisition, directory creation, and extraction of the shapefile and required sidecars. The original byte-checking acquisition test only used an absent single-level directory. This candidate parameterizes that same test over an existing category directory and a wholly missing `shapefiles/natural_earth/physical` hierarchy under pytest's isolated `tmp_path`. Preconditions explicitly establish both states and the absence of the requested shapefile.

Only `tests/test.patch` changes. The test calls real `NEShpDownloader.acquire_resource`; only `_urlopen` is replaced with the existing `_ZipResponse`, `_zip_bytes`, and `_coastline_members` offline fixtures. It retains the requested URL, returned path, exact extracted member set, and correct bytes for `.shp`, `.dbf`, `.shx`, `.prj`, and `.cpg`. The intended regressions are directory creation that fails when the category already exists or fails to create missing intermediate directories. Existing optional/missing-required-member tests and native runner inclusion are unchanged. Raw task bytes, instruction, environment, and command wiring remain unchanged.

Authored and derived only: static only; no native oracle or retained-shortcut trial executed. No tests, builds, linters, formatters, sandbox runs, external downloads, or model calls were run. Behavioral success and rejection of historical wrong patches remain untested; the parent owns integrated verification and any later native qualification.
