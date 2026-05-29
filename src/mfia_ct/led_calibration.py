"""Automated LED power calibration using the PM16 meter.

Replaces the manual "type the power reading" workflow in the led_driver
GUI: for each selected wavelength, set the meter's wavelength correction,
step the LED drive through a set of percentages, settle, and read averaged
power. The resulting (drive%, power_mW) curves are persisted in the
led_driver calibration format (``~/.config/led_driver/calibration.json``,
keyed by channel index) so ``LEDController(use_cal=True)`` — and mfia-cf's
"apply power calibration" option — pick them up directly.

The core ``calibrate()`` is hardware-agnostic (any LedSource + PowerMeter),
so it runs end-to-end against the mocks in tests. ``save_calibration()``
does the led_driver-format persistence and is the only part that touches
that package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .led_source import LedSource
from .pm16 import PowerMeter

# Standard drive points — endpoints plus quartiles. 0% gives the dark/offset
# reading; 100% sets each channel's max (and the equalization ceiling).
DEFAULT_DRIVE_POINTS = [0.0, 25.0, 50.0, 75.0, 100.0]


@dataclass
class CalSpec:
    """What to calibrate and how."""

    wavelengths_nm: list[float]
    drive_points_pct: list[float] = field(
        default_factory=lambda: list(DEFAULT_DRIVE_POINTS)
    )
    settle_s: float = 2.0        # after each drive change, before reading
    meter_averages: int = 10     # reads averaged per point
    led_warmup_s: float = 0.0    # extra settle on the first point of a channel


@dataclass
class CalPoint:
    wavelength_nm: float
    drive_pct: float
    power_mw: float


# progress: (wavelength_index, point_index, wavelength_nm, drive_pct)
ProgressCallback = Callable[[int, int, float, float], None]


def calibrate(
    led: LedSource,
    meter: PowerMeter,
    spec: CalSpec,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    point_cb: Optional[Callable[[CalPoint], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> dict[float, list[CalPoint]]:
    """Run the calibration sweep. Returns {wavelength_nm: [CalPoint, ...]}.

    ``led`` and ``meter`` must already be connected. The caller owns their
    lifecycle. ``point_cb`` fires with each CalPoint as it's measured (for
    live plotting); ``progress_cb`` fires with the indices for a progress
    bar. ``sleeper`` is injectable so tests can skip the settle waits.
    """
    sleep = sleeper or time.sleep
    curves: dict[float, list[CalPoint]] = {}

    for wi, nm in enumerate(spec.wavelengths_nm):
        if stop_check is not None and stop_check():
            break
        meter.set_wavelength(nm)
        points: list[CalPoint] = []
        for pi, drive in enumerate(spec.drive_points_pct):
            if stop_check is not None and stop_check():
                break
            if drive <= 0.0:
                # 0% = dark/offset: turn the channel off rather than drive 0.
                led.all_off()
            else:
                led.set_intensity(nm, drive)
            settle = spec.settle_s + (spec.led_warmup_s if pi == 0 else 0.0)
            if settle > 0:
                sleep(settle)
            power_mw = meter.read_power_mw(
                averages=spec.meter_averages, settle_s=0.0
            )
            point = CalPoint(nm, drive, power_mw)
            points.append(point)
            if point_cb is not None:
                try:
                    point_cb(point)
                except Exception:
                    pass
            if progress_cb is not None:
                try:
                    progress_cb(wi, pi, nm, drive)
                except Exception:
                    pass
        curves[nm] = points
        led.all_off()

    return curves


def save_calibration(
    curves: dict[float, list[CalPoint]],
    led: LedSource,
    *,
    cal_path=None,
    equalize: bool = True,
    enabled: bool = True,
    merge: bool = True,
) -> "object":
    """Persist calibration curves in the led_driver format.

    Maps each wavelength to its channel index via ``led.channel_of`` and
    writes the led_driver ``CalibrationManager`` JSON at ``cal_path``
    (default: led_driver's ``CAL_PATH``). With ``merge=True`` the existing
    on-disk calibration is loaded first so re-calibrating a subset of
    channels doesn't wipe the others.

    Returns the CalibrationManager that was saved. Requires the led_driver
    package to be importable.
    """
    from led_driver import CAL_PATH
    from led_driver.calibration import CalibrationManager

    path = cal_path or CAL_PATH
    mgr = CalibrationManager.load(path) if merge else CalibrationManager()

    for nm, points in curves.items():
        ch = led.channel_of(nm)
        cal = mgr.channels[ch]
        cal.clear()
        for p in points:
            cal.add_point(p.drive_pct, p.power_mw)

    mgr.enabled = enabled
    mgr.equalize = equalize
    mgr.save(path)
    return mgr
