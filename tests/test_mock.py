"""Smoke tests that don't require an MFIA — exercise the mock end-to-end."""

from __future__ import annotations

import h5py
import numpy as np

from mfia_ct.config import CtConfig, PulseSource
from mfia_ct.experiment import CtExperiment
from mfia_ct.led_source import MockLedSource
from mfia_ct.mock_fg import MockFunctionGenerator
from mfia_ct.mock_hardware import MockMFIA
from mfia_ct.storage import save_run


def _fast_cfg(n_pulses: int = 3) -> CtConfig:
    cfg = CtConfig()
    cfg.pulse.n_pulses = n_pulses
    cfg.pulse.period_s = 0.02
    cfg.pulse.pulse_width_s = 0.005
    cfg.acq.poll_interval_s = 0.005
    cfg.demod.sample_rate_hz = 10_000.0
    return cfg


def test_continuous_stream_emits_chunks() -> None:
    cfg = _fast_cfg(n_pulses=3)
    backend = MockMFIA()
    exp = CtExperiment(backend, cfg)

    chunks = list(exp.run())
    assert len(chunks) > 0

    t = np.concatenate([c.t for c in chunks])
    cp = np.concatenate([c.cp for c in chunks])
    gp = np.concatenate([c.gp for c in chunks])

    # Time axis is monotonic non-decreasing.
    assert np.all(np.diff(t) >= 0)
    # Stream covers at least the pulse train duration.
    assert t[-1] >= (cfg.pulse.n_pulses - 1) * cfg.pulse.period_s
    # Data is finite.
    assert np.isfinite(cp).all() and np.isfinite(gp).all()

    # Pulser actually fired the requested number of pulses.
    assert len(exp.pulse_times) == cfg.pulse.n_pulses


def test_pulse_times_match_period() -> None:
    cfg = _fast_cfg(n_pulses=5)
    exp = CtExperiment(MockMFIA(), cfg)
    list(exp.run())
    times = exp.pulse_times
    assert len(times) == 5

    # Inter-pulse spacing should be close to period_s (allow software jitter).
    diffs = np.diff(times)
    assert np.allclose(diffs, cfg.pulse.period_s, atol=0.02)


def test_external_sync_collects_edges_from_chunks() -> None:
    cfg = _fast_cfg(n_pulses=4)
    cfg.pulse.source = PulseSource.EXTERNAL
    exp = CtExperiment(MockMFIA(), cfg)
    chunks = list(exp.run())

    edges = [e for c in chunks for e in c.pulse_edges_s]
    assert len(edges) == cfg.pulse.n_pulses
    assert edges == exp.pulse_times
    # External-sync edges are scheduled at exactly i * period in the mock.
    expected = [i * cfg.pulse.period_s for i in range(cfg.pulse.n_pulses)]
    assert np.allclose(edges, expected)


def test_fg_call_sequence_in_external_mode() -> None:
    cfg = _fast_cfg(n_pulses=3)
    cfg.pulse.source = PulseSource.EXTERNAL
    cfg.fg.enabled = True
    cfg.fg.high_v = 4.0
    cfg.fg.low_v = 0.0
    cfg.fg.load_ohms = "INF"

    fg = MockFunctionGenerator()
    exp = CtExperiment(MockMFIA(), cfg, fg=fg)
    list(exp.run())

    names = [c.name for c in fg.calls]
    assert names[:4] == ["connect", "reset", "configure_pulse_burst", "trigger"]
    assert names[-2:] == ["disarm", "disconnect"]
    assert fg.trigger_count == 1

    sent = fg.last_config
    assert sent is not None
    assert sent["freq_hz"] == 1.0 / cfg.pulse.period_s
    assert sent["width_s"] == cfg.pulse.pulse_width_s
    assert sent["n_cycles"] == cfg.pulse.n_pulses
    assert sent["high_v"] == cfg.fg.high_v
    assert sent["load_ohms"] == cfg.fg.load_ohms


def test_fg_skipped_when_disabled() -> None:
    cfg = _fast_cfg(n_pulses=2)
    cfg.pulse.source = PulseSource.EXTERNAL
    cfg.fg.enabled = False

    fg = MockFunctionGenerator()
    exp = CtExperiment(MockMFIA(), cfg, fg=fg)
    list(exp.run())

    # FG was passed in but not enabled → nothing should have been written.
    assert fg.calls == []


def test_led_pulse_source_drives_wavelength_channel() -> None:
    cfg = _fast_cfg(n_pulses=3)
    cfg.pulse.source = PulseSource.LED_8CH
    cfg.pulse.led_wavelength_nm = 470.0
    cfg.pulse.led_intensity_pct = 60.0
    led = MockLedSource()
    exp = CtExperiment(MockMFIA(), cfg, led=led)
    chunks = list(exp.run())

    assert len(chunks) > 0
    # One pulse per requested pulse, fired on the 470 nm channel at 60 %.
    assert len(exp.pulse_times) == cfg.pulse.n_pulses
    on = [c for c in led.calls if c.name == "set_intensity"]
    assert len(on) == cfg.pulse.n_pulses
    assert all(c.kwargs == {"nm": 470.0, "pct": 60.0} for c in on)
    # Connected at start, disconnected at teardown.
    assert led.calls[0].name == "connect"
    assert led.calls[-1].name == "disconnect"
    # Each pulse drops the LED back off (all_off) at least n times + teardown.
    assert sum(1 for c in led.calls if c.name == "all_off") >= cfg.pulse.n_pulses


def test_led_pulse_source_requires_led() -> None:
    cfg = _fast_cfg(n_pulses=2)
    cfg.pulse.source = PulseSource.LED_8CH
    exp = CtExperiment(MockMFIA(), cfg, led=None)
    try:
        list(exp.run())
    except RuntimeError as e:
        assert "LED" in str(e)
    else:
        raise AssertionError("expected RuntimeError when LED mode has no LED source")


def test_save_run_roundtrip(tmp_path) -> None:
    cfg = _fast_cfg(n_pulses=3)
    exp = CtExperiment(MockMFIA(), cfg)
    chunks = list(exp.run())
    t = np.concatenate([c.t for c in chunks])
    cp = np.concatenate([c.cp for c in chunks])
    gp = np.concatenate([c.gp for c in chunks])

    out = save_run(tmp_path / "run.h5", cfg, t, cp, gp, exp.pulse_times)

    with h5py.File(out, "r") as f:
        assert f.attrs["n_samples"] == t.size
        assert f.attrs["n_pulses"] == len(exp.pulse_times)
        assert f["stream/t"].shape == t.shape
        assert f["pulse_times"].shape == (len(exp.pulse_times),)
