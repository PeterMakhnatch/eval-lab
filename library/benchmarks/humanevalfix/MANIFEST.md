# HumanEvalFix @ Harbor Hub 1.0

## Source and pin

- **Lane:** `harbor download humanevalfix@1.0`
- **Task git:** `https://github.com/laude-institute/harbor-datasets.git` @ `ab02ff13250fae8d91b93a6e4c11ce0bdcb78215`
- **Upstream:** HumanEvalPack / OctoPack ([arXiv:2308.07124](https://arxiv.org/abs/2308.07124)), HF `bigcode/humanevalpack`
- **On-disk:** `library/benchmarks/humanevalfix/humanevalfix/` (164 `python-*` tasks)

## License

HumanEval / HumanEvalPack research terms. Lab-internal eval use.

## Counts / subset

- **Full pin:** 164 Python repair tasks
- **Materialized:** full 164 (~4.5 MB)
- **Verified sample:** `python-0`, `python-1`, `python-10`, `python-100`, `python-101`

## Resources

CPU Python pytest. No GPU.

## Sample verification

Harbor 0.21.0; `-k 1 -n 2`. Oracle applies the official fix; nop leaves the buggy function.

| Task | Oracle job | Oracle | Nop job | Nop |
| --- | --- | --- | --- | --- |
| python-0 | `oracle-ingest-hef-python-0` | **1.0** | `nop-ingest-hef-python-0` | **0.0** |
| python-1 | `oracle-ingest-hef-python-1` | **1.0** | `nop-ingest-hef-python-1` | **0.0** |
| python-10 | `oracle-ingest-hef-python-10` | **1.0** | `nop-ingest-hef-python-10` | **0.0** |
| python-100 | `oracle-ingest-hef-python-100` | **1.0** | `nop-ingest-hef-python-100` | **0.0** |
| python-101 | `oracle-ingest-hef-python-101` | **1.0** | `nop-ingest-hef-python-101` | **0.0** |
