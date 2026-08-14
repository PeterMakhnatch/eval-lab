# GPQA-Diamond @ Harbor Hub 1.0

## Source and pin

- **Lane:** `harbor download gpqa-diamond@1.0`
- **Task git:** `https://github.com/laude-institute/harbor-datasets.git` @ `1983ac5c4d43f43cb7a9af9f89c54d09025589ec`
- **Upstream:** [Idavidrein/gpqa](https://huggingface.co/datasets/Idavidrein/gpqa) · [arXiv:2311.12022](https://arxiv.org/abs/2311.12022)
- **On-disk:** `library/benchmarks/gpqa-diamond/gpqa-diamond/` (198 tasks, numeric ids)

## License

CC-BY-4.0 (dataset card).

## Counts / subset

- **Full pin:** 198 Diamond items
- **Materialized:** full 198 (~3.9 MB)
- **Verified sample:** ids `0`, `1`, `10`, `100`, `101`

## Resources

CPU; scientific Python in images. No GPU.

## Sample verification

| Task | Oracle job | Oracle | Nop job | Nop |
| --- | --- | --- | --- | --- |
| 0 | `oracle-ingest-gpqa-0` | **1.0** | `nop-ingest-gpqa-0` | **0.0** |
| 1 | `oracle-ingest-gpqa-1` | **1.0** | `nop-ingest-gpqa-1` | **0.0** |
| 10 | `oracle-ingest-gpqa-10` | **1.0** | `nop-ingest-gpqa-10` | **0.0** |
| 100 | `oracle-ingest-gpqa-100` | **1.0** | `nop-ingest-gpqa-100` | **0.0** |
| 101 | `oracle-ingest-gpqa-101` | **1.0** | `nop-ingest-gpqa-101` | **0.0** |

Harbor 0.21.0; `-k 1 -n 2`. Oracle writes the correct letter to `/app/answer.txt`; nop writes nothing.
