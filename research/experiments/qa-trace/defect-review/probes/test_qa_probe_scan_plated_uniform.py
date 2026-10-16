"""QA Trace probe for format-code-task-002402 (scan with plated Uniform sites).

The prompt is a bug report: ``control_flow.scan`` over a step that samples a
plated ``dist.Uniform()`` raises ``NotImplementedError`` because Uniform's
argument constraints are ``Dependent``. These cases run the prompt's own
reproducer, then the same model with array-valued (both ``Dependent``) Uniform
arguments, and check only what the reproducer implies: it runs, the sampled
site keeps its plated shape across scan steps, and every new height lies
between the step's bounds.
"""

import jax.numpy as jnp
import numpyro
import numpyro.distributions as dist
import pytest
from numpyro.contrib import control_flow

LOWER = jnp.array(
    [
        [
            [106.0026, 214.71115, 208.37521, 241.16296, 149.16177],
            [106.48185, 110.21102, 220.84187, 182.59181, 228.84312],
        ],
        [
            [352.876, 567.84467, 477.1498, 548.78644, 467.59097],
            [528.7906, 481.11255, 476.48227, 521.28345, 503.7652],
        ],
    ]
)
UPPER = jnp.array(
    [
        [
            [326.3172, 431.25897, 277.67496, 475.2467, 286.37335],
            [181.98593, 383.44434, 293.90152, 348.08966, 391.97644],
        ],
        [
            [594.8691, 688.70105, 813.79987, 736.4565, 848.7659],
            [625.6504, 527.63043, 815.475, 551.9418, 789.07635],
        ],
    ]
)

UNIFORMS = {
    "prompt Uniform()": lambda n: dist.Uniform(),
    "array low and high": lambda n: dist.Uniform(jnp.zeros(n), jnp.ones(n)),
    "scalar low, array high": lambda n: dist.Uniform(0.0, jnp.ones(n)),
}


def _model(make_uniform):
    def layer_unit_model(m, n):
        with numpyro.plate("units", m * n):
            us = numpyro.sample("us", make_uniform(m * n))
        return us.reshape((m, n))

    def model(lower_bounds, upper_bounds):
        def transition(current_height, height_bounds):
            height_lb, height_ub = height_bounds
            thickness_lb = height_lb - current_height
            new_units = layer_unit_model(*current_height.shape)
            thickness = thickness_lb + (height_ub - height_lb) * new_units
            return current_height + thickness, thickness

        _, thicknesses = control_flow.scan(
            transition, jnp.zeros_like(upper_bounds[0]), (lower_bounds, upper_bounds)
        )
        return thicknesses

    return model


@pytest.mark.parametrize("name", list(UNIFORMS))
def test_scan_with_plated_uniform_runs_and_respects_bounds(name: str) -> None:
    model = numpyro.handlers.seed(_model(UNIFORMS[name]), rng_seed=2)
    trace = numpyro.handlers.trace(model).get_trace(LOWER, UPPER)
    thicknesses = numpyro.handlers.seed(_model(UNIFORMS[name]), rng_seed=2)(LOWER, UPPER)
    assert thicknesses.shape == LOWER.shape
    heights = jnp.cumsum(thicknesses, axis=0)
    assert bool(jnp.all(heights >= LOWER - 1e-3)) and bool(jnp.all(heights <= UPPER + 1e-3))
    assert trace["us"]["value"].shape == (LOWER.shape[0], LOWER.shape[1] * LOWER.shape[2])
