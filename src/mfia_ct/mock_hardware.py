"""Synthetic continuous backend.

Streams Cp(t), Gp(t) with a configurable DC baseline plus photo-transients
that respond to pulse times recorded via ``record_pulse()``. Lets the full
GUI run without a connected MFIA.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np

from .acquisition import StreamChunk
from .config import CtConfig


class MockMFIA:
    device_id = "mock"
    device = "mock"

    def __init__(self, seed: int = 0) -> None:
        self._rng = np.random.default_rng(seed)
        self._cfg: Optional[CtConfig] = None
        self._t_zero_mono: Optional[float] = None
        self._last_emitted_s: float = 0.0
        self._pulses: list[float] = []
        self._pulses_lock = threading.Lock()
        # Device-physics constants for the synthetic transient.
        self._c_dc = 10e-12
        self._g_dc = 1e-9
        self._delta_c = -1e-12  # depletion drop on illumination
        self._delta_g = 5e-9
        self._tau_on_fast = 5e-5
        self._tau_on_slow = 5e-3
        self._a_on = 0.6
        self._tau_off_fast = 3e-4
        self._tau_off_slow = 5e-2
        self._a_off = 0.4

    # zhinst-equivalent interface ---------------------------------------------
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def __enter__(self) -> "MockMFIA":
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def configure_impedance(self, cfg: CtConfig) -> None:
        self._cfg = cfg

    def set_aux_out(self, channel: int, value_v: float) -> None:
        pass

    def clockbase(self) -> float:
        return 60e6

    @property
    def imps_sample_path(self) -> str:
        return "/mock/imps/0/sample"

    # Continuous acquisition --------------------------------------------------
    def start_continuous(self) -> None:
        self._t_zero_mono = time.monotonic()
        self._last_emitted_s = 0.0
        with self._pulses_lock:
            self._pulses = []

    def stop_continuous(self) -> None:
        pass

    def record_pulse(self, t_s: float) -> None:
        """Called by the Pulser whenever Aux Out goes high."""
        with self._pulses_lock:
            self._pulses.append(t_s)

    def poll_continuous(self, length_s: float, timeout_ms: int = 500) -> Optional[StreamChunk]:
        if self._cfg is None or self._t_zero_mono is None:
            return None
        time.sleep(length_s)
        now = time.monotonic() - self._t_zero_mono
        rate = self._cfg.demod.sample_rate_hz
        n_new = int((now - self._last_emitted_s) * rate)
        if n_new <= 0:
            return None
        dt = 1.0 / rate
        t = self._last_emitted_s + np.arange(1, n_new + 1) * dt
        cp, gp = self._synthesize(t)
        self._last_emitted_s = float(t[-1])
        return StreamChunk(t=t, cp=cp, gp=gp)

    def _synthesize(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        assert self._cfg is not None
        cp = np.full_like(t, self._c_dc)
        gp = np.full_like(t, self._g_dc)

        pw = self._cfg.pulse.pulse_width_s
        with self._pulses_lock:
            pulses = list(self._pulses)

        # Approach toward photoinduced steady state during the pulse, then
        # double-exponential recovery once it's over.
        end_approach = 1 - (
            self._a_on * np.exp(-pw / self._tau_on_fast)
            + (1 - self._a_on) * np.exp(-pw / self._tau_on_slow)
        )
        for pt in pulses:
            on = (t >= pt) & (t < pt + pw)
            on_t = t[on] - pt
            approach = 1 - (
                self._a_on * np.exp(-on_t / self._tau_on_fast)
                + (1 - self._a_on) * np.exp(-on_t / self._tau_on_slow)
            )
            cp[on] += self._delta_c * approach
            gp[on] += self._delta_g * approach

            off = t >= pt + pw
            off_t = t[off] - pt - pw
            decay = self._a_off * np.exp(-off_t / self._tau_off_fast) + (
                1 - self._a_off
            ) * np.exp(-off_t / self._tau_off_slow)
            cp[off] += self._delta_c * end_approach * decay
            gp[off] += self._delta_g * end_approach * decay

        # Noise floor scaled to demod TC (toy estimate).
        enbw = 1.0 / (8 * self._cfg.demod.time_constant_s)
        cp += self._rng.normal(0, 1e-18 * np.sqrt(enbw), len(t))
        gp += self._rng.normal(0, 1e-12 * np.sqrt(enbw), len(t))
        return cp, gp
