| policy | n | acc | memo-classify | err | budget | iters | sub | in tok | out tok | reason tok | $/task | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| orchestrator-lenient | 4 | 1.000 | 1.00 | 0 | 0 | 5.0 | 3.5 | 24308 | 19291 | 14532 | 0.119 | 414 |
| stock-lenient | 4 | 1.000 | 1.00 | 0 | 0 | 4.8 | 5.2 | 25976 | 15264 | 11205 | 0.104 | 1449 |

Paired against `stock-lenient` (same task ids; exact two-sided sign test):

| candidate | paired n | acc wins | acc losses | ties | acc delta | p(acc) | cost ratio | cheaper/dearer | p(cost) | iters ratio | wall ratio |
|---|---|---|---|---|---|---|---|---|---|---|---|
| orchestrator-lenient | 4 | 0 | 0 | 4 | +0.000 | 1.000 | 1.15 | 1/3 | 0.625 | 1.05 | 0.29 |
