"""Scout LLM reader for the HAR-119 hand-label fields (Traces/model-readers).

One structured ``llm_scanner`` (``trial_reader``) asks the shared question
set from ``model_readers.prompts`` (RATER_GUIDE distilled) and returns one
typed answer per transcript, with ``[Mn]`` message cites and short verbatim
quotes per claim. Ref mapping (``[Mn]`` -> ``head#N``) and quote grounding
happen afterwards in ``build_predictions.py`` (see ``../verify.py``) —
the model never sees step ids, so numbering drift cannot silently corrupt
a step.

Model selection is NOT hardcoded here: pass it on the scan command line so
the same frozen question set runs under any provider, e.g. ::

    keys run -- \
    env ZAI_OPENAPI_BASE_URL=https://api.z.ai/api/paas/v4 \
    uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with openai \\
      scout scan research/explorations/trace-lab/model-readers/scout_llm/trial_reader.py \\
      -T <transcripts dir> --scans <scans dir> --display plain \\
      --model openai-api/zai-openapi/glm-5.3 \\
      --model-base-url https://api.z.ai/api/paas/v4

Once Peter adds ``ANTHROPIC_API_KEY`` to the keys store, the one-line
change is the ``--model`` flag::

    --model anthropic/claude-opus-5-5

(no ``--model-base-url``; the key comes from ``ANTHROPIC_API_KEY``).
"""

import json
import sys
from pathlib import Path
from typing import Literal

from inspect_scout import Scanner, Transcript, llm_scanner, scanner
from inspect_scout._llm_scanner.types import AnswerStructured
from inspect_scout._scanner.extract import MessagesPreprocessor
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve()
MODEL_READERS = HERE.parent.parent
if str(MODEL_READERS) not in sys.path:
    sys.path.insert(0, str(MODEL_READERS))

from prompts import SCOUT_FACTS_TAIL, SCOUT_MODEL, SCOUT_QUESTION  # noqa: E402


class TrialReading(BaseModel):
    """One typed reading of a single agent-run transcript."""

    stop_reason: Literal[
        "model_finished",
        "request_ceiling",
        "token_ceiling",
        "infra_error",
        "agent_timeout",
        "other",
    ] = Field(description="How the run ended, per the STOP definitions.")
    stop_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the ending: completion handshake, budget exception, or error.'
    )
    first_failure_is_pass: bool = Field(
        description="True ONLY for a clean earned pass on the model's own work; a pass built on fetched upstream code is not a pass here."
    )
    first_failure_what: str = Field(
        description="One line saying what the earliest failing turn did; for a fetched-code pass, describe the fetch."
    )
    first_failure_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the earliest failing turn, or of the fetch/paste turn for a fetched-code pass; "none" for a clean earned pass. Earliest deciding evidence first.'
    )
    blame: Literal["model", "harness", "task", "infra", "none"] = Field(
        description="Whose failure best explains the outcome, per the BLAME definitions; none for a clean earned pass."
    )
    blame_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the deciding evidence for blame.'
    )
    loop_kind: Literal["completion-claim", "repetition", "none"] = Field(
        description="The loop pattern that consumed the run, per the LOOP definitions."
    )
    loop_start_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the loop start: first completion claim, or first repeated action; "none" when loop_kind is none.'
    )
    loop_end_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the loop end: last claim or last repeated turn; "none" when loop_kind is none.'
    )
    upstream_fetch: bool = Field(
        description="True when the run fetched outside code (pip, curl, git clone, download) or pasted a large outside block that the run stands on."
    )
    upstream_evidence: str = Field(
        description='The [Mn] cite and verbatim "quote" (at most 25 words) of the fetch or paste turn; "none observed" when every edit was typed in place.'
    )


# Keep every rendered message so [Mn] matches transcript.messages order
# (the order _ref_index_map in scout/scanners.py assumes). The default
# preprocessor drops system messages, which would shift all cites.
_KEEP_ALL = MessagesPreprocessor(
    exclude_system=False, exclude_reasoning=False, exclude_tool_usage=False
)


async def _question(transcript: Transcript) -> str:
    """Frozen question plus blind run facts (reward/episodes/exception).

    The facts come from the run record (``result.json`` via the
    transcript's ``trial_dir`` metadata) — the same record the human
    raters had — never from labels.
    """
    reward: object = "unknown"
    episodes: object = "unknown"
    exception: object = "none"
    meta = transcript.metadata or {}
    trial_dir = meta.get("trial_dir") if isinstance(meta, dict) else None
    if trial_dir:
        try:
            result = json.loads((Path(str(trial_dir)) / "result.json").read_text())
            rewards = (result.get("verifier_result") or {}).get("rewards") or {}
            if rewards.get("reward") is not None:
                reward = rewards["reward"]
            n_ep = ((result.get("agent_result") or {}).get("metadata") or {}).get(
                "n_episodes"
            )
            if n_ep is not None:
                episodes = n_ep
            exc = (result.get("exception_info") or {}).get("exception_type")
            if exc:
                exception = exc
        except (OSError, ValueError):
            pass
    return SCOUT_QUESTION + SCOUT_FACTS_TAIL.format(
        reward=reward, episodes=episodes, exception=exception
    )


@scanner(messages="all")
def trial_reader() -> Scanner[Transcript]:
    """Structured LLM reading: the frozen HAR-119 question set per transcript."""
    return llm_scanner(
        question=_question,
        answer=AnswerStructured(type=TrialReading),
        preprocessor=_KEEP_ALL,
        # Model comes from the scan --model flag (default model). The frozen
        # value is prompts.SCOUT_MODEL (recorded in predictions rows).
        model=None,
        name="trial_reader",
        context_window=200000,
    )


# Provenance anchor: the frozen model this question set is validated with.
FROZEN_SCOUT_MODEL: str = SCOUT_MODEL
