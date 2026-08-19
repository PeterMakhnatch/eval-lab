# M041 — Antigravity Harbor Model Identifier Mapping

Status: complete — ready for review
Last: resolved the Antigravity lane model-identifier namespace mismatch. The lab now maps the local CLI identifier `gemini-3.7-flash-high` to Harbor's expected format `google/gemini-3.7-flash` in `evallab.runner.build_command`, while keeping the verified local CLI identifier distinct in `evallab.modeladapter` and `evallab.profiles`.
Next: OAuth token provisioning for the Antigravity lane container environment via `harbor.agents.installed.antigravity_login`.
Blockers: none for this PR. Real billable execution requires the container OAuth token and standing approval / gate authorization.

## Problem & Namespace Separation

Two distinct model identifier namespaces exist across the lab:

1. **Local CLI namespace** (host execution via `modeladapter.py`):
   - Invokes `agy --model <model> -p <prompt>` on the host.
   - Pinned to `gemini-3.7-flash-high` (listed by `agy models`, verified working).
   - Profiles in `evallab.profiles` and defaults in `evallab.credentials` track this namespace.

2. **Harbor namespace** (container execution via `runner.py` -> `harbor run --model`):
   - Harbor's `antigravity-cli` adapter runs inside Docker.
   - Hard-requires `provider/model_name` format with a slash (`harbor/agents/installed/antigravity_cli.py:776-777`). Passing `gemini-3.7-flash-high` directly raises `ValueError: Model name must be in the format provider/model_name`.
   - Requires base model `google/gemini-3.7-flash` (see source citations below).

The two namespaces must not be collapsed into a single string. `evallab.runner` now defines `LOCAL_TO_HARBOR_MODEL` and `resolve_harbor_model()` to translate local CLI identifiers to Harbor format when building Harbor command lines.

## Harbor Source Evidence

Authority: `/Users/petermakhnatch/.local/share/uv/tools/harbor/lib/python3.12/site-packages/harbor/agents/installed/antigravity_cli.py`

| Concern | Harbor Source Lines | Finding |
|---|---|---|
| Model Name Format | `antigravity_cli.py:776-777` | `if not self.model_name or "/" not in self.model_name: raise ValueError("Model name must be in the format provider/model_name")`. Harbor strictly requires a provider prefix with `/`. |
| Provider Stripping | `antigravity_cli.py:779` | `model = self.model_name.split("/")[-1]`. Harbor strips `google/` to extract base model `gemini-3.7-flash`. |
| LiteLLM Cost Lookup | `antigravity_cli.py:625-629` | `for key in (self.model_name, self.model_name.split("/", 1)[-1]): entry = litellm.model_cost.get(key)`. `litellm.model_cost` contains `gemini-3.7-flash` (valid pricing rates), but has no entry for `gemini-3.7-flash-high` (returns `None`). |
| Reasoning Effort Separation | `antigravity_cli.py:73-78, 81-109, 713-752` | `reasoning_effort` is a separate parameter (`_ReasoningEffort = Literal["minimal", "low", "medium", "high"]`). In `_build_settings_config`, Harbor builds alias `harbor-{model}-{self._reasoning_effort}` and sets `thinkingLevel: self._reasoning_effort.upper()`. Baking `-high` into the model string corrupts the alias and passes an invalid model name to `thinkingConfig`. |
| Container CLI Invocation | `antigravity_cli.py:810-819` | `model_flag = f"--model {shlex.quote(model)} " if model else ""` passes `--model 'gemini-3.7-flash'` to `$HOME/.local/bin/agy` inside the container. |

Harbor model string emitted by the lab for Antigravity: **`google/gemini-3.7-flash`**

## Exact `harbor run --print-config` Check

Executed command (argv shape produced by `build_command`):
```bash
uv run harbor run --path library/tasks/event-summary --agent antigravity-cli --env docker --job-name event-summary-antigravity --jobs-dir runs --n-concurrent 1 --n-attempts 1 --model google/gemini-3.7-flash --print-config
```

Real output:
```json
{
  "job_name": "event-summary-antigravity",
  "jobs_dir": "runs",
  "n_concurrent_trials": 1,
  "agents": [
    {
      "name": "antigravity-cli",
      "model_name": "google/gemini-3.7-flash"
    }
  ],
  "tasks": [
    {
      "path": "library/tasks/event-summary"
    }
  ]
}
```

## What landed

| File | Change |
|---|---|
| `src/evallab/runner.py` | Added `LOCAL_TO_HARBOR_MODEL` mapping dictionary and `resolve_harbor_model(agent, model)` function with Harbor source line citations; updated `build_command(request)` to route through `resolve_harbor_model`. |
| `tests/test_runner.py` | Added unit tests: `test_antigravity_model_translation_for_harbor`, `test_resolve_harbor_model_distinguishes_local_and_harbor_namespaces`, `test_resolve_harbor_model_variants_and_passthrough`. |
| `docs/repo-map.md` | Regenerated repository map. |
| `docs/INDEX.md` | Regenerated documentation index. |

## Mutation Evidence

```
MUTATION 1: Disable translation (map gemini-3.7-flash-high -> gemini-3.7-flash-high)
FAILED tests/test_runner.py::test_antigravity_model_translation_for_harbor
  AssertionError: assert 'gemini-3.7-flash-high' == 'google/gemini-3.7-flash'
FAILED tests/test_runner.py::test_resolve_harbor_model_distinguishes_local_and_harbor_namespaces
  AssertionError: assert 'gemini-3.7-flash-high' == 'google/gemini-3.7-flash'

MUTATION 2: Corrupt variant translation (map gemini-3.1-pro-high -> google/gemini-3.1-pro-WRONG)
FAILED tests/test_runner.py::test_resolve_harbor_model_variants_and_passthrough
  AssertionError: assert 'google/gemini-3.1-pro-WRONG' == 'google/gemini-3.1-pro'

Restored: 33 passed in 1.17s
```

## Verification

- `env -u EVALLAB_DERIVED_ROOT bash scripts/premerge.sh` -> exit code `0` (1522 passed, 2 skipped, 1 xfailed; ty 27 <= 28).
- `uv run pytest tests/test_runner.py tests/test_modeladapter.py tests/test_profiles.py` -> 55 passed.
