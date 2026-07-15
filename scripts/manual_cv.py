#!/usr/bin/env python3
"""Manual-method C-V v3 (rested-0V halves, banded dynamic pins) at a fixed low frequency (sub-kHz C-V — the sweeper/CV
engine is not used; the manual demod-poll method is the validated instrument
for filtered measurements). Sweeps bias BOTH directions (hysteresis = data).
Dynamic pin per point (I_pk < 70% FS). Filter must be IN for f <= 340 Hz.

Usage: manual_cv.py <freq_hz> <out_dir> [n_points_per_direction] [vmax] [tag]
"""
import sys, time, math
import os
import numpy as np
from zhinst.core import ziDAQServer
DEV_ID = os.environ.get("MFIA_DEVICE_ID", "2013-3")
SUBSTRATE = os.environ.get("MFIA_SUBSTRATE", "n-Si")

F = float(sys.argv[1])
OUT = sys.argv[2]
NPTS = int(sys.argv[3]) if len(sys.argv) > 3 else 141
VMAX = float(sys.argv[4]) if len(sys.argv) > 4 else 7.0
TAG = sys.argv[5] if len(sys.argv) > 5 else "CV"
P = "/dev32369/imps/0/"; AMP = 0.030
PINS = [1.0e-8, 1.0e-7, 1.0e-6, 1.0e-5]

daq = ziDAQServer("mf-dev32369", 8004, 6); daq.getInt(P+"enable")
dev = "dev32369"
for ch, v in ((1, 0.0), (0, 10.0)):   # filter IN (f <= 340 Hz use only)
    daq.set([(f"/{dev}/auxouts/{ch}/outputselect", -1),
             (f"/{dev}/auxouts/{ch}/offset", float(v))])
daq.set([[P+"enable",1],[P+"mode",1],[P+"model",0],[P+"auto/output",0],
         [P+"output/amplitude",AMP*2**0.5],[P+"auto/bw",1],
         [P+"bias/enable",1],[P+"output/on",1]])
daq.setDouble(P+"output/range",10.0); daq.sync()

def ramp(t):
    c = daq.getDouble(P+"bias/value"); n = max(1, int(abs(t-c)/0.5+0.5))
    for i in range(1, n+1):
        daq.setDouble(P+"bias/value", c+(t-c)*i/n); daq.sync(); time.sleep(0.25)

def setpin(p):
    daq.setInt(P+"auto/inputrange",0); daq.setDouble(P+"current/range",p)
    daq.sync(); time.sleep(1.0)

def rd():
    daq.subscribe(P+"sample"); daq.sync()
    d = daq.poll(max(2.0, 6.0/F), 500, 0, True); daq.unsubscribe(P+"sample")
    s = d.get(P+"sample")
    return complex(np.mean(s["z"])) if s is not None and len(s.get("z",[]))>0 else None

daq.setDouble(P+"freq", F); daq.sync()
# v3 (audit 2026-07-14): each half-sweep STARTS from rested 0 V so the V=0
# point anchors to the C-f composite; hysteresis reported via the return legs.
ramp(0.0); time.sleep(240)
neg = list(np.linspace(0, -VMAX, (NPTS+1)//2))
pos = list(np.linspace(0,  VMAX, (NPTS+1)//2))
sweep = ([(b,"neg_out") for b in neg] + [(b,"neg_back") for b in reversed(neg)]
         + [("REST", "rest")]
         + [(b,"pos_out") for b in pos] + [(b,"pos_back") for b in reversed(pos)])
pin = 1.0e-6; setpin(pin)
rows = []
t0 = time.time()
for b, direction in sweep:
    if direction == "rest":              # rested-0V pause between half-sweeps
        ramp(0.0); time.sleep(300); continue
    daq.setDouble(P+"bias/value", b); daq.sync()
    time.sleep(max(10.0, 20.0/F))        # C-V settle law: >= 10 s per bias step
    z = rd()
    if z is None: continue
    ipk = 2**0.5*AMP/abs(z)
    # banded dynamic pin: AT MOST one shift per point (70% up / 60%-guard down)
    if ipk > 0.70*pin and PINS.index(pin)+1 < len(PINS):
        pin = PINS[PINS.index(pin)+1]; setpin(pin); z = rd()
    elif ipk < 0.05*pin and PINS.index(pin) > 0 and 2**0.5*AMP/abs(z) < 0.60*PINS[PINS.index(pin)-1]:
        pin = PINS[PINS.index(pin)-1]; setpin(pin); z = rd()
    if z is not None:
        rows.append((b, direction, z, pin))
ramp(0.0); daq.setInt(P+"bias/enable",0); daq.setInt(P+"auto/inputrange",1); daq.sync()

ts = time.strftime("%Y%m%d_%H%M%S")
fn = f"{OUT}/MFIA_CV_{DEV_ID}_f{F:g}Hz_dark_manual_{ts}.csv"
with open(fn, "w") as fh:
    fh.write(f"# device_id: {DEV_ID}\n# substrate_type: {SUBSTRATE}\n# sweep_type: C-V\n")
    fh.write(f"# frequency_hz: {F}\n# illumination_label: dark\n")
    fh.write(f"# amplitude_vrms: {AMP}\n")
    fh.write("# current_range_a: per-point (column)\n")
    fh.write("# method: manual-poll C-V (both directions; hysteresis = data)\n")
    fh.write(f"# pass: {TAG}\n# timestamp: {ts}\n# duration_s: {time.time()-t0:.0f}\n")
    fh.write("bias_v,direction,frequency_hz,z_mag_ohm,phase_deg,g_siemens,b_siemens,cp_f,rp_ohm,re_z_ohm,im_z_ohm,current_range_a\n")
    for b, direction, z, p in rows:
        w = 2*math.pi*F; y = 1/z
        cp = y.imag/w; rp = 1/y.real if y.real != 0 else float("inf")
        fh.write(f"{b:.4f},{direction},{F},{abs(z):.6g},"
                 f"{math.degrees(math.atan2(z.imag,z.real)):.4f},{y.real:.6g},"
                 f"{y.imag:.6g},{cp:.6g},{rp:.6g},{z.real:.6g},{z.imag:.6g},{p:g}\n")
print(f"[{time.strftime('%H:%M:%S')}] manual C-V {F:g} Hz: {len(rows)} pts "
      f"({time.time()-t0:.0f} s) -> {fn.split('/')[-1]}")
