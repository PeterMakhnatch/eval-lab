"""Preserve lost-unit composites and existing conversion-error semantics."""

import numpy as np
import pytest
import xarray as xr

# Reuse the native external-I/O boundary; all Satpy writer logic stays real.
from satpy.tests.writer_tests.test_v7_ninjotiff_workflow import (
    offline_ninjotiff as offline_ninjotiff,
)


def _output_options(conversion_mode):
    options = {
        "filename": "offline-unit-workflow.tif",
        "ch_min_measurement_unit": -100.0,
        "ch_max_measurement_unit": 50.0,
    }
    if conversion_mode != "default":
        options["convert_temperature_units"] = conversion_mode == "enabled"
    return options


def _assert_written_values(backend, expected):
    assert backend.calls, "The dataset must reach the external writer boundary"
    for image, _filename, _compute, _options in backend.calls:
        np.testing.assert_allclose(image.data.squeeze().values, expected, rtol=0, atol=1e-7)


@pytest.mark.parametrize("conversion_mode", ["default", "enabled", "disabled"])
@pytest.mark.parametrize("unit_source", ["argument", "product_config"])
def test_missing_units_preserve_composite_values(offline_ninjotiff, conversion_mode, unit_source):
    """Composites that lost units must still be saved without conversion."""
    backend = offline_ninjotiff
    values = np.array([[250.15, 273.15, 310.15], [280.15, 290.15, 300.15]], dtype=np.float64)
    dataset = xr.DataArray(values.copy(), dims=("y", "x"), attrs={})
    options = _output_options(conversion_mode)
    if unit_source == "argument":
        options["physic_unit"] = "CELSIUS"
    else:
        options.update(
            ninjo_product_name="synthetic-ir",
            ninjo_product_file="offline-products.cfg",
        )

    writer = backend.module.NinjoTIFFWriter(enhance=False)
    writer.save_dataset(dataset, **options)

    _assert_written_values(backend, values)


@pytest.mark.parametrize("conversion_mode", ["default", "enabled", "disabled"])
def test_unsupported_conversion_requires_explicit_skip(offline_ninjotiff, conversion_mode):
    """C-to-K remains unsupported unless conversion is explicitly disabled."""
    backend = offline_ninjotiff
    values = np.array([[-40.0, -1.5, 0.0], [5.5, 20.0, 35.0]], dtype=np.float64)
    dataset = xr.DataArray(values.copy(), dims=("y", "x"), attrs={"units": "C"})
    options = _output_options(conversion_mode)
    options["physic_unit"] = "K"
    writer = backend.module.NinjoTIFFWriter(enhance=False)

    if conversion_mode == "disabled":
        writer.save_dataset(dataset, **options)
        _assert_written_values(backend, values)
    else:
        with pytest.raises(NotImplementedError):
            writer.save_dataset(dataset, **options)
        assert not backend.calls, "An unsupported conversion must not write an image"
