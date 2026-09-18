---
status: living
audience:
  - operator
---

# Workbench Guide: Multi-Agent Terminal Workflow

This document records the standard tools, keybindings, and delegation conventions for operators working in `eval-lab`.

---

## 1. Paired Request & Response Viewer (`last`)

Whenever an agent runs long tool loops and you want to see your **full unclipped request** sitting directly beside the **agent's final response**:

```bash
last             # Shows your latest full request paired with the final response
last <num>       # Shows Turn #N directly (e.g. `last 4` or `last 20`)
last -<num>      # Shows N turns ago (e.g. `last -1` or `last -2`)
last -l          # Lists recent 25 human prompts with #Turn numbers
last -e <num>    # Opens Turn #N in Cursor (zero terminal truncation)
last -e all      # Opens the ENTIRE conversation history in Cursor
```
