# har104-d-001896 — linkpreview fallback metadata (reward 0.0)

## What the task asked

Teach `link_preview()` to fill in plain-HTML fallback data when OpenGraph,
Twitter Card, microdata, and JSON-LD have nothing: site name from the page
hostname, title from `<title>` then `<h1>`, description from the meta tag then
nearby paragraphs, image from pictures near the heading then the first picture.
Also add favicon discovery (`favicon`, `absolute_favicon`, plus `to_dict`
keys) as tuples of (link, sizes, type).

## What the model did

| Step | What happened |
|------|---------------|
| 2–4 | Explored the small repo, read the preview classes. |
| 6–7 | Wrote a new `HtmlPreview` class and wired it into `LinkPreview`. |
| 8–29 | Tested with its own check scripts, fixing small bugs (hostname, icon types). |
| 21–22 | Found a real ordering bug (fallback beat OpenGraph) but never fixed it. |
| 30–42 | Tried to finish 5 times, never confirmed; sent empty echo commands instead. |

## Why it failed

The favicon list came back as the wrong container. Tests expect a tuple like
`("a.ico", None, "icon")`, even empty as `()`. The model returned a list like
`["a.ico", ...]`, even empty as `[]`. Same content, wrong box — 4 of 18 tests
failed, so the score was 0. Its own checks asserted the list form, hiding it.

A second bug survived too: the fallback outranks OpenGraph, so a page with
both shows the fallback image. No hidden test caught that one.

## Who is to blame

The model. The task text and tests match exactly, and the test setup ran fine.
The model wrote the wrong container type and walked past its own evidence of
the ordering bug.

## Is the task fair?

Yes. The instruction is a complete behavior list, the package is tiny, and 14
of 18 tests passed with the model's approach. A careful read of the expected
tuple shapes would have closed the gap.
