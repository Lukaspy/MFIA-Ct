"""C-f (impedance spectroscopy) campaign configuration.

This is the contract for a full C-f run: instrument settings + frequency
sweep parameters + bias list + illumination sequence + run-level metadata
(device id, substrate, notes). The whole CfConfig is serialized into each
output CSV's metadata header so a sweep file is fully self-describing.

Important: ``IASettings.ac_amplitude_v`` here is in **V RMS** (the unit the
B1500 reference data uses). The hardware backend converts to V peak when
writing the MFIA's ``/imps/N/output/amplitude`` node, which the LabOne
API expects in peak. Mixing these silently introduces a √2 offset
between MFIA and B1500 data — the analysis pipeline's stitching across
the 1–300 kHz overlap is what catches it, if it slips.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from .config import EquivCircuit, IASettings


class AmplitudeUnit(str, Enum):
    """How the user typed the IA test amplitude in the GUI.

    Stored explicitly so the backend's RMS→peak conversion is unambiguous
    even after a config is round-tripped through CSV/JSON.
    """

    VRMS = "V rms"
    VPK = "V peak"


@dataclass
class SweeperSettings:
    """LabOne Sweeper module parameters for one frequency sweep.

    Defaults match the campaign spec (10 mHz – 300 kHz, 10 pts/decade log,
    auto-bandwidth, ~5 time constants of settling). The low-frequency
    floor dominates wall time — at 10 mHz one cycle is 100 s and the
    settling spec is 5–10 TC, so a single point below 0.1 Hz can be
    minutes. The GUI surfaces a quick-look option that raises the floor.
    """

    start_hz: float = 0.01
    stop_hz: float = 300_000.0
    points_per_decade: int = 10
    log_spacing: bool = True
    settling_tcs: float = 5.0
    settling_inaccuracy: float = 0.01
    averaging_samples: int = 1
    averaging_time_s: float = 0.0
    auto_bandwidth: bool = True
    filter_order: int = 4

    @property
    def n_points(self) -> int:
        """Total sweep points implied by start/stop and pts/decade."""
        import math

        if not self.log_spacing:
            # Lin spacing: caller provides explicit n via points_per_decade as
            # a stand-in. For the log default case this branch is unused.
            return max(2, int(self.points_per_decade))
        decades = math.log10(self.stop_hz / self.start_hz)
        return max(2, int(round(decades * self.points_per_decade)) + 1)


@dataclass
class BiasSequence:
    """List of device-terminal DC bias values to step through.

    Default matches the campaign spec's "preferred" matrix
    [-5, -4, -2, -1, 0, +1, +2, +4, +5] V — covers polarity and the +1..+4 V
    points that overlap the B1500 reference data for stitching.
    """

    values_v: list[float] = field(
        default_factory=lambda: [-5.0, -4.0, -2.0, -1.0, 0.0, 1.0, 2.0, 4.0, 5.0]
    )
    bias_settle_s: float = 90.0

    def __len__(self) -> int:
        return len(self.values_v)


@dataclass
class IlluminationStep:
    """One illumination state in the per-bias sequence.

    ``channel_index`` is the Mightex channel (0-based); None means "all
    LEDs off" (dark). ``wavelength_nm`` is informational (it goes into
    the filename and metadata so the analysis loader can group sweeps)
    and is 0 for dark steps.

    ``current_ma`` is what gets written to the LED driver. The eventual
    photon-flux normalization the analysis side wants comes from a
    separate optical-power measurement (Thorlabs PM at sample plane);
    this field is just what was commanded, not what was delivered.
    """

    label: str  # "dark", "385nm", etc — used in filename
    channel_index: Optional[int]  # None = dark
    wavelength_nm: float  # 0 for dark
    current_ma: float = 0.0
    settle_s: float = 30.0

    @property
    def is_dark(self) -> bool:
        return self.channel_index is None


@dataclass
class IlluminationSequence:
    """Ordered list of IlluminationStep that runs at every bias point.

    Default mirrors the campaign spec's interleaved pattern:
    [dark, 385, dark, 470, dark, 505, dark, 530, dark, 590, dark, 625, dark,
     740, dark, 850, dark] — dark interposed so persistent photoconductivity
    from one wavelength can recover before the next.
    """

    steps: list[IlluminationStep] = field(default_factory=list)
    dark_settle_s: float = 60.0  # used for the trailing/dark steps if not set

    def __len__(self) -> int:
        return len(self.steps)

    @classmethod
    def default_interleaved(cls) -> "IlluminationSequence":
        wavelengths = [
            (0, 385.0),
            (1, 470.0),
            (2, 505.0),
            (3, 530.0),
            (4, 590.0),
            (5, 625.0),
            (6, 740.0),
            (7, 850.0),
        ]
        steps: list[IlluminationStep] = [
            IlluminationStep("dark_pre", None, 0.0, current_ma=0.0, settle_s=60.0)
        ]
        for idx, wl in wavelengths:
            steps.append(
                IlluminationStep(
                    f"{int(wl)}nm",
                    idx,
                    wl,
                    current_ma=20.0,
                    settle_s=30.0,
                )
            )
            steps.append(
                IlluminationStep(
                    f"dark_post_{int(wl)}",
                    None,
                    0.0,
                    current_ma=0.0,
                    settle_s=60.0,
                )
            )
        return cls(steps=steps)


@dataclass
class RunMetadata:
    """Top-level run identifiers — passed straight into each CSV header."""

    device_id: str = ""
    substrate_type: str = ""  # "p-Si", "n-Si", "nitride", etc.
    notes: str = ""
    output_dir: str = ""  # destination folder for CSVs


@dataclass
class CfConfig:
    ia: IASettings = field(default_factory=lambda: IASettings(
        frequency_hz=1000.0,
        ac_amplitude_v=0.030,  # 30 mV RMS — matches B1500 reference
        dc_bias_v=0.0,
        equiv_circuit=EquivCircuit.CP_RP,
        imp_index=0,
    ))
    amplitude_unit: AmplitudeUnit = AmplitudeUnit.VRMS
    sweep: SweeperSettings = field(default_factory=SweeperSettings)
    bias: BiasSequence = field(default_factory=BiasSequence)
    illumination: IlluminationSequence = field(
        default_factory=IlluminationSequence.default_interleaved
    )
    run: RunMetadata = field(default_factory=RunMetadata)

    def to_dict(self) -> dict:
        return asdict(self)
