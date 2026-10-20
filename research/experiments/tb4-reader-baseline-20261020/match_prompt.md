# Semantic match prompt (frozen before any matching ran)

## Goal
Decide, per task, which findings of two anonymous QA reviewers (list A and list B) are the same defect as a finding in an answer key. The key is an independent audit of the task; the reviewers each read the task package separately.

## Input
`<root>/match/<aid>.json` for each task id in your batch file. Each has:
- `key_findings`: the key's findings for the task (`item_id`, `title`, `class`, `category`).
- `preference_model_text_for_task`: the key author's full write-up of the task, which explains what each key finding is. Use it to understand each key finding's mechanism.
- `list_A`, `list_B`: each reviewer's findings (`idx`, `class`, `confidence`, `title`, `summary`, `evidence_refs`, quotes).

You do not need the task files. Read only your assigned packets and the batch file; do not open anything else, and do not browse the web.

## Matching rules
For each list separately, build a 1-to-1 matching: each reviewer finding is matched to at most one key finding and each key finding to at most one reviewer finding in that list. The two lists are matched independently, with the same standard.

- `same`: the reviewer finding identifies the same underlying fault as the key finding: same place in the task (file, test, rule, mechanism) and same way it makes the reward wrong. Different wording or a different class label is fine. A reviewer finding that is broader but clearly covers the key finding's specific mechanism counts as `same` only if it names that mechanism.
- `partial`: same area of the task or same class of fault, but a different mechanism, or too vague to show it found this key finding.
- Leave a reviewer finding unmatched if it concerns something the key does not list. Leave a key finding unmatched if no reviewer finding covers it.
- Prefer `same` matches; use `partial` only for the remaining pairs. A key finding the reviewer found by a different route, as long as it is the same fault, is `same`.

## Output
Write `<outroot>/<aid>.json` for each id:

```json
{"aid": "r-xxxxxxxx",
 "A": [{"idx": 0, "key_item_id": "pm-tb4:...", "strength": "same" | "partial"}],
 "B": [{"idx": 2, "key_item_id": "pm-tb4:...", "strength": "same" | "partial"}],
 "notes": "<= 2 sentences, optional"}
```

List only matched findings. Each `idx` and each `key_item_id` appears at most once per list. Valid JSON only. Final message: per task, the number of `same` and `partial` matches in A and in B.
