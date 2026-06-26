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

from .config import EquivCircuit, IASettings, TerminalMode


class AmplitudeUnit(str, Enum):
    """How the user typed the IA test amplitude in the GUI.

    Stored explicitly so the backend's RMS→peak conversion is unambiguous
    even after a config is round-tripped through CSV/JSON.
    """

    VRMS = "V rms"
    VPK = "V peak"


class SweepType(str, Enum):
    """Which axis the LabOne Sweeper steps for a campaign.

    C_F: sweep frequency at each (bias, illumination) — classic impedance
        spectroscopy. ``bias`` is the outer loop, ``sweep`` the swept axis.
    C_V: sweep DC bias at each (test-frequency, illumination) — capacitance-
        voltage. ``cv_frequencies_hz`` is the outer loop and ``cv_bias`` is
        the swept axis (Sweeper gridnode = ``/imps/N/bias/value``). A single-
        element frequency list is a classic single-frequency C-V; a multi-
        element list is a C-V-f map for frequency-dispersion / trap work.

    The illumination sequence is shared: it runs as the inner loop in both
    modes, so the dark-pre / lit / dark-post "plan" carries over verbatim.
    """

    C_F = "C-f (sweep frequency)"
    C_V = "C-V (sweep bias)"


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
    # Average over this many demod time constants per point. With this at 0 AND
    # averaging_samples=1 the sweeper did NO averaging at any frequency.
    averaging_tc: float = 0.0
    auto_bandwidth: bool = True
    filter_order: int = 4
    # Sinc filter — rejects harmonic / feedthrough content, auto-relevant below
    # ~50 Hz; ZI's recommendation for low-frequency impedance sweeps.
    sinc_filter: bool = True

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
class BiasSweepSettings:
    """Swept DC-bias axis for a C-V measurement (Sweeper gridnode = bias).

    Mirrors ``SweeperSettings`` but the grid is voltage, not frequency, and
    linear spacing is the default (C-V curves are read on a linear V axis).
    ``n_points`` is explicit — there's no per-decade concept on a bipolar,
    zero-crossing voltage axis. Settling/averaging mean the same as in the
    frequency sweep: at low test frequency each bias point still needs
    several time constants to settle before it's measured, so a fine bias
    sweep at 1 Hz is slow by the same arithmetic as a low-frequency C-f point.

    The ±range is validated against the terminal-mode bias limit
    (``TERMINAL_BIAS_LIMIT_V``) by the GUI before a run, same as the C-f
    bias list.
    """

    start_v: float = -5.0
    stop_v: float = 5.0
    n_points: int = 101
    log_spacing: bool = False
    settling_tcs: float = 5.0
    settling_inaccuracy: float = 0.01
    averaging_samples: int = 1
    averaging_time_s: float = 0.0
    averaging_tc: float = 0.0
    auto_bandwidth: bool = True
    filter_order: int = 4
    sinc_filter: bool = True

    @property
    def values_v(self) -> list[float]:
        """The commanded bias points, for validation/preview (not the run —
        the Sweeper generates its own grid from start/stop/n_points)."""
        import numpy as np

        n = max(2, int(self.n_points))
        if self.log_spacing:
            # Log spacing across a bipolar range is ill-defined; callers use
            # linear for bias. Fall back to linear if someone sets it.
            return list(np.linspace(self.start_v, self.stop_v, n))
        return list(np.linspace(self.start_v, self.stop_v, n))


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

    The LED source (NI PXI-7853R via ``led_driver``) is addressed by
    **wavelength** and driven by **intensity percent**. ``wavelength_nm``
    of None means "all LEDs off" (dark); otherwise it selects the channel
    and ``intensity_pct`` (0–100) is the commanded drive.

    Intensity is a percent, not a current — the driver maps percent to
    current internally (per-channel full-scale + safety limits). The actual
    drive current is recorded best-effort in the sweep metadata; the real
    photon-flux normalization the analysis side wants comes from a separate
    optical-power measurement (Thorlabs PM at sample plane).
    """

    label: str  # "dark_pre", "385nm", "dark_post_385" — used in filename
    wavelength_nm: Optional[float]  # None = dark
    intensity_pct: float = 0.0
    settle_s: float = 30.0

    @property
    def is_dark(self) -> bool:
        return self.wavelength_nm is None


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
    def intensity_series(
        cls,
        wavelength_nm: float,
        drive_points_pct: list[float],
        *,
        dark_pre: bool = True,
        interleave_dark: bool = True,
        light_settle_s: float = 30.0,
        dark_settle_s: float = 60.0,
    ) -> "IlluminationSequence":
        """A single wavelength stepped through several drive levels — the
        photo-response-vs-intensity sweep used to test whether the response is
        linear in flux (and therefore whether a power-normalized action
        spectrum is valid). Dark is interleaved by default so persistent
        photoconductivity from one level recovers before the next.
        """
        steps: list[IlluminationStep] = []
        if dark_pre:
            steps.append(
                IlluminationStep("dark_pre", None, intensity_pct=0.0, settle_s=dark_settle_s)
            )
        for pct in drive_points_pct:
            steps.append(
                IlluminationStep(
                    f"{int(wavelength_nm)}nm_{pct:g}pct",
                    wavelength_nm,
                    intensity_pct=pct,
                    settle_s=light_settle_s,
                )
            )
            if interleave_dark:
                steps.append(
                    IlluminationStep(
                        f"dark_post_{pct:g}pct",
                        None,
                        intensity_pct=0.0,
                        settle_s=dark_settle_s,
                    )
                )
        return cls(steps=steps)

    @classmethod
    def wavelength_intensity_matrix(
        cls,
        wavelengths_nm: list[float],
        drive_points_pct: list[float],
        *,
        dark_pre: bool = True,
        interleave_dark: bool = True,
        light_settle_s: float = 30.0,
        dark_settle_s: float = 60.0,
    ) -> "IlluminationSequence":
        """The full wavelength × intensity matrix — every wavelength stepped
        through every drive level. This is ``intensity_series`` generalized to
        several wavelengths: the complete action-spectrum-vs-flux campaign in
        one sequence (run at a fixed bias, or under the C-V/C-f loop, which
        repeats this whole plan at each bias / test frequency).

        Dark is interleaved between lit steps by default (one ``dark_pre`` then
        a ``dark_post`` after each level) so persistent photoconductivity
        recovers before the next condition. Wavelength order is the measurement
        order of ``wavelengths_nm`` as given (caller sorts/reverses to taste).
        """
        steps: list[IlluminationStep] = []
        if dark_pre:
            steps.append(
                IlluminationStep("dark_pre", None, intensity_pct=0.0, settle_s=dark_settle_s)
            )
        for wl in wavelengths_nm:
            for pct in drive_points_pct:
                steps.append(
                    IlluminationStep(
                        f"{int(wl)}nm_{pct:g}pct",
                        wl,
                        intensity_pct=pct,
                        settle_s=light_settle_s,
                    )
                )
                if interleave_dark:
                    steps.append(
                        IlluminationStep(
                            f"dark_post_{int(wl)}_{pct:g}pct",
                            None,
                            intensity_pct=0.0,
                            settle_s=dark_settle_s,
                        )
                    )
        return cls(steps=steps)

    @classmethod
    def default_interleaved(cls) -> "IlluminationSequence":
        # Sweep UV→IR; canonical channel wiring (Ch0=850 … Ch7=385) is
        # handled by the driver's wavelength addressing, so order here is
        # just the measurement order.
        wavelengths = [385.0, 470.0, 505.0, 530.0, 590.0, 625.0, 740.0, 850.0]
        steps: list[IlluminationStep] = [
            IlluminationStep("dark_pre", None, intensity_pct=0.0, settle_s=60.0)
        ]
        for wl in wavelengths:
            steps.append(
                IlluminationStep(
                    f"{int(wl)}nm",
                    wl,
                    intensity_pct=100.0,
                    settle_s=30.0,
                )
            )
            steps.append(
                IlluminationStep(
                    f"dark_post_{int(wl)}",
                    None,
                    intensity_pct=0.0,
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
        # 2-terminal by default for the C-f campaign: the devices are
        # high-impedance at low frequency (slow arc ≥ MΩ), and 2-terminal
        # allows the full ±10 V bias range the ±5 V matrix needs. 4-terminal
        # would cap bias at ±3 V. Confirm this matches the B1500 reference
        # contact config before trusting the 1–300 kHz stitch.
        terminal_mode=TerminalMode.TWO_TERMINAL,
        imp_index=0,
    ))
    amplitude_unit: AmplitudeUnit = AmplitudeUnit.VRMS
    sweep_type: SweepType = SweepType.C_F
    # C-f axis: swept frequency at each bias.
    sweep: SweeperSettings = field(default_factory=SweeperSettings)
    bias: BiasSequence = field(default_factory=BiasSequence)
    # C-V axis: swept bias at each fixed test frequency. cv_frequencies_hz is
    # the outer loop — one entry is a single-frequency C-V, several entries a
    # C-V-f map. Ignored when sweep_type is C_F (and vice-versa for sweep/bias).
    cv_frequencies_hz: list[float] = field(default_factory=lambda: [100_000.0])
    cv_bias: BiasSweepSettings = field(default_factory=BiasSweepSettings)
    illumination: IlluminationSequence = field(
        default_factory=IlluminationSequence.default_interleaved
    )
    run: RunMetadata = field(default_factory=RunMetadata)

    def to_dict(self) -> dict:
        return asdict(self)
