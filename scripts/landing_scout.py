#!/usr/bin/env python3
"""Landing scout — run after landing probes on ANY device, before its first
session. Maps |Z|(bias, f) at both polarities (single-visit, rested-order),
recommends per-rung pins (I_pk < 70% FS rule), and flags which polarity is
slow/drifty (settle assignment). Saves a CSV next to the device's data.

Usage: .venv/bin/python scripts/landing_scout.py <device_id> <out_dir>
Safe by construction: 100 uA pin (clip needs |Z| < ~430 ohm), 30 mV, ramped
bias, 30 s settle per rung, dark. ~14 min. Parks 0 V dark.
"""
import sys, time, math
import numpy as np
from zhinst.core import ziDAQServer

DEV_ID = sys.argv[1] if len(sys.argv) > 1 else "unknown"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
P = "/dev32369/imps/0/"
AMP = 0.030
PINS = [(1.0e-8, "10 nA"), (1.0e-7, "100 nA"), (1.0e-6, "1 uA"),
        (1.0e-5, "10 uA"), (1.0e-4, "100 uA"), (1.0e-3, "1 mA")]

daq = ziDAQServer("mf-dev32369", 8004, 6); daq.getInt(P + "enable")
daq.set([[P+"enable",1],[P+"mode",1],[P+"model",0],[P+"auto/output",0],
         [P+"output/amplitude",AMP*2**0.5],[P+"auto/bw",1],
         [P+"auto/inputrange",0],[P+"bias/enable",1],[P+"output/on",1]])
daq.setDouble(P+"output/range",10.0)
daq.setDouble(P+"current/range",1.0e-4)
daq.sync()

def ramp(t):
    c=daq.getDouble(P+"bias/value"); n=max(1,int(abs(t-c)/0.5+0.5))
    for i in range(1,n+1):
        daq.setDouble(P+"bias/value",c+(t-c)*i/n); daq.sync(); time.sleep(0.25)

def rd(f):
    daq.setDouble(P+"freq",f); daq.sync()
    time.sleep(max(4.0, 20.0/f))
    daq.subscribe(P+"sample"); daq.sync()
    d=daq.poll(max(3.0,6.0/f),500,0,True); daq.unsubscribe(P+"sample")
    s=d.get(P+"sample")
    if s is None or len(s.get("z",[]))==0: return None
    return complex(np.mean(s["z"]))

FREQS=(1.0,3.0,10.0,30.0,100.0,300.0)
# rested order: 0 first, then alternate polarity outward (each rung one visit)
BIASES=(0.0,1.0,-1.0,2.0,-2.0,4.0,-4.0,7.0,-7.0)
rows=[]
print(f"landing scout: {DEV_ID}")
print("bias    freq      |Z|        phase     I_pk@30mV  (%% of 100uA)")
for b in BIASES:
    ramp(b); time.sleep(30.0)
    for f in FREQS:
        z=rd(f)
        if z is None: continue
        ipk=2**0.5*AMP/abs(z)
        rows.append((b,f,z))
        print(f"{b:+4.0f} V {f:7.1f} Hz {abs(z):10.4g} {np.degrees(np.angle(z)):8.1f} {ipk*1e9:9.1f} nA ({ipk/1e-4*100:5.2f}%)", flush=True)
ramp(0.0); daq.setInt(P+"auto/inputrange",1); daq.sync()

ts=time.strftime("%Y%m%d_%H%M%S")
fn=f"{OUT}/SCOUT_{DEV_ID}_{ts}.csv"
with open(fn,"w") as fh:
    fh.write(f"# landing scout: {DEV_ID}  {ts}\n")
    fh.write("# single-visit rested-order, 30 mV, 100 uA pin, 30 s settles, dark\n")
    fh.write("bias_v,frequency_hz,z_mag_ohm,phase_deg,re_z_ohm,im_z_ohm\n")
    for b,f,z in rows:
        fh.write(f"{b},{f},{abs(z):.6g},{math.degrees(math.atan2(z.imag,z.real)):.3f},{z.real:.6g},{z.imag:.6g}\n")
print(f"\nsaved: {fn}")

print("\n=== pin recommendations per bias (I_pk x1.43 headroom, in accuracy band) ===")
for b in BIASES:
    zs=[(f,abs(z)) for bb,f,z in rows if bb==b]
    if not zs: continue
    ipks=[2**0.5*AMP/z for _,z in zs]
    imax,imin=max(ipks),min(ipks)
    ok=[nm for v,nm in PINS if imax<0.70*v]
    lowocc=[nm for v,nm in PINS if imax<0.70*v and imin>0.01*v]
    pick=(lowocc or ok or ["100 uA+"])[0]
    split="" if lowocc else "  (consider band split: quietest point <1% FS)"
    print(f"  {b:+4.0f} V: I_pk {imin*1e9:7.1f}-{imax*1e9:8.1f} nA -> {pick}{split}")
print("\n=== polarity drift check (compare |Z| stability across own rungs; the")
print("    polarity whose 1-10 Hz phases sit far from -90 or vary rung-to-rung")
print("    is the drifty side -> assign 1800 s settles in that S2 plan) ===")
