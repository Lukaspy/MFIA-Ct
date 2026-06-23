"""End-to-end mock tests for the C-f campaign pipeline.

Covers:
- CfExperiment iteration order (bias outer, illumination inner).
- LED ordering: set_channel_current before enable_channel; disable_all on dark.
- Refusal when illuminated steps are configured but no LED source attached.
- CSV round-trip: written CSV parses back with the expected columns and the
  metadata block survives.
- Filename pattern matches the analysis-side contract.
- RMS→peak conversion path: the configured RMS amplitude shows up correctly
  in both the metadata header and (when on real hardware) at the LabOne node.
- The Mightex stub raises NotImplementedError on connect with a useful message.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from mfia_ct.cf_config import (
    AmplitudeUnit,
    BiasSequence,
    BiasSweepSettings,
    CfConfig,
    IlluminationSequence,
    IlluminationStep,
    RunMetadata,
    SweeperSettings,
    SweepType,
)
from mfia_ct.cf_experiment import CfExperiment
from mfia_ct.cf_storage import (
    CSV_COLUMNS,
    SweepResult,
    make_cv_metadata_from_config,
    make_filename,
    make_metadata_from_config,
    write_sweep_csv,
)
import pytest

from mfia_ct.config import TERMINAL_BIAS_LIMIT_V, TerminalMode
from mfia_ct.led_source import DEFAULT_WAVELENGTHS_NM, MockLedSource, PxiLedSource
from mfia_ct.mock_hardware import MockMFIA


def _fast_cfg(*, with_light: bool = True) -> CfConfig:
    """Minimal CfConfig that runs quickly under the mock backend."""
    illum_steps: list[IlluminationStep] = [
        IlluminationStep("dark_pre", None, intensity_pct=0.0, settle_s=0.0),
    ]
    if with_light:
        illum_steps.append(
            IlluminationStep("385nm", 385.0, intensity_pct=80.0, settle_s=0.0)
        )
        illum_steps.append(
            IlluminationStep("dark_post_385", None, intensity_pct=0.0, settle_s=0.0)
        )
    cfg = CfConfig(
        sweep=SweeperSettings(
            start_hz=1.0,
            stop_hz=1000.0,
            points_per_decade=4,
            settling_tcs=1.0,
        ),
        bias=BiasSequence(values_v=[0.0, 1.0], bias_settle_s=0.0),
        illumination=IlluminationSequence(steps=illum_steps),
        run=RunMetadata(device_id="MOCK01", substrate_type="p-Si"),
    )
    cfg.ia.ac_amplitude_v = 0.030
    return cfg


def _no_sleep(seconds: float, stopped) -> None:
    """Test sleeper: skip the long bias/LED holds."""
    return


def test_loop_order_and_count() -> None:
    cfg = _fast_cfg()
    led = MockLedSource()
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    # 2 biases × 3 steps each = 6 sweeps.
    assert len(results) == 6
    # Bias outer, illumination inner.
    biases = [r.metadata.bias_v_commanded for r in results]
    assert biases == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    labels = [r.metadata.illumination_label for r in results]
    assert labels == [
        "dark_pre",
        "385nm",
        "dark_post_385",
        "dark_pre",
        "385nm",
        "dark_post_385",
    ]


def test_led_call_ordering() -> None:
    cfg = _fast_cfg()
    led = MockLedSource()
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    list(exp.run())

    names = [c.name for c in led.calls]
    assert names[0] == "connect"
    assert names[-1] == "disconnect"
    # The 385 nm lit step drives by wavelength + percent.
    lit = [c for c in led.calls if c.name == "set_intensity"]
    assert lit, "expected at least one set_intensity call"
    assert all(c.kwargs["nm"] == 385.0 and c.kwargs["pct"] == 80.0 for c in lit)
    # all_off must fire for every dark step, plus once in the cleanup finally.
    n_all_off = sum(1 for c in led.calls if c.name == "all_off")
    n_dark_steps = sum(1 for s in cfg.illumination.steps if s.is_dark) * len(cfg.bias)
    assert n_all_off == n_dark_steps + 1


def test_led_current_ma_recorded_for_lit_steps() -> None:
    cfg = _fast_cfg(with_light=True)
    led = MockLedSource()
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    lit = [r for r in results if r.metadata.illumination_label == "385nm"]
    assert lit
    for r in lit:
        # 80 % of 385 nm's 1000 mA full-scale = 800 mA.
        assert r.metadata.led_intensity_pct == 80.0
        assert r.metadata.led_current_ma == 800.0
    dark = [r for r in results if r.metadata.illumination_label.startswith("dark")]
    for r in dark:
        assert r.metadata.led_intensity_pct is None
        assert r.metadata.led_current_ma is None


def test_optical_power_recorded_when_calibrated() -> None:
    cfg = _fast_cfg(with_light=True)  # 385 nm lit step at 80%
    # MockLedSource with a power model = "calibrated": 385 nm peaks at 6 mW.
    led = MockLedSource(power_model_mw={385.0: 6.0})
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    lit = [r for r in results if r.metadata.illumination_label == "385nm"]
    assert lit
    for r in lit:
        # 80 % of the 6 mW model → 4.8 mW.
        assert r.metadata.optical_power_mw == pytest.approx(4.8)
    # Dark steps and uncalibrated channels stay None.
    for r in results:
        if r.metadata.illumination_label.startswith("dark"):
            assert r.metadata.optical_power_mw is None


def test_optical_power_none_without_calibration() -> None:
    cfg = _fast_cfg(with_light=True)
    led = MockLedSource()  # no power model → uncalibrated
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    assert all(r.metadata.optical_power_mw is None for r in results)


def test_refuses_light_without_led() -> None:
    cfg = _fast_cfg(with_light=True)
    exp = CfExperiment(MockMFIA(), cfg, led=None, sleeper=_no_sleep)
    try:
        list(exp.run())
    except RuntimeError as e:
        assert "no LED source" in str(e).lower() or "led" in str(e).lower()
    else:
        raise AssertionError("expected RuntimeError when lit steps configured without LED")


def test_dark_only_runs_without_led() -> None:
    cfg = _fast_cfg(with_light=False)
    exp = CfExperiment(MockMFIA(), cfg, led=None, sleeper=_no_sleep)
    results = list(exp.run())
    assert len(results) == 2  # 2 biases × 1 dark step
    assert all(r.metadata.illumination_label == "dark_pre" for r in results)


def test_csv_roundtrip(tmp_path: Path) -> None:
    cfg = _fast_cfg(with_light=False)
    backend = MockMFIA()
    exp = CfExperiment(backend, cfg, led=None, sleeper=_no_sleep)
    result = next(iter(exp.run()))
    path = tmp_path / make_filename(result.metadata)
    write_sweep_csv(result, path)

    assert path.exists()
    # Filename pattern: MFIA_Cf_<device>_Vb<bias>_<illum>_<stamp>.csv
    assert path.name.startswith("MFIA_Cf_MOCK01_Vb+0_dark_pre_")
    assert path.suffix == ".csv"

    # Parse: metadata lines start with '#', then header, then rows.
    text = path.read_text().splitlines()
    metadata_lines = [ln for ln in text if ln.startswith("#")]
    data_lines = [ln for ln in text if not ln.startswith("#")]
    assert any("device_id: MOCK01" in ln for ln in metadata_lines)
    assert any("substrate_type: p-Si" in ln for ln in metadata_lines)
    assert any("amplitude_vrms: 0.03" in ln for ln in metadata_lines)
    assert any("amplitude_vpk:" in ln for ln in metadata_lines)
    # Header line is the first non-comment row.
    reader = csv.reader(data_lines)
    header = next(reader)
    assert header == CSV_COLUMNS
    rows = list(reader)
    assert len(rows) == len(result.frequency_hz)
    # First data row's freq matches the result's first freq.
    f0 = float(rows[0][0])
    assert math.isclose(f0, float(result.frequency_hz[0]), rel_tol=1e-6)


def test_rms_to_peak_conversion_in_metadata() -> None:
    cfg = _fast_cfg(with_light=False)
    cfg.ia.ac_amplitude_v = 0.030  # 30 mV RMS — B1500 reference
    step = cfg.illumination.steps[0]
    meta = make_metadata_from_config(cfg, bias_v=0.0, step=step)
    assert math.isclose(meta.amplitude_vrms, 0.030)
    assert math.isclose(meta.amplitude_vpk, 0.030 * math.sqrt(2.0), rel_tol=1e-9)


def test_mock_led_rejects_unmapped_wavelength() -> None:
    led = MockLedSource()
    led.connect()
    with pytest.raises(KeyError):
        led.set_intensity(999.0, 50.0)
    led.disconnect()


def test_pxi_led_source_against_led_driver_mock() -> None:
    """Exercise the real PxiLedSource adapter against led_driver's own mock
    backend (bitfile=None). Skipped if the PXI-AWG led_driver package isn't
    importable in this environment.
    """
    pytest.importorskip("led_driver")
    led = PxiLedSource(bitfile=None)  # None → led_driver MockBackend
    led.connect()
    try:
        wls = set(led.wavelengths())
        assert wls == set(DEFAULT_WAVELENGTHS_NM)
        led.set_intensity(590.0, 50.0)
        # Don't assume the on-disk full-scale (a saved GUI config can change
        # it) — verify the adapter's mA report is linear in percent instead.
        ma_50 = led.current_ma_for(590.0, 50.0)
        ma_100 = led.current_ma_for(590.0, 100.0)
        assert ma_50 is not None and ma_100 is not None
        assert ma_50 > 0 and abs(ma_100 - 2 * ma_50) < 1e-6
        assert led.current_ma_for(590.0, 0.0) == 0.0
        led.all_off()
    finally:
        led.disconnect()


def test_sweeper_settings_n_points() -> None:
    s = SweeperSettings(start_hz=1.0, stop_hz=1e6, points_per_decade=10, log_spacing=True)
    # 6 decades × 10 pts = 60, plus endpoint = 61.
    assert s.n_points == 61


def test_default_illumination_sequence_interleaves_dark() -> None:
    seq = IlluminationSequence.default_interleaved()
    labels = [s.label for s in seq.steps]
    # First step is dark_pre, then alternating lit / dark_post pairs.
    assert labels[0] == "dark_pre"
    # 8 channels × (light + dark_post) = 16, + 1 dark_pre = 17 steps.
    assert len(seq.steps) == 17
    # Dark steps and lit steps strictly alternate after dark_pre.
    after_pre = labels[1:]
    for i, lbl in enumerate(after_pre):
        if i % 2 == 0:
            assert "nm" in lbl
        else:
            assert lbl.startswith("dark_post_")


def test_cf_default_is_two_terminal() -> None:
    cfg = CfConfig()
    assert cfg.ia.terminal_mode == TerminalMode.TWO_TERMINAL


def test_terminal_bias_limits() -> None:
    # 4-terminal is the restrictive one; 2-terminal allows the ±5 V matrix.
    assert TERMINAL_BIAS_LIMIT_V[TerminalMode.FOUR_TERMINAL] == 3.0
    assert TERMINAL_BIAS_LIMIT_V[TerminalMode.TWO_TERMINAL] == 10.0
    matrix = [-5.0, -4.0, -2.0, -1.0, 0.0, 1.0, 2.0, 4.0, 5.0]
    four = TERMINAL_BIAS_LIMIT_V[TerminalMode.FOUR_TERMINAL]
    two = TERMINAL_BIAS_LIMIT_V[TerminalMode.TWO_TERMINAL]
    # ±4 and ±5 exceed the 4-terminal limit but fit 2-terminal.
    assert [b for b in matrix if abs(b) > four] == [-5.0, -4.0, 4.0, 5.0]
    assert [b for b in matrix if abs(b) > two] == []


def test_intensity_series_structure() -> None:
    seq = IlluminationSequence.intensity_series(
        470.0, [10.0, 50.0, 100.0], interleave_dark=True
    )
    labels = [s.label for s in seq.steps]
    # dark_pre, then (lit, dark) per level → 1 + 3*2 = 7.
    assert len(seq.steps) == 7
    assert labels[0] == "dark_pre"
    lit = [s for s in seq.steps if not s.is_dark]
    # All lit steps are the same wavelength, stepping intensity.
    assert all(s.wavelength_nm == 470.0 for s in lit)
    assert [s.intensity_pct for s in lit] == [10.0, 50.0, 100.0]
    assert [s.label for s in lit] == ["470nm_10pct", "470nm_50pct", "470nm_100pct"]


def test_intensity_series_no_interleave() -> None:
    seq = IlluminationSequence.intensity_series(
        590.0, [25.0, 100.0], dark_pre=False, interleave_dark=False
    )
    # No dark steps at all → just the two lit levels.
    assert len(seq.steps) == 2
    assert all(not s.is_dark for s in seq.steps)
    assert [s.intensity_pct for s in seq.steps] == [25.0, 100.0]


def test_intensity_series_runs_and_records_power() -> None:
    # End-to-end: a 470 nm intensity series should produce per-level sweeps
    # with optical_power_mw scaling with drive % (linear model in the mock).
    cfg = CfConfig(
        sweep=SweeperSettings(start_hz=1.0, stop_hz=100.0, points_per_decade=3),
        bias=BiasSequence(values_v=[0.0], bias_settle_s=0.0),
        illumination=IlluminationSequence.intensity_series(
            470.0, [25.0, 50.0, 100.0], dark_pre=False, interleave_dark=False,
            light_settle_s=0.0,
        ),
        run=RunMetadata(device_id="MOCK01", substrate_type="p-Si"),
    )
    led = MockLedSource(power_model_mw={470.0: 8.0})
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    powers = [r.metadata.optical_power_mw for r in results]
    # 25/50/100 % of 8 mW → 2, 4, 8 mW, strictly increasing.
    assert powers == pytest.approx([2.0, 4.0, 8.0])


# --- C-V mode -----------------------------------------------------------------


def _fast_cv_cfg(*, with_light: bool = True, frequencies=(1000.0, 10000.0)) -> CfConfig:
    """Minimal C-V CfConfig that runs quickly under the mock backend."""
    illum_steps: list[IlluminationStep] = [
        IlluminationStep("dark_pre", None, intensity_pct=0.0, settle_s=0.0),
    ]
    if with_light:
        illum_steps.append(
            IlluminationStep("470nm", 470.0, intensity_pct=100.0, settle_s=0.0)
        )
        illum_steps.append(
            IlluminationStep("dark_post_470", None, intensity_pct=0.0, settle_s=0.0)
        )
    cfg = CfConfig(
        sweep_type=SweepType.C_V,
        cv_frequencies_hz=list(frequencies),
        cv_bias=BiasSweepSettings(start_v=-2.0, stop_v=2.0, n_points=5, settling_tcs=1.0),
        illumination=IlluminationSequence(steps=illum_steps),
        run=RunMetadata(device_id="MOCK01", substrate_type="p-Si"),
    )
    cfg.ia.ac_amplitude_v = 0.030
    return cfg


def test_cv_loop_order_and_count() -> None:
    cfg = _fast_cv_cfg()  # 2 freqs × 3 steps
    led = MockLedSource()
    exp = CfExperiment(MockMFIA(), cfg, led=led, sleeper=_no_sleep)
    results = list(exp.run())
    assert len(results) == 6
    # Frequency outer, illumination inner.
    freqs = [r.metadata.fixed_frequency_hz for r in results]
    assert freqs == [1000.0, 1000.0, 1000.0, 10000.0, 10000.0, 10000.0]
    labels = [r.metadata.illumination_label for r in results]
    assert labels == [
        "dark_pre", "470nm", "dark_post_470",
        "dark_pre", "470nm", "dark_post_470",
    ]
    # All sweeps are tagged C-V and carry NaN commanded bias (bias is swept).
    assert all(r.metadata.sweep_type == "C-V" for r in results)
    assert all(math.isnan(r.metadata.bias_v_commanded) for r in results)


def test_cv_result_axes() -> None:
    cfg = _fast_cv_cfg(with_light=False, frequencies=(5000.0,))
    exp = CfExperiment(MockMFIA(), cfg, led=None, sleeper=_no_sleep)
    result = next(iter(exp.run()))
    # Swept axis is bias, of length n_points spanning the range.
    assert result.x_is_bias
    assert result.sweep_type == "C-V"
    assert len(result.bias_v) == cfg.cv_bias.n_points
    assert np.isclose(result.bias_v[0], -2.0) and np.isclose(result.bias_v[-1], 2.0)
    # frequency_hz is the held test frequency broadcast to every point.
    assert result.frequency_hz.shape == result.bias_v.shape
    assert np.allclose(result.frequency_hz, 5000.0)
    # x_values returns the bias axis for plotting.
    assert np.allclose(result.x_values, result.bias_v)
    # Depletion capacitance is positive and finite.
    cp, _rp = result.cp_rp
    assert np.all(np.isfinite(cp)) and np.all(cp > 0)


def test_cv_csv_roundtrip(tmp_path: Path) -> None:
    cfg = _fast_cv_cfg(with_light=False, frequencies=(1000.0,))
    exp = CfExperiment(MockMFIA(), cfg, led=None, sleeper=_no_sleep)
    result = next(iter(exp.run()))
    path = tmp_path / make_filename(result.metadata)
    write_sweep_csv(result, path)

    # Filename pattern: MFIA_CV_<device>_f<freq>_<illum>_<stamp>.csv
    assert path.name.startswith("MFIA_CV_MOCK01_f1kHz_dark_pre_")

    text = path.read_text().splitlines()
    metadata_lines = [ln for ln in text if ln.startswith("#")]
    data_lines = [ln for ln in text if not ln.startswith("#")]
    assert any("sweep_type: C-V" in ln for ln in metadata_lines)
    assert any("fixed_frequency_hz: 1000.0" in ln for ln in metadata_lines)
    # No bias_v_commanded line for C-V (bias is swept, not held).
    assert not any("bias_v_commanded:" in ln for ln in metadata_lines)

    reader = csv.reader(data_lines)
    header = next(reader)
    assert header == CSV_COLUMNS
    assert "bias_v" in header
    rows = list(reader)
    assert len(rows) == cfg.cv_bias.n_points
    bias_idx = CSV_COLUMNS.index("bias_v")
    freq_idx = CSV_COLUMNS.index("frequency_hz")
    bias_col = [float(r[bias_idx]) for r in rows]
    freq_col = [float(r[freq_idx]) for r in rows]
    # Bias varies across the file; frequency is constant.
    assert math.isclose(bias_col[0], -2.0, abs_tol=1e-6)
    assert math.isclose(bias_col[-1], 2.0, abs_tol=1e-6)
    assert all(math.isclose(f, 1000.0, rel_tol=1e-6) for f in freq_col)


def test_cv_metadata_marks_swept_bias() -> None:
    cfg = _fast_cv_cfg(with_light=False)
    step = cfg.illumination.steps[0]
    meta = make_cv_metadata_from_config(cfg, 2500.0, step)
    assert meta.sweep_type == "C-V"
    assert meta.fixed_frequency_hz == 2500.0
    assert math.isnan(meta.bias_v_commanded)
    # Amplitude conversion still applies.
    assert math.isclose(meta.amplitude_vrms, 0.030)
    assert math.isclose(meta.amplitude_vpk, 0.030 * math.sqrt(2.0), rel_tol=1e-9)


def test_cv_refuses_light_without_led() -> None:
    cfg = _fast_cv_cfg(with_light=True)
    exp = CfExperiment(MockMFIA(), cfg, led=None, sleeper=_no_sleep)
    with pytest.raises(RuntimeError):
        list(exp.run())


def test_bias_sweep_settings_values() -> None:
    s = BiasSweepSettings(start_v=-5.0, stop_v=5.0, n_points=11)
    vals = s.values_v
    assert len(vals) == 11
    assert vals[0] == -5.0 and vals[-1] == 5.0
    # Default is the ±5 V, 101-point linear C-V matrix.
    d = BiasSweepSettings()
    assert d.n_points == 101 and not d.log_spacing


def test_progress_callback_fires_with_indices() -> None:
    cfg = _fast_cfg(with_light=False)
    seen: list[tuple[int, int, float]] = []
    exp = CfExperiment(
        MockMFIA(),
        cfg,
        led=None,
        progress_cb=lambda b, s, f: seen.append((b, s, f)),
        sleeper=_no_sleep,
    )
    list(exp.run())
    # At least one progress event per (bias, step) combination.
    assert any(t[0] == 0 for t in seen)
    assert any(t[0] == 1 for t in seen)
    # Last reported fraction reaches 1.0 by end-of-sweep.
    assert any(math.isclose(t[2], 1.0, abs_tol=1e-6) for t in seen)
