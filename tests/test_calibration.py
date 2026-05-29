"""Tests for automated LED power calibration and the led_driver limit fix."""

from __future__ import annotations

import pytest

from mfia_ct.led_calibration import CalPoint, CalSpec, calibrate, save_calibration
from mfia_ct.led_source import MockLedSource
from mfia_ct.pm16 import MockPowerMeter


def _no_sleep(_s: float) -> None:
    return


def test_calibrate_builds_monotonic_curves() -> None:
    led = MockLedSource()
    led.connect()
    meter = MockPowerMeter(led=led)
    meter.connect()
    spec = CalSpec(
        wavelengths_nm=[470.0, 590.0],
        drive_points_pct=[0.0, 50.0, 100.0],
        settle_s=0.0,
        meter_averages=1,
    )
    curves = calibrate(led, meter, spec, sleeper=_no_sleep)

    assert set(curves.keys()) == {470.0, 590.0}
    for nm, pts in curves.items():
        assert [p.drive_pct for p in pts] == [0.0, 50.0, 100.0]
        powers = [p.power_mw for p in pts]
        assert powers[0] == 0.0  # 0% drive → LED off → no power
        assert powers[0] < powers[1] < powers[2]  # monotonic
    # 470 nm is brighter than 590 nm in the mock model — exercises equalization.
    assert curves[470.0][-1].power_mw > curves[590.0][-1].power_mw
    # LED ends dark after the sweep.
    assert led.active == {}


def test_calibrate_callbacks_fire() -> None:
    led = MockLedSource()
    led.connect()
    meter = MockPowerMeter(led=led)
    meter.connect()
    spec = CalSpec(
        wavelengths_nm=[505.0],
        drive_points_pct=[0.0, 100.0],
        settle_s=0.0,
        meter_averages=1,
    )
    points: list[CalPoint] = []
    progress: list[tuple] = []
    calibrate(
        led,
        meter,
        spec,
        point_cb=points.append,
        progress_cb=lambda wi, pi, nm, d: progress.append((wi, pi, nm, d)),
        sleeper=_no_sleep,
    )
    assert len(points) == 2
    assert len(progress) == 2
    assert progress[0] == (0, 0, 505.0, 0.0)


def test_calibrate_stop_aborts_early() -> None:
    led = MockLedSource()
    led.connect()
    meter = MockPowerMeter(led=led)
    meter.connect()
    spec = CalSpec(
        wavelengths_nm=[470.0, 590.0, 625.0],
        drive_points_pct=[0.0, 100.0],
        settle_s=0.0,
        meter_averages=1,
    )
    seen = {"n": 0}

    def stop() -> bool:
        seen["n"] += 1
        return seen["n"] > 2  # stop after a couple of checks

    curves = calibrate(led, meter, spec, stop_check=stop, sleeper=_no_sleep)
    # Should not have completed all three wavelengths.
    assert len(curves) < 3


def test_save_calibration_roundtrip(tmp_path) -> None:
    pytest.importorskip("led_driver")
    from led_driver.calibration import CalibrationManager

    led = MockLedSource()
    led.connect()
    meter = MockPowerMeter(led=led)
    meter.connect()
    spec = CalSpec(
        wavelengths_nm=[470.0, 590.0],
        drive_points_pct=[0.0, 50.0, 100.0],
        settle_s=0.0,
        meter_averages=1,
    )
    curves = calibrate(led, meter, spec, sleeper=_no_sleep)

    cal_path = tmp_path / "calibration.json"
    save_calibration(curves, led, cal_path=cal_path, equalize=True, merge=False)
    assert cal_path.exists()

    mgr = CalibrationManager.load(cal_path)
    assert mgr.equalize is True
    # MockLedSource maps 470 nm to its index in DEFAULT_WAVELENGTHS_NM.
    ch470 = led.channel_of(470.0)
    cal = mgr.channels[ch470]
    assert cal.is_calibrated
    assert len(cal.points) == 3
    # Points are (drive%, power_mW), sorted by drive.
    drives = [p[0] for p in cal.points]
    assert drives == [0.0, 50.0, 100.0]


def test_led_driver_limit_keyed_by_wavelength() -> None:
    """The led_driver fix: 590 nm carries its 700 mA limit by wavelength,
    independent of channel index / config order.
    """
    pytest.importorskip("led_driver")
    from led_driver.config import hw_defaults_for_wavelength

    assert hw_defaults_for_wavelength(590.0) == (750.0, 700.0)
    assert hw_defaults_for_wavelength(530.0) == (1000.0, None)
    assert hw_defaults_for_wavelength(None) == (1000.0, None)
