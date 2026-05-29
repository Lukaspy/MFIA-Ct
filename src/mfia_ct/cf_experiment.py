"""C-f campaign orchestrator: bias × illumination × frequency-sweep loops.

Loop order matches the campaign spec — bias outer, illumination inner:

    for bias in cfg.bias.values_v:
        set_dc_bias(bias)
        hold(bias_settle_s)
        for step in cfg.illumination.steps:
            set_led(step)
            hold(step.settle_s)
            (freq, z_real, z_imag) = backend.run_sweep(cfg.sweep)
            yield SweepResult(...)
            # storage happens in the worker, not here, so the experiment
            # stays mock-testable without touching disk

Single-threaded by design — the zhinst client isn't thread-safe and the
Sweeper module is a long-blocking call that already pumps its own
progress. The worker thread in the GUI just iterates ``run()``.

If illuminated steps are configured but no LED source is attached, the
experiment refuses to start with a clear error. Dark-only campaigns
(illumination_sequence has only dark steps) run without an LED source.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterator, Optional, Protocol

import numpy as np

from .cf_config import CfConfig, IlluminationStep
from .cf_storage import SweepResult, make_metadata_from_config
from .led_source import LedSource


class _Backend(Protocol):
    device: str

    def configure_impedance_for_cf(self, cfg: CfConfig) -> None: ...
    def set_dc_bias(self, bias_v: float) -> None: ...
    def run_sweep(
        self,
        cfg,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...


# Progress payload emitted up to the GUI: (bias_idx, step_idx, point_idx_frac).
# point_idx_frac is the 0..1 sweep progress within the current point.
ProgressCallback = Callable[[int, int, float], None]


class CfExperiment:
    def __init__(
        self,
        backend: _Backend,
        cfg: CfConfig,
        led: Optional[LedSource] = None,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        sleeper: Optional[Callable[[float, Callable[[], bool]], None]] = None,
    ) -> None:
        self.backend = backend
        self.cfg = cfg
        self.led = led
        self._progress_cb = progress_cb
        # Tests inject ``sleeper`` to skip the 90 s bias holds; default sleeps
        # in 0.25 s slices so stop() is responsive.
        self._sleeper = sleeper or _default_sleeper
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _stopped(self) -> bool:
        return self._stop.is_set()

    def _requires_led(self) -> bool:
        return any(not s.is_dark for s in self.cfg.illumination.steps)

    def run(self) -> Iterator[SweepResult]:
        if self._requires_led() and self.led is None:
            raise RuntimeError(
                "Illumination steps are configured but no LED source was "
                "attached. Pass led=... or drop the lit steps."
            )

        self.backend.configure_impedance_for_cf(self.cfg)
        led_connected = False
        try:
            if self.led is not None:
                self.led.connect()
                led_connected = True

            for bi, bias in enumerate(self.cfg.bias.values_v):
                if self._stopped():
                    return
                self.backend.set_dc_bias(bias)
                self._sleeper(self.cfg.bias.bias_settle_s, self._stopped)
                if self._stopped():
                    return

                for si, step in enumerate(self.cfg.illumination.steps):
                    if self._stopped():
                        return
                    self._apply_illumination(step)
                    self._sleeper(step.settle_s, self._stopped)
                    if self._stopped():
                        return

                    def _per_point(frac: float, bi=bi, si=si) -> None:
                        if self._progress_cb is not None:
                            try:
                                self._progress_cb(bi, si, frac)
                            except Exception:
                                pass

                    freq, z_real, z_imag = self.backend.run_sweep(
                        self.cfg.sweep,
                        progress_cb=_per_point,
                        stop_check=self._stopped,
                        light_active=not step.is_dark,
                    )
                    current_ma = self._current_ma_for(step)
                    power_mw = self._optical_power_for(step)
                    meta = make_metadata_from_config(
                        self.cfg,
                        bias,
                        step,
                        led_current_ma=current_ma,
                        optical_power_mw=power_mw,
                    )
                    yield SweepResult(
                        frequency_hz=freq,
                        z_real=z_real,
                        z_imag=z_imag,
                        metadata=meta,
                    )
        finally:
            # Always: LEDs off, bias to 0, LED disconnect. Don't propagate
            # cleanup errors; they'd mask the original exception.
            try:
                if self.led is not None:
                    self.led.all_off()
            except Exception:
                pass
            try:
                self.backend.set_dc_bias(0.0)
            except Exception:
                pass
            if led_connected and self.led is not None:
                try:
                    self.led.disconnect()
                except Exception:
                    pass

    def _apply_illumination(self, step: IlluminationStep) -> None:
        if self.led is None:
            return
        if step.is_dark:
            self.led.all_off()
            return
        assert step.wavelength_nm is not None
        self.led.set_intensity(step.wavelength_nm, step.intensity_pct)

    def _current_ma_for(self, step: IlluminationStep) -> Optional[float]:
        """Best-effort actual drive current for the metadata, or None."""
        if self.led is None or step.is_dark or step.wavelength_nm is None:
            return None
        fn = getattr(self.led, "current_ma_for", None)
        if fn is None:
            return None
        try:
            return fn(step.wavelength_nm, step.intensity_pct)
        except Exception:
            return None

    def _optical_power_for(self, step: IlluminationStep) -> Optional[float]:
        """Calibration-predicted delivered optical power (mW) for the metadata,
        so each sweep carries the power the analysis side needs for photon-flux
        normalization. None for dark steps or uncalibrated channels.
        """
        if self.led is None or step.is_dark or step.wavelength_nm is None:
            return None
        fn = getattr(self.led, "predicted_power_mw", None)
        if fn is None:
            return None
        try:
            return fn(step.wavelength_nm, step.intensity_pct)
        except Exception:
            return None


def _default_sleeper(seconds: float, stopped: Callable[[], bool]) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stopped():
            return
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
