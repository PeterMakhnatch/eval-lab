# Pinned INDEX render, pre-HAR-452

`INDEX.md` and `INDEX-all.md` are the byte output of
`evallab.results_home.write_index` at
`bc186e4749a834302e009b780e54d14f070802cc` (the parent of the HAR-452 merge
commit `613c42936053a9286156e5a2f9c8f67149e521ad`), rendered for the exact
input `tests/test_results_home.py::test_index_reward_report_flags` builds: one
published job `results/2026-09-30/HAR-452-reward-report/` whose
`processed/job.json` summary is `{"n_pass": 1, "n_fail": 0, "n_unscored": 0}`
with no `reward_report_flags`, a `provenance.json` of
`{"card": "HAR-452", "repository": {"commit": "abc123"}}`, and an empty
`primary_checkout`.

`{{HOME}}` stands in for the resolved results-home path, so the bytes compare
independent of the machine's `tmp_path` layout. The test templates its own
render the same way before comparing.
