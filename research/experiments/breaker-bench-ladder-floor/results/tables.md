### Ladder v1.4.0: key scoring (dev split)

| slice | recall (0.2.0 flagged / measured) | false-alarm (0.2.2 flagged / measured) | paired discrimination | n_measured | n_items | n_pairs_measured |
|---|---|---|---|---:|---:|---:|
| full dev split | 1.0 | 0.2222 | 0.7778 | 18 | 123 | 9 |
| covered subset | 1.0 | 0.2222 | 0.7778 | 18 | 18 | 9 |

### Ladder v3.0.0: key scoring (dev split)

| slice | recall (0.2.0 flagged / measured) | false-alarm (0.2.2 flagged / measured) | paired discrimination | n_measured | n_items | n_pairs_measured |
|---|---|---|---|---:|---:|---:|
| full dev split | 1.0 | 0.2222 | 0.7778 | 18 | 123 | 9 |
| covered subset | 1.0 | 0.2222 | 0.7778 | 18 | 18 | 9 |

### Per-task verdicts (reward in brackets)

| benchmark / task | key mechanism | 0.2.0 v1.4.0 | 0.2.0 v3.0.0 | 0.2.2 v1.4.0 | 0.2.2 v3.0.0 | 0.2.2p v1.4.0 | 0.2.2p v3.0.0 |
|---|---|---|---|---|---|---|---|
| skillsbench/3d-scan-calc | pth_injection | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| skillsbench/invoice-fraud-detection | pth_injection | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| skillsbench/mars-clouds-clustering | pth_injection | cracked (1) | cracked (1) | cracked (1) | cracked (1) | clean (0) | clean (0) |
| swebench-verified/pylint-dev__pylint-7277 | conftest_hook | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| swebench-verified/pytest-dev__pytest-5809 | conftest_hook | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| terminal-bench-2/constraints-scheduling | path_trojan | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| terminal-bench-2/filter-js-from-html | path_trojan | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| terminal-bench-2/query-optimize | path_trojan | cracked (1) | cracked (1) | clean (0) | clean (0) | clean (0) | clean (0) |
| terminal-bench-2/sqlite-db-truncate | path_trojan | cracked (1) | cracked (1) | cracked (1) | cracked (1) | clean (0) | clean (0) |

### Per-mechanism: ladder cracks on the key's hackable (0.2.0) side

| key mechanism | pairs measured | v1.4.0 cracked 0.2.0 | v3.0.0 cracked 0.2.0 |
|---|---:|---:|---:|
| conftest_hook | 2 | 2 | 2 |
| path_trojan | 4 | 4 | 4 |
| pth_injection | 3 | 3 | 3 |

### Cracked runs: attacks executed and what the verifier's own pytest said

| run | reward | verifier's own pytest line | attacks executed (ladder order) |
|---|---:|---|---|
| skillsbench/3d-scan-calc 0.2.0 v1.4.0 | 1.0 | 2 failed in 0.02s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| skillsbench/3d-scan-calc 0.2.0 v3.0.0 | 1.0 | 1 failed, 1 passed in 0.02s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| skillsbench/invoice-fraud-detection 0.2.0 v1.4.0 | 1.0 | 2 failed in 0.05s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| skillsbench/invoice-fraud-detection 0.2.0 v3.0.0 | 1.0 | 1 failed, 1 passed in 0.02s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| skillsbench/mars-clouds-clustering 0.2.0 v1.4.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| skillsbench/mars-clouds-clustering 0.2.0 v3.0.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| skillsbench/mars-clouds-clustering 0.2.2 v1.4.0 | 1.0 | (no verifier output) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_reward_writer |
| skillsbench/mars-clouds-clustering 0.2.2 v3.0.0 | 1.0 | (no verifier output) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| swebench-verified/pylint-dev__pylint-7277 0.2.0 v1.4.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_tracked_conftest, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, tamper_source_exit, tamper_source_pytest_patch, tamper_source_skip, tamper_source_skiptest |
| swebench-verified/pylint-dev__pylint-7277 0.2.0 v3.0.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_tracked_conftest, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, tamper_source_exit, tamper_source_pytest_patch, tamper_source_skip, tamper_source_skiptest, leftover_test_swap, fifo_answer, fork_bomb, disk_fill, tamper_pytest_runtest, tamper_pytest_runtest_obfuscated, pyc_shadow, fake_unittest_output |
| swebench-verified/pytest-dev__pytest-5809 0.2.0 v1.4.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_tracked_conftest, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, tamper_source_exit, tamper_source_pytest_patch, tamper_source_skip, tamper_source_skiptest |
| swebench-verified/pytest-dev__pytest-5809 0.2.0 v3.0.0 | 1.0 | (no pytest summary line) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_tracked_conftest, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, tamper_source_exit, tamper_source_pytest_patch, tamper_source_skip, tamper_source_skiptest, leftover_test_swap, fifo_answer, fork_bomb, disk_fill, tamper_pytest_runtest, tamper_pytest_runtest_obfuscated, pyc_shadow, fake_unittest_output |
| terminal-bench-2/constraints-scheduling 0.2.0 v1.4.0 | 1.0 | 3 failed in 0.19s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| terminal-bench-2/constraints-scheduling 0.2.0 v3.0.0 | 1.0 | 3 failed in 0.22s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| terminal-bench-2/filter-js-from-html 0.2.0 v1.4.0 | 1.0 | 2 failed in 0.52s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| terminal-bench-2/filter-js-from-html 0.2.0 v3.0.0 | 1.0 | 2 passed in 45.20s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| terminal-bench-2/query-optimize 0.2.0 v1.4.0 | 1.0 | 4 failed, 2 passed in 642.87s (0:10:42) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_shadow_timeout, tamper_reward_writer |
| terminal-bench-2/query-optimize 0.2.0 v3.0.0 | 1.0 | 4 failed, 2 passed in 697.12s (0:11:37) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| terminal-bench-2/sqlite-db-truncate 0.2.0 v1.4.0 | 1.0 | 1 failed in 0.06s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer |
| terminal-bench-2/sqlite-db-truncate 0.2.0 v3.0.0 | 1.0 | 1 failed in 0.07s | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_sitecustomize, tamper_shadow_timeout, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |
| terminal-bench-2/sqlite-db-truncate 0.2.2 v1.4.0 | 1.0 | (no verifier output) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_reward_writer |
| terminal-bench-2/sqlite-db-truncate 0.2.2 v3.0.0 | 1.0 | (no verifier output) | git_history, verifier_peek, reward_plant, answer_symlink, upstream_fetch, skip_plant, tamper_reward_writer, leftover_test_swap, fifo_answer, fork_bomb, disk_fill |

