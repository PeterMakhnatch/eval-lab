#!/bin/sh
set -eu

# Mutates state directly without the qualifying song replacement
python3 -c "
import json
from pathlib import Path
state_path = Path('/app/state/state.json')
data = json.loads(state_path.read_text())
data['gmail']['drafts']['draft_katie_001']['body'] = 'Corrupted body without correct songs'
data['gmail']['drafts']['draft_katie_001']['status'] = 'corrupted'
state_path.write_text(json.dumps(data, indent=2))
"
