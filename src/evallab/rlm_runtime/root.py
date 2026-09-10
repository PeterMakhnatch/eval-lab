"""OpenAI-compatible turn binding onto the RLM capture boundary (HAR-10).

Provenance: this binding is new Eval Lab code; the record shape and the
qualification semantics it feeds come from the Harbor capture-repair lane
decoder at ``lanes/post-training/capture-repair/src/capture_qualification/decoder.py``
commit ``227a5ed7b6b1efbcca945172b1fce36e34b2a961``
(branch ``fix/capture-logprob-alignment``, 2026-09-08); see
``evallab.rlm_runtime.capture`` for the ported contract.

The shared harness-first contract keeps future training on the existing Lego
session proxy token/mask schema rather than a second one, so this module
only binds: it splits the caller's tokenizer output into the prompt and
response regions and forwards everything to :func:`record_root_turn`.

Rules that hold here:

- Token ids are never inferred from text. ``token_ids`` must be the
  caller's tokenizer output for the full turn in reading order (prompt
  tokens followed by response tokens); the response region is the last
  ``len(mask)`` ids. This function performs no tokenization.
- Length mismatches (logprobs vs mask, mask vs token ids) are recorded
  verbatim and rejected by :func:`evallab.rlm_runtime.capture.qualify`
  with explicit reasons; nothing is truncated, padded, or repaired.
- Raw prompt/response text is never stored (AGENTS.md rule 22: no
  unredacted model prompts in committed artifacts). SHA-256 digests keep
  the token-space record correlatable with the conversation log.
- ``actor`` is required so root turns and fixed-worker turns stay distinct
  identities end to end; it is carried into the record and surfaced by
  ``qualify``.
- This binding has no sampling surface of its own; ``sampling`` stays
  ``None`` (explicit missingness) rather than being invented.
"""

from __future__ import annotations

import hashlib

from evallab.rlm_runtime.capture import record_root_turn

OPENAI_BINDING = "openai_chat_completion"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_openai_turn(
    *,
    session_id: str,
    prompt_text: str,
    response_text: str,
    token_ids: list[int],
    logprobs: list[float],
    mask: list[int],
    weight: dict | None,
    origin: str,
    actor: str,
) -> dict:
    """Bind one OpenAI-compatible chat-completion turn into a capture record.

    ``token_ids`` is the caller's tokenizer output for the whole turn in
    reading order; the response region is the trailing ``len(mask)`` ids and
    the prompt region is the remainder. When the mask claims more response
    tokens than exist, the caller's arrays are still forwarded verbatim and
    the structural gate in :func:`qualify` rejects the result with reasons.
    """
    if not isinstance(actor, str) or not actor:
        raise TypeError(
            "actor must be a non-empty string; root and fixed-worker "
            "turns must remain distinct identities"
        )
    if not isinstance(prompt_text, str):
        raise TypeError("prompt_text must be a string")
    if not isinstance(response_text, str):
        raise TypeError("response_text must be a string")
    if not isinstance(token_ids, list):
        raise TypeError("token_ids must be the caller's tokenizer output as a list")

    response_len = len(mask)
    prompt_len = len(token_ids) - response_len
    if prompt_len >= 0:
        prompt_ids = list(token_ids[:prompt_len])
        response_ids = list(token_ids[prompt_len:])
    else:
        prompt_ids = []
        response_ids = list(token_ids)

    record = record_root_turn(
        session_id=session_id,
        prompt_ids=prompt_ids,
        response_ids=response_ids,
        response_mask=mask,
        response_logprobs=logprobs,
        weight=weight,
        origin=origin,
        sampling=None,
    )
    record["actor"] = actor
    record["binding"] = OPENAI_BINDING
    record["prompt_text_sha256"] = _sha256_text(prompt_text)
    record["response_text_sha256"] = _sha256_text(response_text)
    return record
