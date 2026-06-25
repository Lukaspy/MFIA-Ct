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


class TerminalMode(str, Enum):
    """MFIA impedance measurement terminal configuration (/imps/n/mode).

    FOUR_TERMINAL: uses current + voltage drop across the DUT; excludes
        series-resistance influences. Best for LOW impedance. The DC bias
        rides on the device's common-mode input range, so it is limited
        to ±3 V on the MFIA.
    TWO_TERMINAL: uses the driving voltage + measured current. Best for
        HIGH impedance (voltage-measurement parasitics otherwise limit the
        frequency range). DC bias can reach ±10 V since the voltage inputs
        are not connected.
    """

    FOUR_TERMINAL = "4-terminal"
    TWO_TERMINAL = "2-terminal"


# Max DC bias the MFIA allows in each terminal mode (volts, ±). The 4-terminal
# limit is set by the common-mode input range; 2-terminal is the full source
# range. From the MFIA user manual specifications table.
TERMINAL_BIAS_LIMIT_V = {
    TerminalMode.FOUR_TERMINAL: 3.0,
    TerminalMode.TWO_TERMINAL: 10.0,
}


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
    LED_8CH = "LED driver (8-ch PXI)"


@dataclass
class IASettings:
    frequency_hz: float = 100_000.0
    ac_amplitude_v: float = 0.05
    dc_bias_v: float = 0.0
    equiv_circuit: EquivCircuit = EquivCircuit.CP_RP
    terminal_mode: TerminalMode = TerminalMode.FOUR_TERMINAL
    imp_index: int = 0
    # Fixed current-input range (A) for the impedance measurement. ``None`` =
    # auto-range (the default, and what the sweeper normally wants). Pin a
    # sensitive range (e.g. 10e-9) for a high-impedance / low-current
    # measurement where auto-range misbehaves at the low-frequency end —
    # validate the pinned range against a known reference (e.g. the B1500).
    current_range_a: float | None = None


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
    # LED-driver mode (8-ch PXI) — software-paced pulses on one LED channel.
    led_wavelength_nm: float = 470.0
    led_intensity_pct: float = 100.0


@dataclass
class AcquisitionSettings:
    """Continuous-acquisition pacing."""

    poll_interval_s: float = 0.05


@dataclass
class FunctionGeneratorSettings:
    """Optional external pulse driver (Agilent 33250A over GPIB).

    Only consulted in EXTERNAL pulse mode. When ``enabled`` is true the
    experiment configures the FG for a triggered pulse burst, arms it, and
    fires ``*TRG`` once the MFIA is subscribed. Pulse width / count / period
    come from ``PulseSettings`` so there's a single source of truth.
    """

    enabled: bool = False
    resource: str = "GPIB0::10::INSTR"
    high_v: float = 5.0
    low_v: float = 0.0
    load_ohms: str = "INF"  # "INF" for high-Z LED driver inputs, "50" for 50 Ω


@dataclass
class CtConfig:
    ia: IASettings = field(default_factory=IASettings)
    demod: DemodSettings = field(default_factory=DemodSettings)
    pulse: PulseSettings = field(default_factory=PulseSettings)
    acq: AcquisitionSettings = field(default_factory=AcquisitionSettings)
    fg: FunctionGeneratorSettings = field(default_factory=FunctionGeneratorSettings)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
