# Tripwire rules v4 (tripwires-v4@7b9391dce855), gate `drop_attempts`

Confirmation gate over retained `tripwires-v3@d338116d6583` rule hits; gate selected on the by-task tune half only (PROTOCOL.md). $0, 0 model calls.

| cohort | v4 gate | v3 ANY (same items) |
|---|---|---|
| tb4-hub-dev-official/full/holdout | recall n/a, precision 0.0% (FA 88/475) | recall n/a, precision 0.0% (FA 351/475) |
| tb4-hub-dev-bootstrap/full/holdout | recall 0/2 = 0.0%, precision 0.0% (FA 90/482) | recall 0/2 = 0.0%, precision 0.0% (FA 356/482) |
| cheatbench-actions-v1/full/test | recall 20/75 = 26.7%, precision 69.0% (FA 9/75) | recall 20/75 = 26.7%, precision 69.0% (FA 9/75) |
| cheatbench-actions-v1/actions/test | recall 20/75 = 26.7%, precision 69.0% (FA 9/75) | recall 20/75 = 26.7%, precision 69.0% (FA 9/75) |
| hacktrace-act-v1/full/test | recall 0/75 = 0.0%, precision n/a (FA 0/75) | recall 0/75 = 0.0%, precision n/a (FA 0/75) |
| hacktrace-act-v1/actions/test | recall 0/75 = 0.0%, precision n/a (FA 0/75) | recall 0/75 = 0.0%, precision n/a (FA 0/75) |
| mimo-v7-canonical-pass70/full/test | recall 1/6 = 16.7%, precision 7.1% (FA 13/64) | recall 3/6 = 50.0%, precision 8.1% (FA 34/64) |
| mimo-v7-canonical-pass70/actions/test | recall 1/6 = 16.7%, precision 7.1% (FA 13/64) | recall 3/6 = 50.0%, precision 8.1% (FA 34/64) |
| mimo-fresh-window3/full/test | recall 1/4 = 25.0%, precision 25.0% (FA 3/17) | recall 3/4 = 75.0%, precision 17.6% (FA 14/17) |
| mimo-fresh-window3/actions/test | recall 1/4 = 25.0%, precision 25.0% (FA 3/17) | recall 3/4 = 75.0%, precision 17.6% (FA 14/17) |
