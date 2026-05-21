"""Synthetic backend that emits plausible photo-C-t transients.

Lets the full GUI run without a connected MFIA. Models a simple two-exponential
photo-capacitance response: fast rise on light-on, slower exponential approach
to a photoinduced steady-state, then double-exponential recovery on light-off.
"""

from __future__ import annotations

import numpy as np

from .config import CtConfig


class MockMFIA:
    device_id = "mock"
    device = "mock"

    def __init__(self, seed: int = 0) -> None:
        self._rng = np.random.default_rng(seed)
        self._cfg: CtConfig | None = None

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

    def generate_trace(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (t, Cp, Gp) for one trigger segment.

        The DC capacitance is set to 10 pF; on-light photoresponse adds ~1 pF
        with fast (50 µs) and slow (5 ms) components; off-light decays with a
        300 µs and 50 ms double exponential. Channel conductance Gp tracks
        capacitance but with opposite sign — photo-generated carriers raise
        conductance and (in a depletion-modulated FET) drop measured C.
        """
        if self._cfg is None:
            raise RuntimeError("configure_impedance() must be called first")
        acq = self._cfg.acq
        pulse = self._cfg.pulse
        rate = self._cfg.demod.sample_rate_hz

        n = int(acq.duration_s * rate)
        t = np.linspace(-acq.pre_trigger_s, acq.duration_s - acq.pre_trigger_s, n)

        c_dc = 10e-12
        delta_c = -1e-12  # depletion-mediated drop on illumination
        g_dc = 1e-9
        delta_g = 5e-9

        on_mask = (t >= 0) & (t < pulse.pulse_width_s)
        off_mask = t >= pulse.pulse_width_s

        cp = np.full_like(t, c_dc)
        gp = np.full_like(t, g_dc)

        # On transient: 1 - (a*exp(-t/tau_f) + (1-a)*exp(-t/tau_s)) approach
        tau_f, tau_s, a = 5e-5, 5e-3, 0.6
        on_t = t[on_mask]
        approach = 1 - (a * np.exp(-on_t / tau_f) + (1 - a) * np.exp(-on_t / tau_s))
        cp[on_mask] += delta_c * approach
        gp[on_mask] += delta_g * approach

        # Off transient: start from where on transient ended, decay back
        c_at_off = cp[on_mask][-1] - c_dc if on_mask.any() else 0
        g_at_off = gp[on_mask][-1] - g_dc if on_mask.any() else 0
        tau_f_off, tau_s_off, b = 3e-4, 5e-2, 0.4
        off_t = t[off_mask] - pulse.pulse_width_s
        decay = b * np.exp(-off_t / tau_f_off) + (1 - b) * np.exp(-off_t / tau_s_off)
        cp[off_mask] = c_dc + c_at_off * decay
        gp[off_mask] = g_dc + g_at_off * decay

        # Noise floor scaled to demod TC: ~1 aF/√Hz @ 1 MHz BW (toy estimate)
        enbw = 1 / (8 * self._cfg.demod.time_constant_s)
        cp_noise = 1e-18 * np.sqrt(enbw)
        gp_noise = 1e-12 * np.sqrt(enbw)
        cp += self._rng.normal(0, cp_noise, n)
        gp += self._rng.normal(0, gp_noise, n)

        return t, cp, gp
