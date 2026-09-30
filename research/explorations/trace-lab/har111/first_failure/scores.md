# HAR-111 first_failure scores

Method: real `report run` output per trial, ±2 steps, null == null is a match. HAR-109 labels from git b6fada64; HAR-81 `step_ref` mapped to report ordinals through report segment order.

|  | HAR-109 (10) | HAR-81 (44) |
|---|---|---|
| first_error | 3/10 | 10/44 |
| first_failure (new) | 8/10 | 30/44 |

## HAR-109 per-run

- har104-d-000226__JCDfZFi: hand=4 first_error=2 [hit] first_failure=upstream_fetch=4 [hit]
- har104-d-000383__PmZMZ6z: hand=17 first_error=5 [miss] first_failure=bad_edit=17 [hit]
- har104-d-000927__23aAzui: hand=11 first_error=9 [hit] first_failure=upstream_fetch=11 [hit]
- har104-d-001832__d7Hop8E: hand=19 first_error=10 [miss] first_failure=bad_edit=20 [hit]
- har104-d-001896__MDkTErY: hand=6 first_error=4 [hit] first_failure=tool_error=4 [hit]
- har104-d-002256__RDffvXQ: hand=3 first_error=None [miss] first_failure=None=None [miss]
- har104-d-002259__cptLF6h: hand=7 first_error=50 [miss] first_failure=None=None [miss]
- har104-d-002391__WxBjcjX: hand=7 first_error=11 [miss] first_failure=bad_edit=6 [hit]
- har104-d-002407__LRiiKmy: hand=24 first_error=21 [miss] first_failure=upstream_fetch=24 [hit]
- har104-d-002864__B7cJ4cG: hand=28 first_error=6 [miss] first_failure=stuck_cycle=29 [hit]

## HAR-81 per-run

- har81-l-d-a2-arvo-18737__2kbVhKB: hand=None first_error=7 [miss] first_failure=bad_edit=7 [miss]
- har81-l-d-a2-arvo-42485576__YQ3rQ7R: hand=2 (head#2) first_error=None [miss] first_failure=harness_rejection=2 [hit]
- har81-l-d-a2-arvo-42496599__rzayj9j: hand=11 (head#11) first_error=None [miss] first_failure=harness_rejection=11 [hit]
- har81-l-d-a2-candidate-1271-medi__2tAyTq3: hand=87 (trajectory.cont-1.json#30) first_error=27 [miss] first_failure=harness_rejection=87 [hit]
- har81-l-d-a2-candidate-1634-soft__B8Jt3cw: hand=None first_error=6 [miss] first_failure=bad_edit=9 [miss]
- har81-l-d-a2-candidate-1789-secu__4kHk8vj: hand=7 (head#7) first_error=4 [miss] first_failure=bad_edit=10 [miss]
- har81-l-d-a2-format-code-task-00__6TqzNQw: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-l-d-a2-format-code-task-00__NGhDDRU: hand=None first_error=19 [miss] first_failure=bad_edit=8 [miss]
- har81-l-d-a3-arvo-18737__FygpNSe: hand=4 (head#4) first_error=32 [miss] first_failure=harness_rejection=4 [hit]
- har81-l-d-a3-arvo-42485576__MNqUNYv: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-l-d-a3-arvo-42496599__RC4jf7K: hand=None first_error=18 [miss] first_failure=None=None [hit]
- har81-l-d-a3-candidate-1271-medi__xn4sAug: hand=7 (head#7) first_error=10 [miss] first_failure=harness_rejection=7 [hit]
- har81-l-d-a3-candidate-1634-soft__rfQaHJx: hand=None first_error=8 [miss] first_failure=bad_edit=12 [miss]
- har81-l-d-a3-candidate-1789-secu__WacRiXN: hand=6 (head#6) first_error=4 [hit] first_failure=bad_edit=8 [hit]
- har81-l-d-a3-format-code-task-00__BrJZfvJ: hand=None first_error=4 [miss] first_failure=bad_edit=4 [miss]
- har81-l-d-a3-format-code-task-00__ZsTxf68: hand=None first_error=33 [miss] first_failure=bad_edit=20 [miss]
- har81-l-d-a4-arvo-18737__P3YLKKe: hand=3 (head#3) first_error=39 [miss] first_failure=harness_rejection=3 [hit]
- har81-l-d-a4-arvo-42485576__AAB9BeL: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-l-d-a4-arvo-42496599__XC6B4Ue: hand=None first_error=5 [miss] first_failure=None=None [hit]
- har81-l-d-a4-candidate-1271-medi__wCBkiuG: hand=None first_error=20 [miss] first_failure=None=None [hit]
- har81-l-d-a4-candidate-1634-soft__TJN8GB5: hand=None first_error=7 [miss] first_failure=bad_edit=10 [miss]
- har81-l-d-a4-candidate-1789-secu__VfC7WMo: hand=8 (head#8) first_error=4 [miss] first_failure=bad_edit=12 [miss]
- har81-l-d-a4-format-code-task-00__2JNXcHz: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-l-d-a4-format-code-task-00__ZmQBZYW: hand=None first_error=4 [miss] first_failure=bad_edit=4 [miss]
- har81-p-d-arvo-18737__8bHpbg3: hand=16 (head#16) first_error=13 [miss] first_failure=harness_rejection=16 [hit]
- har81-p-d-arvo-41330__Zi79ug4: hand=None first_error=56 [miss] first_failure=None=None [hit]
- har81-p-d-arvo-42485576__dsgmV5c: hand=None first_error=72 [miss] first_failure=bad_edit=33 [miss]
- har81-p-d-arvo-42496599__GkwMiLe: hand=14 (head#14) first_error=11 [miss] first_failure=harness_rejection=14 [hit]
- har81-p-d-arvo-42514310__BGTEMs2: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-p-d-arvo-42528228__8WpUvat: hand=4 (head#4) first_error=14 [miss] first_failure=harness_rejection=4 [hit]
- har81-p-d-arvo-57589__xvqMDKd: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-p-d-candidate-1048-operati__Rh9y42B: hand=39 (head#39) first_error=None [miss] first_failure=harness_rejection=39 [hit]
- har81-p-d-candidate-1271-media-g__grehkae: hand=None first_error=17 [miss] first_failure=None=None [hit]
- har81-p-d-candidate-1559-ml-eval__bqBv49S: hand=7 (head#7) first_error=None [miss] first_failure=harness_rejection=7 [hit]
- har81-p-d-candidate-1634-softwar__PaSTYBj: hand=None first_error=9 [miss] first_failure=tool_error=9 [miss]
- har81-p-d-candidate-1702-ml-infe__izs4jgG: hand=2 (head#2) first_error=2 [hit] first_failure=tool_error=2 [hit]
- har81-p-d-candidate-1789-securit__Nhf2HdJ: hand=7 (head#7) first_error=7 [hit] first_failure=bad_edit=7 [hit]
- har81-p-d-candidate-2684-securit__uwoAzn7: hand=2 (head#2) first_error=15 [miss] first_failure=harness_rejection=2 [hit]
- har81-p-d-format-code-task-00024__9KYGvwT: hand=None first_error=None [hit] first_failure=None=None [hit]
- har81-p-d-format-code-task-00043__iT2yUa7: hand=None first_error=12 [miss] first_failure=bad_edit=17 [miss]
- har81-p-d-format-code-task-00152__P9EP83h: hand=None first_error=4 [miss] first_failure=bad_edit=4 [miss]
- har81-p-d-format-code-task-00164__bMN27md: hand=None first_error=11 [miss] first_failure=None=None [hit]
- har81-p-d-format-code-task-00253__7xmXPRq: hand=None first_error=8 [miss] first_failure=None=None [hit]
- har81-p-d-format-code-task-00301__4KvLSba: hand=None first_error=32 [miss] first_failure=None=None [hit]
