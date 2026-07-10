#!/usr/bin/env python3
"""Smoke-test QC: de-embedded seams, per-band roughness, publication-gate verdicts.
Run locally after syncing 2013-3_SMOKE. Bands are identified by frequency span."""
import csv, glob, math, sys
import statistics as st

D = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/lukas/Documents/capacitance_measurements/2013-3_SMOKE"

def rd(fn):
    with open(fn) as fh:
        for line in fh:
            if line.startswith('#'): continue
            return list(csv.DictReader([line]+fh.read().splitlines()))

def rough(rows):
    """RMS % deviation from local log-log quadratic (the plan's §3 metric),
    approximated by median geometric-neighbor residual on |Z|."""
    v=[float(r['z_mag_ohm']) for r in rows]
    res=[]
    for i in range(1,len(v)-1):
        pred=math.sqrt(v[i-1]*v[i+1])
        res.append(abs(v[i]-pred)/pred*100)
    return st.median(res) if res else float('nan')

files=sorted(glob.glob(D+"/MFIA_Cf_*_deembedded.csv"))+ \
      sorted(f for f in glob.glob(D+"/MFIA_Cf_*.csv") if "_deembedded" not in f)
# keep latest instance of each band (smoke ran unfiltered twice)
bands={}
for fn in files:
    rows=rd(fn)
    if not rows or len(rows)<4: continue
    lo,hi=float(rows[0]['frequency_hz']),float(rows[-1]['frequency_hz'])
    key=(round(lo,1),round(hi,1),'deemb' in fn)
    bands[key]=(fn,rows)   # later files overwrite (sorted -> latest wins)

print(f"{'band':>18s} {'src':>6s} {'roughness%':>10s} {'gate':>6s} {'pts':>4s}")
gates=[]
for (lo,hi,de),(fn,rows) in sorted(bands.items()):
    r=rough(rows)
    lim = 10.0 if hi<=34 else (5.0 if hi<=1100 else 2.0)
    ok = r<=lim
    gates.append(ok)
    print(f"{lo:8g}-{hi:8g} {'deemb' if de else 'raw':>6s} {r:10.2f} {'PASS' if ok else 'FAIL':>6s} {len(rows):4d}")

# seam continuity at shared endpoints (use de-embedded for filtered bands)
print()
print("seams (|Z| at shared f, adjacent bands):")
pairs=[(0.3,1.0,1.0,3.4),(1.0,3.4,3.4,34),(3.4,34,34,340),(34,340,340,3400)]
for a_lo,a_hi,b_lo,b_hi in pairs:
    A=[v for (lo,hi,de),v in bands.items() if abs(lo-a_lo)<0.2 and abs(hi-a_hi)<a_hi*0.1 and de]
    B=[v for (lo,hi,de),v in bands.items() if abs(lo-b_lo)<b_lo*0.1 and abs(hi-b_hi)<b_hi*0.1]
    # prefer de-embedded B if exists
    Bde=[v for (lo,hi,de),v in bands.items() if abs(lo-b_lo)<b_lo*0.1 and abs(hi-b_hi)<b_hi*0.1 and de]
    if Bde: B=Bde
    if not A or not B: continue
    za=float(A[0][1][-1]['z_mag_ohm']); fa=float(A[0][1][-1]['frequency_hz'])
    zb=float(B[0][1][0]['z_mag_ohm']); fb=float(B[0][1][0]['frequency_hz'])
    d=(zb-za)/za*100
    print(f"  {a_hi:6g} Hz: {za:10.4g} vs {zb:10.4g}  d={d:+6.2f}%  {'PASS' if abs(d)<=10 else 'FAIL'} (f {fa:g}/{fb:g})")
print()
print("ALL ROUGHNESS GATES:", "PASS" if all(gates) else "CHECK FAILURES")
