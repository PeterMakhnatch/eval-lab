| policy | n | acc | chain-lookup | ledger-agg | state-tracking | err | budget | iters | sub | in tok | out tok | reason tok | $/task | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| stock-lenient | 14 | 1.000 | 1.00 | 1.00 | 1.00 | 0 | 0 | 4.0 | 0.0 | 11593 | 4006 | 2314 | 0.034 | 99 |
| stock | 12 | 1.000 | 1.00 | 1.00 | nan | 0 | 0 | 5.5 | 0.0 | 17910 | 5922 | 3678 | 0.051 | 143 |

Paired against `stock` (same task ids; exact two-sided sign test):

| candidate | paired n | acc wins | acc losses | ties | acc delta | p(acc) | cost ratio | cheaper/dearer | p(cost) | iters ratio | wall ratio |
|---|---|---|---|---|---|---|---|---|---|---|---|
| stock-lenient | 12 | 0 | 0 | 12 | +0.000 | 1.000 | 0.69 | 7/5 | 0.774 | 0.74 | 0.72 |
