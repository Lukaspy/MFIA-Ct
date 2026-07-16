#!/usr/bin/env python3
"""Contact fingerprint check — run at every chain start and after any bench
visit. Replaces the old 300 kHz pre-check that was disabled (--no-contact-check)
on all filtered-era runs because it read wrong through the pi filter; this one
sets the BYPASS state itself, so it is always valid.

Compares the high-frequency bypass signature against the device's stored
fingerprint (written by landing_scout / updated on a human-verified landing).
Contact failures move |Z| by orders of magnitude; state-family drift moves it
by factors of a few — thresholds are set accordingly ([1/5x, 5x] pass band).

Usage:  contact_check.py <device_id> [--update]
  --update : store the current signature as the new fingerprint (use ONLY on a
             landing the operator has verified good).
Exit 0 = contact consistent with fingerprint; 2 = FAIL; 3 = no fingerprint.
Fingerprint file: ~/Documents/capacitance_measurements/<dev>_scout/CONTACT_FINGERPRINT.txt
"""
import sys, time, math, os
import numpy as np
from zhinst.core import ziDAQServer

DEV_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MFIA_DEVICE_ID", "unknown")
UPDATE = "--update" in sys.argv
FPFILE = os.path.expanduser(
    f"~/Documents/capacitance_measurements/{DEV_ID}_scout/CONTACT_FINGERPRINT.txt")
FREQS = (300000.0, 100000.0, 10000.0)
P = "/dev32369/imps/0/"

daq = ziDAQServer("mf-dev32369", 8004, 6); daq.getInt(P + "enable")
dev = "dev32369"
# remember filter state, set BYPASS for the check, restore after
prior = [daq.getDouble(f"/{dev}/auxouts/{ch}/offset") for ch in (0, 1)]
for ch, v in ((0, 0.0), (1, 10.0)):
    daq.set([(f"/{dev}/auxouts/{ch}/outputselect", -1),
             (f"/{dev}/auxouts/{ch}/offset", float(v))])
daq.set([[P+"enable",1],[P+"mode",1],[P+"model",0],[P+"auto/output",0],
         [P+"output/amplitude",0.03*2**0.5],[P+"auto/bw",1],
         [P+"auto/inputrange",1],[P+"bias/enable",0],[P+"output/on",1]])
daq.sync(); time.sleep(1.5)

sig = {}
for f in FREQS:
    daq.setDouble(P+"freq", f); daq.sync(); time.sleep(3)
    daq.subscribe(P+"sample"); daq.sync()
    d = daq.poll(2.5, 500, 0, True); daq.unsubscribe(P+"sample")
    s = d.get(P+"sample")
    if s is None or len(s.get("z", [])) == 0:
        continue
    z = complex(np.mean(s["z"]))
    sig[f] = abs(z)
    print(f"  {f/1e3:6.0f} kHz: |Z| = {abs(z):.4g}  ph = "
          f"{math.degrees(math.atan2(z.imag, z.real)):+.1f}")
# restore prior filter state
for ch, v in ((0, prior[0]), (1, prior[1])):
    daq.set([(f"/{dev}/auxouts/{ch}/offset", float(v))])
daq.sync()

if UPDATE:
    os.makedirs(os.path.dirname(FPFILE), exist_ok=True)
    with open(FPFILE, "w") as fh:
        fh.write(f"# contact fingerprint for {DEV_ID}, stored "
                 f"{time.strftime('%Y-%m-%d %H:%M')} (operator-verified landing)\n")
        for f, z in sig.items():
            fh.write(f"{f:g}: {z:.6g}\n")
    print(f"fingerprint stored: {FPFILE}")
    sys.exit(0)

if not os.path.exists(FPFILE):
    print(f"NO FINGERPRINT for {DEV_ID} — run with --update on a verified landing")
    sys.exit(3)
ref = {}
for line in open(FPFILE):
    line = line.split("#", 1)[0].strip()
    if ":" in line:
        k, v = line.split(":", 1)
        ref[float(k)] = float(v)
fails = []
for f, z in sig.items():
    if f in ref:
        ratio = z / ref[f]
        ok = 0.2 <= ratio <= 5.0
        print(f"  {f/1e3:6.0f} kHz: ratio vs fingerprint = {ratio:.2f}x "
              f"{'OK' if ok else '** FAIL **'}")
        if not ok:
            fails.append(f)
if fails:
    print("CONTACT CHECK FAIL — do not measure; re-land and verify")
    sys.exit(2)
print("CONTACT CHECK PASS")
sys.exit(0)
