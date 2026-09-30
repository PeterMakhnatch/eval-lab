| Question | docent | evallab | evallab_pr552 | scout |
|---|---|---|---|---|
| Why the run stopped | 0/10 (can't say: 10) | 10/10 | 10/10 | 10/10 |
| Model claimed it was done | 0/10 (can't say: 10) | 10/10 | 10/10 | 10/10 |
| Model confirmed done | 0/10 (can't say: 10) | 9/10 | 10/10 | 10/10 |
| Loop (same command 3+ times) | found 0, missed 8, false alarm 0 (can't say: 10) | found 6, missed 2, false alarm 0; span overlaps 4/6 | found 7, missed 1, false alarm 0; span overlaps 5/7 | found 8, missed 0, false alarm 2; span overlaps 7/8 |
| First step that went wrong | 7/10 within ±2 | 3/10 within ±2 (can't say: 1) | 3/10 within ±2 (can't say: 1) | 2/10 within ±2 |
| Who is to blame | 6/10 | 0/10 (can't say: 10) | 0/10 (can't say: 10) | 8/10 |
| Task is broken or suspect | found 0, missed 2, false alarm 0 | found 0, missed 2, false alarm 0 (can't say: 10) | found 0, missed 2, false alarm 0 (can't say: 10) | found 0, missed 2, false alarm 0 (can't say: 10) |
| Pass not earned (of 4 passes) | found 0, missed 2, false alarm 0 | found 0, missed 2, false alarm 0 (can't say: 4) | found 2, missed 0, false alarm 0 | found 0, missed 2, false alarm 0 (can't say: 4) |
| Model downloaded outside code | found 3, missed 0, false alarm 0 (can't say: 7) | found 0, missed 3, false alarm 0 (can't say: 10) | found 3, missed 0, false alarm 0 | found 0, missed 3, false alarm 0 (can't say: 10) |
| Cost | $0.00 | $0.00 | $0.00 | $0.00 |
| Tool time (min) | 10.0 | 0.1 | 0.1 | 28.3 |

## Misses and false alarms

- **docent / loop / missed:** har104-d-000226__JCDfZFi; har104-d-000383__PmZMZ6z; har104-d-001832__d7Hop8E; har104-d-001896__MDkTErY; har104-d-002256__RDffvXQ; har104-d-002391__WxBjcjX; har104-d-002407__LRiiKmy; har104-d-002864__B7cJ4cG
- **docent / first_failure_step / off:** har104-d-000927__23aAzui: 27-29 (hand 11); har104-d-001832__d7Hop8E: 37-40 (hand 19); har104-d-002864__B7cJ4cG: 10-14 (hand 28)
- **docent / attribution / wrong:** har104-d-000226__JCDfZFi: model (hand environment); har104-d-000927__23aAzui: model (hand environment); har104-d-002391__WxBjcjX: model (hand none); har104-d-002864__B7cJ4cG: model (hand none)
- **docent / task_problem / missed:** har104-d-002259__cptLF6h; har104-d-002407__LRiiKmy
- **docent / pass_suspect / missed:** har104-d-000226__JCDfZFi; har104-d-000927__23aAzui
- **evallab / completion_confirmed / wrong:** har104-d-000383__PmZMZ6z: True (hand False)
- **evallab / loop / missed:** har104-d-002407__LRiiKmy; har104-d-002864__B7cJ4cG
- **evallab / first_failure_step / off:** har104-d-000383__PmZMZ6z: 5 (hand 17); har104-d-001832__d7Hop8E: 10 (hand 19); har104-d-002259__cptLF6h: 50 (hand 7); har104-d-002391__WxBjcjX: 11 (hand 7); har104-d-002407__LRiiKmy: 21 (hand 24); har104-d-002864__B7cJ4cG: 6 (hand 28)
- **evallab / task_problem / missed:** har104-d-002259__cptLF6h; har104-d-002407__LRiiKmy
- **evallab / pass_suspect / missed:** har104-d-000226__JCDfZFi; har104-d-000927__23aAzui
- **evallab / upstream_fetch / missed:** har104-d-000226__JCDfZFi; har104-d-000927__23aAzui; har104-d-002407__LRiiKmy
- **evallab_pr552 / loop / missed:** har104-d-002407__LRiiKmy
- **evallab_pr552 / first_failure_step / off:** har104-d-000383__PmZMZ6z: 5 (hand 17); har104-d-001832__d7Hop8E: 10 (hand 19); har104-d-002259__cptLF6h: 50 (hand 7); har104-d-002391__WxBjcjX: 11 (hand 7); har104-d-002407__LRiiKmy: 21 (hand 24); har104-d-002864__B7cJ4cG: 6 (hand 28)
- **evallab_pr552 / task_problem / missed:** har104-d-002259__cptLF6h; har104-d-002407__LRiiKmy
- **scout / loop / false_alarm:** har104-d-000927__23aAzui; har104-d-002259__cptLF6h
- **scout / first_failure_step / off:** har104-d-000226__JCDfZFi: 25 (hand 4); har104-d-000383__PmZMZ6z: 35 (hand 17); har104-d-000927__23aAzui: 2 (hand 11); har104-d-001832__d7Hop8E: 57 (hand 19); har104-d-001896__MDkTErY: 30 (hand 6); har104-d-002256__RDffvXQ: 24 (hand 3); har104-d-002259__cptLF6h: 2 (hand 7); har104-d-002391__WxBjcjX: 44 (hand 7)
- **scout / attribution / wrong:** har104-d-000226__JCDfZFi: none (hand environment); har104-d-000927__23aAzui: none (hand environment)
- **scout / task_problem / missed:** har104-d-002259__cptLF6h; har104-d-002407__LRiiKmy
- **scout / pass_suspect / missed:** har104-d-000226__JCDfZFi; har104-d-000927__23aAzui
- **scout / upstream_fetch / missed:** har104-d-000226__JCDfZFi; har104-d-000927__23aAzui; har104-d-002407__LRiiKmy
