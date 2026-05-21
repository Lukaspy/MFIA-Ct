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
    """Optical pulse parameters; drives Aux Out N as a software-paced square wave."""

    aux_out_channel: int = 0
    high_v: float = 5.0
    low_v: float = 0.0
    pulse_width_s: float = 0.010
    period_s: float = 1.0
    n_pulses: int = 100


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
