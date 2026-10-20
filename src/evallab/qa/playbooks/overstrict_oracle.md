## TS07 `overstrict_oracle`: over-strict oracle

**Question.** The required behaviour is agreed. Does the grader nevertheless accept only one implementation, structure, name or output form, so other correct answers fail?

**Where to look.**
- Exact-string or exact-structure assertions.
- Required helper names or file layouts.
- Brittle answer extraction.
- Geometry or format read from one representation only.

**Useful moves (choose as needed).**
- Rewrite the reference in another form the instruction allows: other structure, names, ordering or formatting. Score the rewrite with `probe`.
- Use `diff`, or an independent check, to show that it behaves the same as the reference.

**Sufficient evidence (confirmed).** All three:
- An executed correct solution in a different form that fails.
- Evidence that it is correct: behaviour equivalent to the reference, an independent checker, or a domain rule.
- The grader line that rejects the form.

**Distinguishing check.** Run the rewrite and the reference on the same inputs. Same behaviour, different verdict.

**Not this / false alarms.**
- The form requirement is a separate unstated rule that changes behaviour → `unstated_requirement`.
- The expected value is wrong → `wrong_answer_key`.
- The instruction explicitly requires that form → not a defect.

**Stop / unknown.**
- The equivalence cannot be shown → suspected.
- The rewrite fails for some other reason → fix the rewrite, or drop the finding.
