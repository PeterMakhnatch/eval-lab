from pathlib import Path
import sys, tempfile
sys.path.insert(0, str(Path(__file__).parents[3]))
from adapter import materialize
from verify import verify

def test_final_state_verifier():
    task_dir=Path(__file__).parents[1]
    size=task_dir.name.rsplit("-", 1)[-1]
    seed=int(task_dir.name.split("-")[3])
    with tempfile.TemporaryDirectory() as tmp:
        generated=Path(tmp) / "task"
        materialize(generated, size, seed)
        assert verify(generated)["reward"] == 0.0
