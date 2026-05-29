"""One-tidy-CSV-per-sweep output, matching the analysis pipeline's contract.

A single sweep file is fully self-describing: a commented metadata block
(device, substrate, bias, illumination, IA settings, timestamp) followed
by a one-line column header and the data rows. The analysis pipeline
(`ogt_eis.io.load_mfia_cf`) parses the commented block for run metadata
and the column header for the data — that's the only schema it needs.

Filename pattern is fixed:
    MFIA_Cf_<device>_Vb<bias>_<wavelength>_<YYYYMMDD_HHMMSS>.csv
Bias is signed-with-leading-sign (+1, -2, 0); wavelength is either
"dark", "dark_pre", "dark_post_<λ>", or "<λ>nm" matching the
IlluminationStep.label. Spaces in metadata strings are kept as-is —
shells that need quoting are the shell's problem.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from .cf_config import CfConfig, IlluminationStep


CSV_COLUMNS = [
    "frequency_hz",
    "z_mag_ohm",
    "phase_deg",
    "g_siemens",
    "b_siemens",
    "cp_f",
    "rp_ohm",
    "cs_f",
    "rs_ohm",
    "re_z_ohm",
    "im_z_ohm",
]


@dataclass
class SweepMetadata:
    """Per-sweep identifiers; bundled into the CSV header."""

    device_id: str
    substrate_type: str
    bias_v_commanded: float
    illumination_label: str  # e.g. "dark_pre", "385nm", "dark_post_385"
    wavelength_nm: float  # 0 for dark
    led_intensity_pct: Optional[float]  # commanded drive %, None for dark
    amplitude_vrms: float
    amplitude_vpk: float
    timestamp: str  # ISO 8601
    notes: str = ""
    # Best-effort actual drive current (pct × per-channel full-scale); None
    # if the LED source can't report it. The analysis pipeline keyed on
    # led_current_ma originally — kept for that, but led_intensity_pct is the
    # native commanded value for this hardware.
    led_current_ma: Optional[float] = None
    optical_power_mw: Optional[float] = None  # measured separately; null if not entered


@dataclass
class SweepResult:
    """Raw output of one frequency sweep, before CSV serialization."""

    frequency_hz: np.ndarray
    z_real: np.ndarray
    z_imag: np.ndarray
    metadata: SweepMetadata
    extra: dict = field(default_factory=dict)

    @property
    def z_mag(self) -> np.ndarray:
        return np.abs(self.z_real + 1j * self.z_imag)

    @property
    def phase_deg(self) -> np.ndarray:
        return np.degrees(np.arctan2(self.z_imag, self.z_real))

    @property
    def admittance(self) -> tuple[np.ndarray, np.ndarray]:
        """Returns (G, B) in Siemens — Y = 1/Z."""
        y = 1.0 / (self.z_real + 1j * self.z_imag)
        return y.real, y.imag

    @property
    def cp_rp(self) -> tuple[np.ndarray, np.ndarray]:
        """Parallel-equivalent Cp (F) and Rp (Ω) at each frequency.

        From Y = G + jB and ω = 2πf: Rp = 1/G, Cp = B/ω.
        """
        g, b = self.admittance
        omega = 2.0 * math.pi * self.frequency_hz
        # Avoid /0 on the pathological G=0 case at DC; CSV writer can hold NaN.
        with np.errstate(divide="ignore", invalid="ignore"):
            rp = np.where(g != 0, 1.0 / g, np.nan)
            cp = np.where(omega != 0, b / omega, np.nan)
        return cp, rp

    @property
    def cs_rs(self) -> tuple[np.ndarray, np.ndarray]:
        """Series-equivalent Cs (F) and Rs (Ω) at each frequency.

        Cs = -1 / (ω · Im(Z)); Rs = Re(Z).
        """
        omega = 2.0 * math.pi * self.frequency_hz
        rs = self.z_real
        with np.errstate(divide="ignore", invalid="ignore"):
            cs = np.where(
                (omega != 0) & (self.z_imag != 0),
                -1.0 / (omega * self.z_imag),
                np.nan,
            )
        return cs, rs


def _fmt_bias(bias_v: float) -> str:
    """Bias formatted for filename: leading sign, no spaces, 4 sig figs."""
    if bias_v == 0:
        return "+0"
    sign = "+" if bias_v > 0 else "-"
    a = abs(bias_v)
    if a == int(a):
        return f"{sign}{int(a)}"
    # Trim trailing zeros so 1.0 → +1, 1.500 → +1.5
    s = f"{a:.4g}"
    return f"{sign}{s}"


def make_filename(meta: SweepMetadata, ts: Optional[datetime] = None) -> str:
    when = ts or datetime.fromisoformat(meta.timestamp)
    stamp = when.strftime("%Y%m%d_%H%M%S")
    bias = _fmt_bias(meta.bias_v_commanded)
    device = meta.device_id or "unknown"
    illum = meta.illumination_label or "unknown"
    return f"MFIA_Cf_{device}_Vb{bias}_{illum}_{stamp}.csv"


def make_metadata_from_config(
    cfg: CfConfig,
    bias_v: float,
    step: IlluminationStep,
    *,
    timestamp: Optional[datetime] = None,
    optical_power_mw: Optional[float] = None,
    led_current_ma: Optional[float] = None,
) -> SweepMetadata:
    """Bundle CfConfig + the active (bias, illumination) into a flat metadata
    record. Convenient single source of truth for both the filename and
    the CSV header block.
    """
    when = (timestamp or datetime.now()).isoformat(timespec="seconds")
    vrms = cfg.ia.ac_amplitude_v  # CfConfig stores this in V RMS by convention
    return SweepMetadata(
        device_id=cfg.run.device_id,
        substrate_type=cfg.run.substrate_type,
        bias_v_commanded=bias_v,
        illumination_label=step.label,
        wavelength_nm=0.0 if step.is_dark else float(step.wavelength_nm),
        led_intensity_pct=None if step.is_dark else step.intensity_pct,
        amplitude_vrms=vrms,
        amplitude_vpk=vrms * math.sqrt(2.0),
        timestamp=when,
        notes=cfg.run.notes,
        led_current_ma=led_current_ma,
        optical_power_mw=optical_power_mw,
    )


def write_sweep_csv(result: SweepResult, path: Path | str) -> Path:
    """Write a single-sweep CSV with commented metadata header then data."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cp, rp = result.cp_rp
    cs, rs = result.cs_rs
    g, b = result.admittance

    with out.open("w", newline="") as f:
        # Commented metadata block — easy for both humans and the loader.
        meta = result.metadata
        f.write(f"# device_id: {meta.device_id}\n")
        f.write(f"# substrate_type: {meta.substrate_type}\n")
        f.write(f"# bias_v_commanded: {meta.bias_v_commanded}\n")
        f.write(f"# illumination_label: {meta.illumination_label}\n")
        f.write(f"# wavelength_nm: {meta.wavelength_nm}\n")
        pct = "none" if meta.led_intensity_pct is None else meta.led_intensity_pct
        f.write(f"# led_intensity_pct: {pct}\n")
        ma = "none" if meta.led_current_ma is None else meta.led_current_ma
        f.write(f"# led_current_ma: {ma}\n")
        f.write(f"# amplitude_vrms: {meta.amplitude_vrms}\n")
        f.write(f"# amplitude_vpk: {meta.amplitude_vpk}\n")
        if meta.optical_power_mw is not None:
            f.write(f"# optical_power_mw: {meta.optical_power_mw}\n")
        f.write(f"# timestamp: {meta.timestamp}\n")
        if meta.notes:
            f.write(f"# notes: {meta.notes}\n")
        # Data
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for i in range(len(result.frequency_hz)):
            w.writerow(
                [
                    f"{result.frequency_hz[i]:.9g}",
                    f"{result.z_mag[i]:.9g}",
                    f"{result.phase_deg[i]:.9g}",
                    f"{g[i]:.9g}",
                    f"{b[i]:.9g}",
                    f"{cp[i]:.9g}",
                    f"{rp[i]:.9g}",
                    f"{cs[i]:.9g}",
                    f"{rs[i]:.9g}",
                    f"{result.z_real[i]:.9g}",
                    f"{result.z_imag[i]:.9g}",
                ]
            )
    return out
