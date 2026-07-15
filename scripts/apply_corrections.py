#!/usr/bin/env python3
"""apply_corrections.py — THE canonical correction pipeline for MFIA-Ct data.

Takes a directory of raw session CSVs, classifies every file, applies the
appropriate correction, and writes results into a parallel `compensated/`
tree plus a CORRECTIONS_APPLIED.md manifest. Fully re-runnable and traceable:
every output header records constants, script version, and correction type.

CLASSIFICATION AND CORRECTIONS
==============================
1. Manual C-V files (sweep_type C-V + per-row current_range_a column)
     -> pi-filter C-V de-embed (per-row Z_in). [deembed_pifilter.process_cv_file]
2. Filtered C-f files (band top <= ~340 Hz, pinned fine range, or
   '# method: manual-poll' header)
     -> pi-filter de-embed. [deembed_pifilter.process_file]
3. Bypass wideband C-f files (band start >= 340 Hz)
     -> series-parasitic comp: Z_dev = Z_raw - (R_s + j*w*L_s)
        (chain measured 2026-07-15 via ISS short: R_s=0.020 ohm, L_s=7.45 uH;
        ~5% reactive at 500 kHz, negligible below ~10 kHz.)
4. Runner C-V files (1 kHz engine C-V, bypass): correction negligible
   (+j0.05 ohm at 1 kHz) -> copied unmodified, noted in manifest.

CONSTANTS come from the canonical cal file (last key wins):
  capacitance_measurements/2013-3_completerun/cal/pifilter_constants.txt
  keys: R_ohm, C1_farad, C2_farad, R_s_ohm, L_s_h

USAGE
  python scripts/apply_corrections.py <session_dir_or_root> \
      [--cal PATH] [--out-name compensated]
If given a root containing session subdirs, processes each subdir.
"""
import argparse
import csv
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deembed_pifilter as dpf

PIPE_VERSION = "1.0 (2026-07-15)"
DEFAULT_CAL = Path(__file__).resolve().parents[1].parent.parent / \
    "capacitance_measurements/2013-3_completerun/cal/pifilter_constants.txt"

SERIES_DEFAULTS = {"R_s_ohm": 0.020, "L_s_h": 7.45e-6}


def load_all_constants(cal_path):
    consts = dpf.load_constants(cal_path)
    series = dict(SERIES_DEFAULTS)
    if cal_path and Path(cal_path).exists():
        for line in Path(cal_path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip() in series:
                    try:
                        series[k.strip()] = float(v.strip())
                    except ValueError:
                        pass
    return consts, series


def read_header(path):
    hdr, first_freq = {}, None
    with open(path) as fh:
        past = False
        for line in fh:
            if line.startswith("#"):
                if ":" in line:
                    k, v = line[1:].split(":", 1)
                    hdr[k.strip()] = v.strip()
                continue
            if not past:
                cols = line.strip().split(",")
                hdr["_cols"] = cols
                past = True
                continue
            try:
                idx = hdr["_cols"].index("frequency_hz")
                first_freq = float(line.split(",")[idx])
            except (ValueError, IndexError):
                pass
            break
    return hdr, first_freq


def classify(path):
    hdr, f0 = read_header(path)
    cols = hdr.get("_cols", [])
    if "C-V" in hdr.get("sweep_type", ""):
        return "cv_manual" if "current_range_a" in cols else "cv_engine"
    if "manual-poll" in hdr.get("method", ""):
        return "filtered"
    if f0 is not None and f0 < 335:
        return "filtered"
    return "bypass_wideband"


def series_comp(path, out_dir, series):
    """Bypass wideband: subtract chain series parasitics."""
    hdr_lines, rows = [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                hdr_lines.append(line.rstrip("\n"))
            else:
                rows = list(csv.DictReader([line.rstrip("\n")] + fh.read().splitlines()))
                break
    out = out_dir / path.name.replace(".csv", "_seriescomp.csv")
    cols = list(rows[0].keys()) if rows else []
    with open(out, "w", newline="") as fh:
        for h in hdr_lines:
            fh.write(h + "\n")
        fh.write(f"# SERIES-COMP: Z - (R_s + jwL_s), R_s={series['R_s_ohm']} ohm, "
                 f"L_s={series['L_s_h']*1e6:.2f} uH (ISS short 2026-07-15; "
                 f"pipeline v{PIPE_VERSION}, run {time.strftime('%Y-%m-%dT%H:%M:%S')})\n")
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            try:
                f = float(r["frequency_hz"])
                z = complex(float(r["re_z_ohm"]), float(r["im_z_ohm"]))
            except (KeyError, ValueError):
                w.writerow(r)
                continue
            z -= complex(series["R_s_ohm"], 2*math.pi*f*series["L_s_h"])
            d = dpf.derived_columns(z, f, float(r.get("bias_v", 0.0)))
            for k in list(r.keys()):
                if k in d:
                    r[k] = f"{d[k]:.9g}"
            w.writerow(r)
    return out.name


def process_session(sdir, out_name, consts, series, manifest):
    sdir = Path(sdir)
    out_dir = sdir / out_name
    out_dir.mkdir(exist_ok=True)
    for f in sorted(sdir.glob("*.csv")):
        if "_deembedded" in f.stem or "_seriescomp" in f.stem:
            continue
        kind = classify(f)
        if kind in ("filtered", "cv_manual"):
            ok = dpf.process_file(f, consts, "_deembedded")
            gen = f.with_name(f.stem + "_deembedded.csv")
            if ok and gen.exists():
                shutil.move(str(gen), out_dir / gen.name)
                manifest.append((f.name, "pi-filter de-embed"
                                 + (" (C-V per-row Z_in)" if kind == "cv_manual" else "")))
            else:
                manifest.append((f.name, "SKIPPED (see stdout)"))
        elif kind == "bypass_wideband":
            name = series_comp(f, out_dir, series)
            manifest.append((f.name, f"series comp -> {name}"))
        else:  # cv_engine
            shutil.copy2(f, out_dir / f.name)
            manifest.append((f.name, "copied unmodified (1 kHz engine C-V: "
                                     "series comp negligible, +j0.05 ohm)"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="session dir, or root containing session subdirs")
    ap.add_argument("--cal", default=str(DEFAULT_CAL))
    ap.add_argument("--out-name", default="compensated")
    args = ap.parse_args()
    consts, series = load_all_constants(args.cal)
    print(f"pipeline v{PIPE_VERSION}: R={consts['R_ohm']} ohm, "
          f"C1={consts['C1_farad']*1e9:.2f} nF, C2={consts['C2_farad']*1e9:.2f} nF, "
          f"R_s={series['R_s_ohm']} ohm, L_s={series['L_s_h']*1e6:.2f} uH")
    root = Path(args.root)
    subs = [d for d in sorted(root.iterdir()) if d.is_dir()
            and list(d.glob("*.csv"))] or [root]
    manifest = []
    for s in subs:
        print(f"== {s}")
        process_session(s, args.out_name, consts, series, manifest)
    mf = root / "CORRECTIONS_APPLIED.md"
    with open(mf, "w") as fh:
        fh.write(f"# Corrections manifest — pipeline v{PIPE_VERSION}, "
                 f"run {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        fh.write(f"Constants: `{args.cal}` — R={consts['R_ohm']} Ω, "
                 f"C1=C2={consts['C1_farad']*1e9:.2f} nF, "
                 f"R_s={series['R_s_ohm']} Ω, L_s={series['L_s_h']*1e6:.2f} µH\n\n")
        fh.write("| raw file | correction |\n|---|---|\n")
        for name, what in manifest:
            fh.write(f"| {name} | {what} |\n")
    print(f"manifest: {mf}  ({len(manifest)} files)")


if __name__ == "__main__":
    main()
