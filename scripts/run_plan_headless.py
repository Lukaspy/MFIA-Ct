"""Headless campaign runner — run a measurement plan without the GUI.

    python scripts/run_plan_headless.py <plan.yaml> [bitfile.lvbitx]

Replicates the GUI's CfQueueWorker: connects the MFIA (embedded data server at
mf-dev32369) + the PXI LED, runs each block's CfExperiment, and writes every
SweepResult to that block's output_dir. One block failing does not abort the
rest (overnight-safe). Streams progress with timestamps.

Before the campaign it runs a CONTACT PRE-CHECK (a quick high-f dark sweep): a
landed ~nF DUT reads ~kΩ at 100 kHz, an open probe reads MΩ–GΩ. If the probe is
open it ABORTS rather than record a night of open-circuit garbage — this contact
drifts, so don't trust an unattended start without it.
"""
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from mfia_ct.cf_plan import load_plan
from mfia_ct.hardware import MFIA
from mfia_ct.led_source import PxiLedSource
from mfia_ct.cf_experiment import CfExperiment
from mfia_ct.cf_storage import make_filename, write_sweep_csv

PLAN = sys.argv[1]
BITFILE = (sys.argv[2] if len(sys.argv) > 2 else
           "/home/lukas/Documents/PXI-AWG/"
           "leddriverfpga_FPGATarget_LEDDriverFPGA_gPdx1Ep2TLE.lvbitx")
RESOURCE = "RIO0"
HOST = "mf-dev32369"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def contact_ok(mfia) -> bool:
    """Quick high-f dark sweep at 0 V; True if it reads capacitive (landed)."""
    daq, dev = mfia.daq, mfia.device
    P = f"/{dev}/imps/0"
    try:
        od = int(daq.getInt(f"{P}/output/demod"))
    except Exception:
        od = 1
    daq.set([(f"{P}/enable", 1), (f"{P}/mode", 1), (f"{P}/auto/output", 0),
             (f"/{dev}/sigouts/0/range", 1.0), (f"{P}/output/on", 1),
             (f"/{dev}/sigouts/0/enables/{od}", 1),  # route amplitude to output
             (f"{P}/auto/bw", 1), (f"{P}/output/amplitude", 0.1 * 2 ** 0.5),
             (f"{P}/bias/value", 0.0), (f"{P}/bias/enable", 0),
             (f"{P}/model", 1), (f"{P}/auto/inputrange", 1)])
    daq.sync(); time.sleep(1.0)
    sw = daq.sweep()
    sw.set("device", dev); sw.set("gridnode", f"{P}/freq")
    sw.set("start", 10000.0); sw.set("stop", 300000.0); sw.set("samplecount", 3)
    sw.set("xmapping", 1); sw.set("bandwidthcontrol", 2)
    sw.set("settling/tc", 10.0); sw.set("averaging/sample", 3); sw.set("order", 4)
    sp = f"{P}/sample"; sw.subscribe(sp); sw.execute()
    t0 = time.time()
    while not sw.finished() and time.time() - t0 < 60:
        time.sleep(0.3)
    raw = sw.read(True); sw.unsubscribe("*"); sw.clear()
    b = raw[sp][0][0]
    z = abs(np.asarray(b["realz"], float)[-1] + 1j * np.asarray(b["imagz"], float)[-1])
    log(f"contact check: |Z| @300kHz = {z:.3g} ohm (clean cap ~400 ohm, open >MOhm)")
    return z < 10_000.0


log(f"PLAN: {PLAN}")
configs = load_plan(PLAN)
log(f"{len(configs)} block(s) loaded")
mfia = MFIA("dev32369", server_host=HOST)
mfia.connect()
log(f"MFIA connected ({HOST})")

if not contact_ok(mfia):
    log("ABORT: probe reads OPEN — re-land the probe and confirm a clean high-f "
        "read before launching. (No data written.)")
    mfia.disconnect()
    sys.exit(2)
log("contact OK — starting campaign")

n_ok = n_err = n_sweeps = 0
try:
    for i, cfg in enumerate(configs):
        st = cfg.sweep_type.name
        bias = (f"{cfg.cv_bias.start_v}..{cfg.cv_bias.stop_v}V"
                if st == "C_V" else f"{cfg.bias.values_v}")
        log(f"--- block {i + 1}/{len(configs)}  [{st}] bias={bias} ---")
        out_dir = Path(cfg.run.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
        led = PxiLedSource(bitfile=BITFILE, resource=RESOURCE, use_cal=True)
        try:
            for result in CfExperiment(mfia, cfg, led=led).run():
                path = out_dir / make_filename(result.metadata)
                write_sweep_csv(result, path)
                n_sweeps += 1
                log(f"    wrote {path.name}")
            n_ok += 1
            log(f"  block {i + 1} OK")
        except Exception as e:
            n_err += 1
            log(f"  BLOCK {i + 1} FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
finally:
    try:
        mfia.disconnect()
    except Exception:
        pass
    log(f"DONE: {n_ok} block(s) ok, {n_err} failed, {n_sweeps} sweeps written")
