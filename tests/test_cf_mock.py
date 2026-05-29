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
    CfConfig,
    IlluminationSequence,
    IlluminationStep,
    RunMetadata,
    SweeperSettings,
)
from mfia_ct.cf_experiment import CfExperiment
from mfia_ct.cf_storage import (
    CSV_COLUMNS,
    SweepResult,
    make_filename,
    make_metadata_from_config,
    write_sweep_csv,
)
from mfia_ct.config import TERMINAL_BIAS_LIMIT_V, TerminalMode
from mfia_ct.led_source import MightexStub, MockLedSource
from mfia_ct.mock_hardware import MockMFIA


def _fast_cfg(*, with_light: bool = True) -> CfConfig:
    """Minimal CfConfig that runs quickly under the mock backend."""
    illum_steps: list[IlluminationStep] = [
        IlluminationStep("dark_pre", None, 0.0, 0.0, settle_s=0.0),
    ]
    if with_light:
        illum_steps.append(
            IlluminationStep("385nm", 0, 385.0, current_ma=20.0, settle_s=0.0)
        )
        illum_steps.append(
            IlluminationStep("dark_post_385", None, 0.0, 0.0, settle_s=0.0)
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
    # The 385 nm step should set current before enabling — never the other way.
    seen_385_current = False
    for c in led.calls:
        if c.name == "set_channel_current" and c.kwargs == {
            "channel": 0,
            "current_ma": 20.0,
        }:
            seen_385_current = True
        if c.name == "enable_channel" and c.kwargs == {"channel": 0}:
            assert seen_385_current, "enable_channel fired before set_channel_current"
    # Disable-all must fire for every dark step.
    n_disable_all = sum(1 for c in led.calls if c.name == "disable_all")
    n_dark_steps = sum(1 for s in cfg.illumination.steps if s.is_dark) * len(cfg.bias)
    # +1 for the final cleanup disable_all in finally.
    assert n_disable_all == n_dark_steps + 1


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


def test_mightex_stub_explains_itself() -> None:
    stub = MightexStub()
    try:
        stub.connect()
    except NotImplementedError as e:
        msg = str(e)
        assert "Mightex" in msg
        assert "model number" in msg or "not yet" in msg.lower()
    else:
        raise AssertionError("MightexStub.connect should raise NotImplementedError")


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
