#!/usr/bin/env python3
"""Bench QA for MFIA C-f / C-V sweeps — catch compensation / ranging trouble.

Run it on a freshly-recompensated open and dark sweep before committing to a
long campaign. It flags the three fingerprints of a bad/disturbed user
compensation or auto-range stitching that we diagnosed on 2013-3:

  1. NEGATIVE conductance (G < 0)        -> physically impossible for a passive
                                            DUT; means comp is over-correcting
                                            or invalid at that input range.
  2. Large adjacent |Z| jumps (>5x)      -> auto-input-range steps the comp
                                            doesn't stitch across.
  3. |phase| never reaching the cap side -> loss-dominated across the band, not
                                            just a genuine low-f tail.

A clean, well-compensated sweep is CLEAN: G >= 0 everywhere, |Z| smooth, and
|phase| climbs toward 90 deg in the mid-band.

Usage:
    python scripts/qa_check.py <file.csv | folder>
    python scripts/qa_check.py ~/.../2013-3_n-Si        # whole folder
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys

JUMP_RATIO = 5.0      # adjacent |Z| ratio that counts as a range step
LOSS_DEG = 45.0       # |phase| below this is loss-dominated


def _read(path: str):
    rows = [ln for ln in open(path) if not ln.startswith("#")]
    out = {"f": [], "phase": [], "g": [], "z": []}
    for r in csv.DictReader(rows):
        try:
            out["f"].append(float(r["frequency_hz"]))
            out["phase"].append(float(r["phase_deg"]))
            out["g"].append(float(r["g_siemens"]))
            out["z"].append(float(r["z_mag_ohm"]))
        except (ValueError, KeyError):
            continue
    return out


def check(path: str) -> dict:
    d = _read(path)
    n = len(d["f"])
    if n == 0:
        return {"name": os.path.basename(path), "empty": True}
    neg_g = sum(1 for v in d["g"] if v < 0)
    max_abs_phase = max(abs(p) for p in d["phase"])
    # A passive cap-dominated DUT has phase in [-90, 0]. Positive phase =
    # inductive-looking = unphysical here (low-f noise / artifact). The most
    # negative phase says how capacitive it ever gets.
    inductive = sum(1 for p in d["phase"] if p > 10.0)
    min_phase = min(d["phase"])
    reaches_cap = min_phase <= -LOSS_DEG
    # Adjacent |Z| jumps, ordered by frequency.
    order = sorted(zip(d["f"], d["z"]))
    worst_jump, jump_at = 1.0, None
    for (f1, z1), (f2, z2) in zip(order, order[1:]):
        if z1 > 0 and z2 > 0:
            ratio = max(z1 / z2, z2 / z1)
            if ratio > worst_jump:
                worst_jump, jump_at = ratio, (f1, f2)
    # Loss crossover: highest frequency that is still loss-dominated (the
    # capacitive band sits above it). None means the whole sweep is capacitive.
    cross = None
    for f, p in sorted(zip(d["f"], d["phase"]), reverse=True):  # high -> low
        if abs(p) < LOSS_DEG:
            cross = f
            break
    clean = (neg_g == 0 and worst_jump <= JUMP_RATIO and reaches_cap
             and inductive == 0)
    return {
        "name": os.path.basename(path), "n": n, "neg_g": neg_g,
        "max_abs_phase": max_abs_phase, "worst_jump": worst_jump,
        "jump_at": jump_at, "cross": cross, "clean": clean, "empty": False,
        "inductive": inductive, "min_phase": min_phase,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="A sweep CSV, or a folder of MFIA_*.csv files.")
    args = ap.parse_args(argv)

    if os.path.isdir(args.path):
        files = sorted(glob.glob(os.path.join(args.path, "MFIA_*.csv")))
    else:
        files = [args.path]
    if not files:
        print("No MFIA_*.csv files found.", file=sys.stderr)
        return 2

    results = [check(f) for f in files]
    results = [r for r in results if not r.get("empty")]
    flagged = [r for r in results if not r["clean"]]

    multi = len(results) > 1
    if not multi:
        r = results[0]
        verdict = "CLEAN ✅" if r["clean"] else "FLAGGED ❌"
        print(f"{r['name']}: {verdict}")
        print(f"  points              : {r['n']}")
        print(f"  negative-G points   : {r['neg_g']}   "
              f"{'(comp over-correcting / invalid range)' if r['neg_g'] else '(ok)'}")
        print(f"  worst |Z| jump      : {r['worst_jump']:.1f}x"
              + (f" between {r['jump_at'][0]:.3g}–{r['jump_at'][1]:.3g} Hz"
                 f" (auto-range step)" if r["worst_jump"] > JUMP_RATIO and r["jump_at"]
                 else " (ok)"))
        print(f"  inductive low-f pts : {r['inductive']}   "
              f"{'(positive phase — unphysical artifact)' if r['inductive'] else '(none — good)'}")
        print(f"  most capacitive ϕ   : {r['min_phase']:.0f}°   "
              f"{'(reaches a real capacitive band)' if r['min_phase'] <= -LOSS_DEG else '(never truly capacitive)'}")
        if r["cross"] is not None:
            print(f"  loss crossover      : ~{r['cross']:.3g} Hz "
                  f"(capacitive above, loss-dominated below)")
        return 0 if r["clean"] else 1

    print(f"{'file':<50} {'neg-G':>6} {'ind':>5} {'jump':>7} {'min ϕ':>6}  verdict")
    for r in results:
        v = "CLEAN" if r["clean"] else "FLAG"
        print(f"{r['name'][:50]:<50} {r['neg_g']:>6} {r['inductive']:>5} "
              f"{r['worst_jump']:>6.1f}x {r['min_phase']:>5.0f}°  {v}")
    print(f"\n{len(results)-len(flagged)}/{len(results)} CLEAN, {len(flagged)} flagged.")
    if flagged:
        ng = sum(1 for r in flagged if r["neg_g"])
        ind = sum(1 for r in flagged if r["inductive"])
        jp = sum(1 for r in flagged if r["worst_jump"] > JUMP_RATIO)
        print(f"  {ng} with negative G, {ind} with inductive (positive-phase) low-f "
              f"points, {jp} with range jumps.")
    return 0 if not flagged else 1


if __name__ == "__main__":
    sys.exit(main())
