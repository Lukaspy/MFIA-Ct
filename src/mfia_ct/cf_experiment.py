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

from .cf_config import CfConfig, IlluminationStep, SweepType
from .cf_storage import (
    IvResult,
    SweepResult,
    make_cv_metadata_from_config,
    make_iv_metadata_from_config,
    make_metadata_from_config,
)
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
    def run_bias_sweep(
        self,
        cv,
        fixed_freq_hz: float,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...

    # I-V is optional — only the B1500 backend implements it. A backend
    # without this method can still run C-f / C-V; the experiment only calls
    # it for an I_V-type config.
    def run_iv_sweep(
        self,
        iv,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]: ...


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

    def _instrument(self) -> str:
        """Analyzer identity for the metadata ('MFIA' / 'B1500')."""
        return getattr(self.backend, "instrument", "MFIA")

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

            if self.cfg.sweep_type == SweepType.C_V:
                yield from self._run_cv()
            elif self.cfg.sweep_type == SweepType.I_V:
                yield from self._run_iv()
            else:
                yield from self._run_cf()
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

    def _run_cf(self) -> Iterator[SweepResult]:
        """C-f: bias outer, illumination inner, frequency the swept axis."""
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
                meta = make_metadata_from_config(
                    self.cfg,
                    bias,
                    step,
                    led_current_ma=self._current_ma_for(step),
                    optical_power_mw=self._optical_power_for(step),
                    instrument=self._instrument(),
                )
                yield SweepResult(
                    frequency_hz=freq,
                    z_real=z_real,
                    z_imag=z_imag,
                    metadata=meta,
                )

    def _run_cv(self) -> Iterator[SweepResult]:
        """C-V: test-frequency outer, illumination inner, bias the swept axis.

        The illumination sequence is the same "plan" as C-f, so dark-pre /
        lit / dark-post runs at each frequency just as it does at each bias.
        A single-entry ``cv_frequencies_hz`` is an ordinary single-frequency
        C-V; multiple entries produce a C-V-f map.
        """
        for fi, freq in enumerate(self.cfg.cv_frequencies_hz):
            if self._stopped():
                return

            for si, step in enumerate(self.cfg.illumination.steps):
                if self._stopped():
                    return
                self._apply_illumination(step)
                self._sleeper(step.settle_s, self._stopped)
                if self._stopped():
                    return

                def _per_point(frac: float, fi=fi, si=si) -> None:
                    if self._progress_cb is not None:
                        try:
                            self._progress_cb(fi, si, frac)
                        except Exception:
                            pass

                bias_v, z_real, z_imag = self.backend.run_bias_sweep(
                    self.cfg.cv_bias,
                    float(freq),
                    progress_cb=_per_point,
                    stop_check=self._stopped,
                    light_active=not step.is_dark,
                )
                meta = make_cv_metadata_from_config(
                    self.cfg,
                    float(freq),
                    step,
                    led_current_ma=self._current_ma_for(step),
                    optical_power_mw=self._optical_power_for(step),
                    instrument=self._instrument(),
                )
                # frequency_hz broadcast to the bias grid so the Cp/Rp ω-math
                # in SweepResult stays identical to the C-f path.
                freq_arr = np.full(np.asarray(bias_v).shape, float(freq))
                yield SweepResult(
                    frequency_hz=freq_arr,
                    z_real=z_real,
                    z_imag=z_imag,
                    bias_v=bias_v,
                    sweep_type="C-V",
                    metadata=meta,
                )

    def _run_iv(self) -> Iterator[IvResult]:
        """I-V: illumination is the only loop; the SMU sweeps voltage itself.

        Mirrors the inner loop of ``_run_cv`` — the same illumination / LED /
        dark-bracket machinery — but the swept axis is the SMU voltage
        staircase and the measured quantity is current, so one I-V *curve* is
        produced per illumination step (no bias / frequency outer loop). The
        photo-response falls straight out of the dark-vs-lit curves, exactly
        like the C-f/C-V action spectrum. Only a backend with ``run_iv_sweep``
        (the B1500) can serve this; a config reaches here only when it is an
        I_V-type block, so the plan/GUI never route I-V to the MFIA.
        """
        if not hasattr(self.backend, "run_iv_sweep"):
            raise RuntimeError(
                "This backend has no SMU — I-V sweeps need the B1500. Select "
                "the B1500 instrument (the MFIA cannot measure DC I-V)."
            )
        for si, step in enumerate(self.cfg.illumination.steps):
            if self._stopped():
                return
            self._apply_illumination(step)
            self._sleeper(step.settle_s, self._stopped)
            if self._stopped():
                return

            def _per_point(frac: float, si=si) -> None:
                if self._progress_cb is not None:
                    try:
                        self._progress_cb(0, si, frac)
                    except Exception:
                        pass

            bias_v, current_a = self.backend.run_iv_sweep(
                self.cfg.iv,
                progress_cb=_per_point,
                stop_check=self._stopped,
                light_active=not step.is_dark,
            )
            meta = make_iv_metadata_from_config(
                self.cfg,
                step,
                led_current_ma=self._current_ma_for(step),
                optical_power_mw=self._optical_power_for(step),
                instrument=self._instrument(),
            )
            yield IvResult(
                voltage_v=bias_v,
                current_a=current_a,
                metadata=meta,
            )

    def _apply_illumination(self, step: IlluminationStep) -> None:
        # Drop the drive for lit steps (and restore it for dark) when a separate
        # light amplitude is configured. Set before the sweep regardless of LED.
        if self.cfg.ia.light_ac_amplitude_v is not None:
            self.backend.set_amplitude(self.cfg.ia.amplitude_for(step.is_dark))
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
