"""Experiment configuration dataclasses.

A `CtConfig` is a complete description of one photo-C-t run: instrument
settings, optical pulse parameters, and acquisition pacing. It is saved
alongside the data so a run is fully reproducible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class EquivCircuit(str, Enum):
    """Equivalent circuit model used to convert measured |Z|, phase to R + C."""

    CP_RP = "Cp||Rp"
    CS_RS = "Cs-Rs"


class PulseSource(str, Enum):
    """Who paces the optical pulse train.

    INTERNAL: MFIA software-toggles its own Aux Out as the pulse signal.
        Convenient but limited to ~ms pulse-timing precision by API round trips.
    EXTERNAL: an external pulse driver (function generator, laser TTL sync)
        drives the LED. Its sync line goes into an MFIA Aux In; the host
        detects rising edges in software at the demod rate, so timing is
        sub-µs accurate. Aux Out is unused in this mode.
    """

    INTERNAL = "Internal (MFIA Aux Out)"
    EXTERNAL = "External (Aux In sync)"


@dataclass
class IASettings:
    frequency_hz: float = 100_000.0
    ac_amplitude_v: float = 0.05
    dc_bias_v: float = 0.0
    equiv_circuit: EquivCircuit = EquivCircuit.CP_RP
    imp_index: int = 0


@dataclass
class DemodSettings:
    time_constant_s: float = 1e-5
    filter_order: int = 4
    sample_rate_hz: float = 100_000.0


@dataclass
class PulseSettings:
    """Optical pulse parameters."""

    source: PulseSource = PulseSource.INTERNAL
    # Internal mode — MFIA drives Aux Out as a software-paced square wave.
    aux_out_channel: int = 0
    high_v: float = 5.0
    low_v: float = 0.0
    pulse_width_s: float = 0.010
    # Common
    period_s: float = 1.0
    n_pulses: int = 100
    # External mode — MFIA reads sync TTL from Aux In and detects edges.
    sync_aux_in_channel: int = 0  # 0 = Aux In 1, 1 = Aux In 2 (API 0-indexed)
    sync_threshold_v: float = 1.0


@dataclass
class AcquisitionSettings:
    """Continuous-acquisition pacing."""

    poll_interval_s: float = 0.05


@dataclass
class CtConfig:
    ia: IASettings = field(default_factory=IASettings)
    demod: DemodSettings = field(default_factory=DemodSettings)
    pulse: PulseSettings = field(default_factory=PulseSettings)
    acq: AcquisitionSettings = field(default_factory=AcquisitionSettings)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
