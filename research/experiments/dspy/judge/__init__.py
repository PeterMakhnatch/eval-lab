"""DSPy calibration judge: a typed judge program over the sealed calibration corpus.

Reuses ``evallab.calibrate`` for corpus loading, the frozen split and rubric
digests. Adds a typed DSPy program, an agreement metric with GEPA feedback and
a runner that writes ``JudgePredictionBundle`` files the existing
``evallab calibrate --predictions`` path can turn into calibration records.
"""

# Import order is load-bearing: see the note in ``lm.py``. Importing the shared
# LM module first brings in numpy before dspy's lazy numpy proxy exists.
import lm  # noqa: F401
