# Debugging discipline (Harbor intervention skill v1 — frozen for the A/B)

Use this skill when writing or fixing code that must survive an adversarial
verifier. It encodes one calibrated lesson: self-tests that only cover the easy
variant of an attack class produce false confidence.

## 1. Test the hostile variant, not the easy one

For every defense you write, construct the variant designed to evade it:

- Entity/nesting-encoded payloads, not just raw ones
  (e.g. entity-encoded `<script>` inside `srcdoc`, not just `<script>`).
- Parser-differential constructs: content that your parser and the real consumer
  (browser, interpreter, DB engine) tokenize differently — `</noscript>` inside
  attribute values, namespaced attributes (`xlink:href`), `//` path confusion,
  duplicate attributes, mixed-case tags.
- Boundary encodings your normalizer might miss (numeric entities, UTF-7-style,
  null bytes, overlong forms) where the consumer accepts them.

If your test battery contains only the raw form, it proves nothing. Write the
hostile form first.

## 2. Never repeat an identical failing probe

If a command fails, the next command must differ in at least one substantive
dimension (different input, different flag, added instrumentation, narrowed
scope). A byte-identical retry is never informative. After two consecutive
failures of the same shape, stop and restate your hypothesis about the mechanism
before acting again.

## 3. Inspect after every mutation

After each patch or file write, read back the artifact (or the relevant section)
before testing it. Verify the edit landed where intended and changed nothing
adjacent. A test run against an un-applied edit measures nothing.

## 4. Verify with the consumer's eyes where possible

Prefer end-to-end checks that exercise the real consumer path over unit checks
against your own helper functions. A helper that agrees with itself is not
evidence. Where the real consumer is unavailable, state that gap explicitly in
your final summary.

## 5. Stop rule

Do not declare completion until: the hostile-variant battery passes, the file
compiles/parses cleanly, and one in-place end-to-end run succeeds. Name any
threat class you did not test.
