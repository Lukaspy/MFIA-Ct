#!/usr/bin/env python3
"""De-embed the LCUR pi filter from raw MFIA-Ct sweep CSVs.

WHAT THIS CORRECTS
==================
Sessions measured through the LCUR input pi filter (built 2026-07-09) with
LabOne compensation OFF store RAW impedance. The filter is a known linear
network; this script inverts it exactly and writes corrected files.

    DUT/arm side o----+---[ R ]---+----o MFIA LCUR
                      |           |
                     C1          C2
                      |           |
                     gnd         gnd     (gnd = BNC shell / measurement ref)

NETWORK MODEL AND EXACT INVERSION
=================================
The instrument reports zeta = V_drive / I_TIA (factory-calibrated: its own
input impedance Z_in does not appear in-band for a direct DUT). The physical
LCUR terminal, however, sits at V_B = I_TIA * Z_in above reference, so C2
carries current the TIA never sees, and C1 shunts the arm node at
V_A = V_B + I_R * R. Solving the two-node network exactly:

    k2    = 1 + j*w*C2*Z_in
    Z_dut = (zeta - R*k2) / (k2 + j*w*C1*(Z_in + R*k2))

Sanity limits:
  * w -> 0:            Z_dut = zeta - R           (simple series subtraction)
  * C1 = C2 = 0:       Z_dut = zeta - R
  * error of ignoring the caps at 300 Hz on the 1 kOhm ranges: ~20% (C1 term)
  * error of ignoring C2 at 3 Hz on the 10 nA range (160 kOhm): ~3%
So the full formula is REQUIRED above ~10 Hz and on the fine ranges.

Z_in BY RANGE (MFIA UM Table 7.10, input impedance at DC — valid in-band):
    10 mA, 1 mA   : 50 Ohm        100 uA, 10 uA : 60 Ohm
    1 uA, 100 nA  : 1 kOhm        10 nA, 1 nA   : 160 kOhm
The current range is read from the CSV header (current_range_a). Filtered
sessions MUST use pinned ranges — 'auto' aborts (realized range is not
recorded per point; see toolchain TODO).

FILTER CONSTANTS
================
Read from a YAML-ish constants file (default: cal/pifilter_constants.txt next
to the data, else --constants). Re-characterize after ANY rework of the board:
  R   : through-loop measurement, 10-100 Hz, reads R directly
        (2026-07-09: 9909.3 / 9904.4 / 9896.5 Ohm across three sessions)
  C1,C2: knee-phase method — through-loop at 100 kHz, phase splits as
        atan(w*C1*50) + atan(w*C2*Z_in_range); or fixture-measure each cap.

USAGE
=====
  python scripts/deembed_pifilter.py <file-or-directory> [more paths ...]
        [--constants cal/pifilter_constants.txt] [--suffix _deembedded]

Writes <name><suffix>.csv next to each input with a provenance header block
(constants, formula id, script version, date). Never overwrites raw files.
Derived columns (g, b, cp, rp, cs, rs) are recomputed from corrected Z.
"""

import argparse
import cmath
import csv
import math
import sys
import time
from pathlib import Path

VERSION = "1.0 (2026-07-09)"

# Z_in by current range (Ohm) — MFIA UM Table 7.10
ZIN_BY_RANGE = {
    1.0e-2: 50.0, 1.0e-3: 50.0,
    1.0e-4: 60.0, 1.0e-5: 60.0,
    1.0e-6: 1.0e3, 1.0e-7: 1.0e3,
    1.0e-8: 1.6e5, 1.0e-9: 1.6e5,
}

DEFAULT_CONSTANTS = {  # as-built 2026-07-09; override via --constants file
    "R_ohm": 9906.0,
    "C1_farad": 9.9e-9,   # arm/DUT side
    "C2_farad": 9.9e-9,   # MFIA side
}


def load_constants(path):
    """Constants file: 'key: value' lines, # comments. Keys as in DEFAULT_CONSTANTS."""
    consts = dict(DEFAULT_CONSTANTS)
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip() in consts:
                    consts[k.strip()] = float(v.strip())
    return consts


def deembed_point(f_hz, z_raw, consts, z_in):
    """Exact pi-network inversion. Returns corrected complex Z_dut."""
    w = 2.0 * math.pi * f_hz
    k2 = 1.0 + 1j * w * consts["C2_farad"] * z_in
    num = z_raw - consts["R_ohm"] * k2
    den = k2 + 1j * w * consts["C1_farad"] * (z_in + consts["R_ohm"] * k2)
    return num / den


def derived_columns(z, f_hz, bias):
    """Recompute the standard MFIA-Ct derived representations from Z."""
    w = 2.0 * math.pi * f_hz
    y = 1.0 / z if z != 0 else complex(0)
    g, b = y.real, y.imag
    cp = b / w if w > 0 else 0.0
    rp = 1.0 / g if g != 0 else float("inf")
    re, im = z.real, z.imag
    cs = -1.0 / (w * im) if (w > 0 and im != 0) else float("inf")
    rs = re
    return {
        "z_mag_ohm": abs(z), "phase_deg": math.degrees(cmath.phase(z)),
        "g_siemens": g, "b_siemens": b, "cp_f": cp, "rp_ohm": rp,
        "cs_f": cs, "rs_ohm": rs, "re_z_ohm": re, "im_z_ohm": im,
        "bias_v": bias,
    }


def process_file(path, consts, suffix):
    header_lines, rows, range_a = [], [], None
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                header_lines.append(line.rstrip("\n"))
                if line.startswith("# current_range_a:"):
                    val = line.split(":", 1)[1].strip()
                    range_a = None if val == "auto" else float(val)
            else:
                rd = csv.DictReader([line.rstrip("\n")] + fh.read().splitlines())
                rows = list(rd)
                break
    if range_a is None:
        print(f"  SKIP {path.name}: current_range_a is auto/absent — "
              f"realized range unknown; de-embed requires a pinned range")
        return False
    z_in = ZIN_BY_RANGE.get(range_a)
    if z_in is None:
        print(f"  SKIP {path.name}: no Z_in known for range {range_a}")
        return False

    out = path.with_name(path.stem + suffix + ".csv")
    cols = ["frequency_hz", "z_mag_ohm", "phase_deg", "g_siemens", "b_siemens",
            "cp_f", "rp_ohm", "cs_f", "rs_ohm", "re_z_ohm", "im_z_ohm", "bias_v"]
    with open(out, "w", newline="") as fh:
        for h in header_lines:
            fh.write(h + "\n")
        fh.write(f"# DEEMBEDDED: LCUR pi filter removed "
                 f"(script v{VERSION}, run {time.strftime('%Y-%m-%dT%H:%M:%S')})\n")
        fh.write(f"# deembed_constants: R={consts['R_ohm']} ohm, "
                 f"C1={consts['C1_farad']*1e9:.3f} nF (arm side), "
                 f"C2={consts['C2_farad']*1e9:.3f} nF (MFIA side), "
                 f"Z_in={z_in:g} ohm (range {range_a:g} A)\n")
        fh.write("# deembed_formula: Zdut=(Zraw-R*k2)/(k2+jwC1*(Zin+R*k2)), "
                 "k2=1+jwC2*Zin  [see scripts/deembed_pifilter.py]\n")
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            f_hz = float(r["frequency_hz"])
            z_raw = complex(float(r["re_z_ohm"]), float(r["im_z_ohm"]))
            bias = float(r.get("bias_v", 0.0))
            if f_hz <= 0:
                continue
            z = deembed_point(f_hz, z_raw, consts, z_in)
            row = {"frequency_hz": f_hz, **derived_columns(z, f_hz, bias)}
            w.writerow({k: f"{v:.9g}" if isinstance(v, float) else v
                        for k, v in row.items()})
    print(f"  ok   {path.name} -> {out.name}  (Z_in={z_in:g})")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="raw CSV files or directories")
    ap.add_argument("--constants", default=None,
                    help="filter constants file (default: built-in as-built values)")
    ap.add_argument("--suffix", default="_deembedded")
    args = ap.parse_args()
    consts = load_constants(args.constants)
    print(f"pi-filter de-embed v{VERSION}: R={consts['R_ohm']} ohm, "
          f"C1={consts['C1_farad']*1e9:.2f} nF, C2={consts['C2_farad']*1e9:.2f} nF")
    n = 0
    for p in args.paths:
        p = Path(p)
        files = sorted(p.glob("*.csv")) if p.is_dir() else [p]
        for f in files:
            if args.suffix in f.stem or f.stem.startswith("CAL_"):
                continue
            n += process_file(f, consts, args.suffix)
    print(f"{n} file(s) de-embedded")


if __name__ == "__main__":
    main()
