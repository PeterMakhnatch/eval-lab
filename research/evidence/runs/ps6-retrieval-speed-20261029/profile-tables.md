# Baseline retrieval profile

Five highest cumulative CPU entries per mode, excluding the retrieval entry point; shares divide each cumulative time by the profiled retrieval CPU time. Entries overlap (for example, parents include children), so shares must not be added. `cProfile` used `time.process_time` and substantially increases the time of call-heavy Python code. Other local measurements ran concurrently.

| Mode | Function (descending cumulative share) |
| --- | --- |
| lexical | `_with_feature_flags` 48.4%; `compute_trace_feature_flags` 48.2%; regex `search` 37.1%; `cues` 26.3%; `_admitted_ids` 22.8% |
| facets | `_example` 29.8%; regex `search` 26.4%; `_admitted_ids` 18.1%; `admitted_cloud_task_ids` 17.3%; `_sealed_text` 17.2% |
| anchor | `_admitted_ids` 40.8%; `admitted_cloud_task_ids` 39.6%; `_retrieval_facets` 29.6%; regex `search` 28.8%; `_with_feature_flags` 17.7% |
| cluster | `_admitted_ids` 34.0%; `admitted_cloud_task_ids` 32.9%; regex `search` 22.6%; `_example` 21.5%; `_retrieval_facets` 18.4% |
| cluster+anchor | `_admitted_ids` 44.2%; `admitted_cloud_task_ids` 42.0%; `_retrieval_facets` 23.3%; regex `search` 22.5%; `task_name` 18.0% |

Unprofiled baseline medians (seconds, n=7). Cold means a fresh database and retriever object per sample; warm is the immediately following call on the same objects. OS caches were not flushed.

| Mode | Cold CPU | Cold wall | Warm CPU | Warm wall |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.784 | 8.723 | 0.325 | 0.744 |
| facets | 1.378 | 16.008 | 0.872 | 3.975 |
| anchor | 0.545 | 5.794 | 0.223 | 0.279 |
| cluster | 0.666 | 0.712 | 0.340 | 0.340 |
| cluster+anchor | 0.515 | 0.601 | 0.192 | 0.223 |

Cold wall timings include substantial I/O contention and should not be interpreted as intrinsic single-user latency. See `profile.py` for the exact workload and `results.json` for the comparison.
