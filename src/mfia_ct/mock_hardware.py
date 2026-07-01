"""Synthetic continuous backend.

Streams Cp(t), Gp(t) with a configurable DC baseline plus photo-transients
that respond to pulse times recorded via ``record_pulse()``. Lets the full
GUI run without a connected MFIA.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

import numpy as np

from .acquisition import StreamChunk
from .cf_config import BiasSweepSettings, CfConfig, SweeperSettings
from .config import CtConfig


class MockMFIA:
    device_id = "mock"
    device = "mock"
    instrument = "MFIA"

    def __init__(self, seed: int = 0) -> None:
        self._rng = np.random.default_rng(seed)
        self._cfg: Optional[CtConfig] = None
        self._t_zero_mono: Optional[float] = None
        self._last_emitted_s: float = 0.0
        self._pulses: list[float] = []
        self._pulses_lock = threading.Lock()
        self._external_sync: bool = False
        self._next_external_pulse: int = 0
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
    def start_continuous(
        self,
        *,
        external_sync: bool = False,
        sync_aux_in_channel: int = 0,
        sync_threshold_v: float = 1.0,
    ) -> None:
        self._t_zero_mono = time.monotonic()
        self._last_emitted_s = 0.0
        self._external_sync = external_sync
        self._next_external_pulse = 0
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

        # External-sync: fake the rising edges at the scheduled period so the
        # GUI sees pulses arriving without anyone toggling Aux Out.
        edges: list[float] = []
        if self._external_sync:
            period = self._cfg.pulse.period_s
            n = self._cfg.pulse.n_pulses
            while self._next_external_pulse < n:
                t_pulse = self._next_external_pulse * period
                if t_pulse > now:
                    break
                edges.append(t_pulse)
                self.record_pulse(t_pulse)
                self._next_external_pulse += 1

        dt = 1.0 / rate
        t = self._last_emitted_s + np.arange(1, n_new + 1) * dt
        cp, gp = self._synthesize(t)
        self._last_emitted_s = float(t[-1])
        return StreamChunk(t=t, cp=cp, gp=gp, pulse_edges_s=edges)

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

    # --- C-f surface --------------------------------------------------------

    def configure_impedance_for_cf(self, cfg: CfConfig) -> None:
        """No-op for the mock; tests assert on later behavior instead."""
        self._cf_cfg = cfg

    def set_dc_bias(self, bias_v: float) -> None:
        self._bias_v = bias_v

    def set_amplitude(self, v_rms: float) -> None:
        self._amplitude_vrms = v_rms

    def run_sweep(
        self,
        cfg: SweeperSettings,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Synthesize a Voigt-2-CPE impedance spectrum.

        Defaults sit roughly where the B1500 analysis put real devices:
        fast arc τ ≈ 0.1 ms (peak ~1.6 kHz), slow arc τ ≈ 1 s (peak ~0.16 Hz).
        ``light_active`` collapses R1 by 100× to mimic the dominant
        photoconductive-shunt effect the analysis report calls out
        (light primarily modulates R, not C).
        """
        n = cfg.n_points
        if cfg.log_spacing:
            freq = np.logspace(math.log10(cfg.start_hz), math.log10(cfg.stop_hz), n)
        else:
            freq = np.linspace(cfg.start_hz, cfg.stop_hz, n)
        omega = 2.0 * math.pi * freq

        rs = 300.0
        # Fast branch (Ge2Se3 / interface), τ1 ≈ 100 µs
        r1 = 1e4 / (100.0 if light_active else 1.0)
        tau1 = 1e-4
        alpha1 = 0.85
        q1 = (tau1 ** alpha1) / r1

        # Slow branch (substrate / deep traps), τ2 ≈ 1 s
        r2 = 1e5 / (3.0 if light_active else 1.0)
        tau2 = 1.0
        alpha2 = 0.70
        q2 = (tau2 ** alpha2) / r2

        def cpe_arc(r: float, q: float, alpha: float) -> np.ndarray:
            jw = 1j * omega
            return r / (1.0 + r * q * (jw ** alpha))

        z = rs + cpe_arc(r1, q1, alpha1) + cpe_arc(r2, q2, alpha2)

        # Simulate sweep time: cap at 0.2 s/point so mock runs aren't painful.
        # Low frequencies are the real bottleneck on hardware; we don't need
        # to reproduce that here.
        t_per_point = 0.005
        for i in range(n):
            time.sleep(t_per_point)
            if progress_cb is not None and (i % max(1, n // 20) == 0):
                progress_cb((i + 1) / n)
            if stop_check is not None and stop_check():
                # Return what we have so the experiment can shut down gracefully.
                return freq[: i + 1], z.real[: i + 1], z.imag[: i + 1]
        if progress_cb is not None:
            progress_cb(1.0)

        # Toss in a touch of noise so plots aren't suspiciously clean.
        noise_real = self._rng.normal(0, 0.001, n) * np.abs(z)
        noise_imag = self._rng.normal(0, 0.001, n) * np.abs(z)
        return freq, z.real + noise_real, z.imag + noise_imag

    def run_bias_sweep(
        self,
        cv: BiasSweepSettings,
        fixed_freq_hz: float,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Synthesize a Mott-Schottky-like C-V curve at one fixed frequency.

        Depletion capacitance falls under reverse bias and rises toward the
        built-in potential: C(V) = C0 / sqrt(1 − V/Vbi), clamped. A diode-like
        leakage conductance grows in forward bias; ``light_active`` multiplies
        it (the photoconductive-shunt effect the C-f mock also models — light
        primarily modulates R, not C). At low ``fixed_freq_hz`` and forward
        bias the leakage dominates, the phase collapses toward 0°, and the
        extracted C goes unreliable — which is exactly what the GUI's
        loss-tangent flag should catch.
        """
        n = max(2, int(cv.n_points))
        bias = np.linspace(cv.start_v, cv.stop_v, n)
        omega = 2.0 * math.pi * float(fixed_freq_hz)

        vbi = 0.8  # built-in potential (V)
        c0 = 1e-9  # 1 nF zero-bias depletion capacitance
        c_min = 50e-12
        c_max = 20e-9
        # 1 − V/Vbi, floored so it stays positive past the built-in potential.
        arg = np.clip(1.0 - bias / vbi, 0.05, None)
        cdep = np.clip(c0 / np.sqrt(arg), c_min, c_max)

        # Leakage: a small baseline shunt plus a forward-bias term. Light adds
        # a photoconductive shunt (×50) so lit curves show higher loss.
        g = 1e-7 + 1e-6 * np.clip(bias, 0.0, None)
        if light_active:
            g = g * 50.0

        y = g + 1j * omega * cdep
        z = 1.0 / y

        # Simulate sweep time so progress/stop behave like the real sweeper.
        t_per_point = 0.005
        for i in range(n):
            time.sleep(t_per_point)
            if progress_cb is not None and (i % max(1, n // 20) == 0):
                progress_cb((i + 1) / n)
            if stop_check is not None and stop_check():
                return bias[: i + 1], z.real[: i + 1], z.imag[: i + 1]
        if progress_cb is not None:
            progress_cb(1.0)

        noise_real = self._rng.normal(0, 0.001, n) * np.abs(z)
        noise_imag = self._rng.normal(0, 0.001, n) * np.abs(z)
        return bias, z.real + noise_real, z.imag + noise_imag


class MockB1500(MockMFIA):
    """Synthetic B1500 backend for the ``--mock`` path and tests.

    Reuses ``MockMFIA``'s C-f / C-V synthetic spectra (the MFCMU measures the
    same complex impedance the MFIA does) and adds a synthetic SMU I-V curve,
    so the whole B1500 code path — C-f, C-V, and I-V under illumination — runs
    without an instrument. ``instrument = "B1500"`` so results are filed and
    merged distinctly from MFIA data.
    """

    device_id = "mock-b1500"
    device = "mock-b1500"
    instrument = "B1500"

    def run_iv_sweep(
        self,
        iv,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Synthesize a Shockley-diode I-V, with light adding a photocurrent.

        I = Is·(exp(V/nVt) − 1); illumination subtracts a photocurrent Iph so
        the lit curve sits below dark in reverse bias (fourth-quadrant photo
        response). Clamped to the configured compliance, matching how the real
        SMU would fold back at the current limit.
        """
        v = np.asarray(iv.values_v, dtype=float)
        npts = len(v)
        i_sat = 1e-11
        ideality = 1.6
        vt = 0.025852
        current = i_sat * (np.exp(v / (ideality * vt)) - 1.0)
        if light_active:
            current = current - 5e-6  # photocurrent
        comp = abs(iv.i_compliance_a)
        current = np.clip(current, -comp, comp)

        t_per_point = 0.002
        for k in range(npts):
            time.sleep(t_per_point)
            if progress_cb is not None and (k % max(1, npts // 20) == 0):
                progress_cb((k + 1) / npts)
            if stop_check is not None and stop_check():
                return v[: k + 1], current[: k + 1]
        if progress_cb is not None:
            progress_cb(1.0)

        noise = self._rng.normal(0, 1e-12, npts)
        return v, current + noise
