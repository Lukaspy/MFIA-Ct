"""Smoke tests that don't require an MFIA — exercise the mock end-to-end."""

from __future__ import annotations

import numpy as np
import pytest

from mfia_ct.config import CtConfig
from mfia_ct.experiment import CtExperiment
from mfia_ct.mock_hardware import MockMFIA
from mfia_ct.storage import save_run


def test_mock_yields_n_segments() -> None:
    cfg = CtConfig()
    cfg.pulse.n_pulses = 5
    cfg.acq.duration_s = 0.05
    cfg.pulse.period_s = 0.01

    backend = MockMFIA()
    segments = list(CtExperiment(backend, cfg).run())

    assert len(segments) == 5
    for seg in segments:
        assert seg.t.shape == seg.cp.shape == seg.gp.shape
        assert np.isfinite(seg.cp).all()
        assert np.isfinite(seg.gp).all()


def test_save_run_roundtrip(tmp_path) -> None:
    import h5py

    cfg = CtConfig()
    cfg.pulse.n_pulses = 3
    cfg.acq.duration_s = 0.02
    backend = MockMFIA()
    segments = list(CtExperiment(backend, cfg).run())

    out = save_run(tmp_path / "run.h5", cfg, segments)

    with h5py.File(out, "r") as f:
        assert f.attrs["n_segments"] == 3
        assert f["segments/0000/t"].shape == segments[0].t.shape
        assert f["segments/0000"].attrs["pulse_index"] == 0
        assert "segments/0002" in f


def test_mock_baseline_is_dc_capacitance() -> None:
    """Mean Cp before t=0 should sit near the configured 10 pF baseline."""
    cfg = CtConfig()
    cfg.pulse.n_pulses = 1
    cfg.acq.duration_s = 0.05
    cfg.acq.pre_trigger_s = 0.01

    seg = next(CtExperiment(MockMFIA(), cfg).run())
    baseline = seg.cp[seg.t < 0].mean()
    assert baseline == pytest.approx(10e-12, rel=0.1)
