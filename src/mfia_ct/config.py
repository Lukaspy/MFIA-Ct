"""Experiment configuration dataclasses.

A `CtConfig` is a complete description of one photo-C-t run: instrument
settings, trigger setup, optical pulse parameters, and acquisition window.
It is saved alongside the data so a run is fully reproducible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class EquivCircuit(str, Enum):
    """Equivalent circuit model used to convert measured |Z|, phase to R + C."""

    CP_RP = "Cp||Rp"
    CS_RS = "Cs-Rs"


class TriggerSource(str, Enum):
    """Where the DAQ module gets its trigger edge.

    SOFTWARE: MFIA toggles Aux Out for the optical pulse and the host software
        force-triggers the DAQ module on the same call. No cabling needed.
        Software latency limits time precision to ~1 ms.
    TRIGGER_IN_1: hardware trigger via back-panel Trigger In 1 BNC. Sub-µs
        precision. Used either by looping Aux Out → Trigger In 1 with a BNC
        cable (MFIA-internal pulse generation), or by feeding an external
        laser/LED sync signal directly into Trigger In 1.
    TRIGGER_IN_2: same as above on the back-panel Trigger In 2 BNC.
    """

    SOFTWARE = "software"
    TRIGGER_IN_1 = "Trigger In 1"
    TRIGGER_IN_2 = "Trigger In 2"


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
    """Optical pulse parameters.

    For INTERNAL trigger source, these drive Aux Out N to produce a square pulse.
    For EXTERNAL, only `period_s` is used (to set the DAQ hold-off / count).
    """

    aux_out_channel: int = 0
    high_v: float = 5.0
    low_v: float = 0.0
    pulse_width_s: float = 0.010
    period_s: float = 1.0
    n_pulses: int = 100


@dataclass
class AcquisitionSettings:
    pre_trigger_s: float = 0.001
    duration_s: float = 0.500
    repetitions: int = 1
    trigger_source: TriggerSource = TriggerSource.SOFTWARE
    trigger_level_v: float = 1.0
    trigger_hysteresis_v: float = 0.1


@dataclass
class CtConfig:
    ia: IASettings = field(default_factory=IASettings)
    demod: DemodSettings = field(default_factory=DemodSettings)
    pulse: PulseSettings = field(default_factory=PulseSettings)
    acq: AcquisitionSettings = field(default_factory=AcquisitionSettings)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
