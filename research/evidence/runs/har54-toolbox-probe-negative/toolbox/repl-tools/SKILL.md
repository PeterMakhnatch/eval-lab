---
name: repl-tools
description: Standard library Python toolbox for bounded file inspection, regex grep, and output checking.
---

# repl-tools

Standard library Python toolbox available at `/harbor/skills/repl-tools/repl_tools.py`.

## CLI Usage
- `python3 /harbor/skills/repl-tools/repl_tools.py read <file> [--start N] [--end M]`
- `python3 /harbor/skills/repl-tools/repl_tools.py grep <pattern> [path] [--max-matches N] [--context N]`
- `python3 /harbor/skills/repl-tools/repl_tools.py check <file> [--format auto|json|text]`

## Python API
```python
import sys
sys.path.insert(0, "/harbor/skills/repl-tools")
from repl_tools import read_window, smart_grep, check_output
```
