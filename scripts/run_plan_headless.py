"""Headless campaign runner — run a measurement plan without the GUI.

    # MFIA (default):
    python scripts/run_plan_headless.py <plan.yaml> [--bitfile <lvbitx>]

    # B1500 over GPIB:
    python scripts/run_plan_headless.py <plan.yaml> \
        --instrument b1500 --resource GPIB0::17::INSTR

Connects the chosen analyzer + the PXI LED, runs each block's CfExperiment, and
writes every result to that block's output_dir. One block failing does not abort
the rest (overnight-safe). Streams progress with timestamps.

Analyzer per session (pick one): the MFIA (embedded data server at mf-dev32369)
or the B1500 (VISA/GPIB; the FLEX command set has no native LAN socket, so
"network" means a GPIB↔LAN gateway resource string). C-f / C-V run on either;
I-V (SMU) blocks need the B1500. Data is filed with an instrument prefix
(MFIA_* / B1500_*) so a device's MFIA and B1500 data don't collide.

For the MFIA a CONTACT PRE-CHECK (quick high-f dark sweep) guards an unattended
start: a landed ~nF DUT reads ~kΩ at 100 kHz, an open probe reads MΩ–GΩ; if open
it ABORTS rather than record a night of garbage.
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from mfia_ct.cf_plan import load_plan
from mfia_ct.led_source import PxiLedSource
from mfia_ct.cf_experiment import CfExperiment
from mfia_ct.cf_storage import IvResult, make_filename, write_iv_csv, write_sweep_csv

DEFAULT_BITFILE = ("/home/lukas/Documents/PXI-AWG/"
                   "leddriverfpga_FPGATarget_LEDDriverFPGA_gPdx1Ep2TLE.lvbitx")


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


# --- Reed filter switcher (JWS-117-10 trio, driven via MFIA Aux Outs) -------
# AS-BUILT WIRING (2026-07-10): panel Aux Out 1 (index 0) -> RF1+RF2 (filter
# reeds); panel Aux Out 2 (index 1) -> RD (bypass reed).
# "out" = bypass energized (filter fully lifted). "in" = filter reeds energized.
# Break-before-make; reed operate ~1.5 ms, we allow 500 ms. Only called between
# blocks, never mid-sweep.
AUX_BYPASS = 1
AUX_FILTER = 0
_GATE_ON_V = 10.0
_last_filter_state = [None]   # cache: skip no-op toggles (a toggle floats the
                              # LCUR line ~0.5 s -> bias-perturbs the DUT, which
                              # then fluctuates for minutes; diagnosed 2026-07-12)


def set_filter_state(backend, state, log_fn):
    if state == _last_filter_state[0]:
        log_fn(f"    switcher: filter {state.upper()} (already set - no toggle)")
        return
    daq = backend.daq
    dev = backend.device

    def aux(ch, volts):
        daq.set([
            (f"/{dev}/auxouts/{ch}/outputselect", -1),   # manual mode
            (f"/{dev}/auxouts/{ch}/offset", float(volts)),
        ])

    # MAKE-BEFORE-BREAK: close the target path first, then open the old one.
    # Momentary both-closed = filter shorted by bypass (harmless, signal stays
    # connected); the DUT low terminal NEVER floats.
    if state == "out":
        aux(AUX_BYPASS, _GATE_ON_V); daq.sync(); time.sleep(0.5)
        aux(AUX_FILTER, 0.0)
    elif state == "in":
        aux(AUX_FILTER, _GATE_ON_V); daq.sync(); time.sleep(0.5)
        aux(AUX_BYPASS, 0.0)
    else:
        raise ValueError(f"bad filter_state {state!r}")
    daq.sync(); time.sleep(0.5)
    _last_filter_state[0] = state
    log_fn(f"    switcher: filter {state.upper()} (make-before-break)")


def _sweep_roughness(path):
    """Median geometric-neighbor residual of |Z| (%) for a written C-f CSV."""
    import math as _m
    v=[]
    with open(path) as fh:
        past_hdr=False
        for line in fh:
            if line.startswith("#"): continue
            if not past_hdr: past_hdr=True; continue
            try: v.append(float(line.split(",")[1]))
            except (ValueError, IndexError): pass
    if len(v)<4: return 0.0
    res=[abs(v[i]-_m.sqrt(v[i-1]*v[i+1]))/_m.sqrt(v[i-1]*v[i+1])*100
         for i in range(1,len(v)-1) if v[i-1]>0 and v[i+1]>0]
    res.sort()
    return res[len(res)//2] if res else 0.0


def _block_gate_limit(cfg):
    lo = cfg.sweeper.start_hz if getattr(cfg,"sweeper",None) else None
    if lo is None: return None          # C-V / I-V blocks: no roughness gate
    return 10.0 if lo < 34 else (5.0 if lo < 1100 else 3.0)


def connect_backend(args):
    """Open the selected analyzer and return it, or exit on a failed pre-check."""
    if args.instrument == "b1500":
        from mfia_ct.b1500 import B1500

        backend = B1500(args.resource)
        backend.connect()
        log(f"B1500 connected ({args.resource}): {backend.idn()}")
        log(f"  CMU channel = {backend._cmu}, SMU channel = {backend._smu}")
        if backend._cmu is None and backend._smu is None:
            log("WARNING: no CMU/SMU discovered from UNT? — pass explicit "
                "channels or check the mainframe.")
        return backend

    from mfia_ct.hardware import MFIA

    backend = MFIA(args.device, server_host=args.host)
    backend.connect()
    log(f"MFIA connected ({args.host})")
    if not args.no_contact_check and not contact_ok(backend):
        log("ABORT: probe reads OPEN — re-land the probe and confirm a clean "
            "high-f read before launching. (No data written.)")
        backend.disconnect()
        sys.exit(2)
    return backend


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("plan", help="measurement plan YAML")
    p.add_argument("--instrument", choices=["mfia", "b1500"], default="mfia",
                   help="analyzer for this session (default: mfia)")
    p.add_argument("--resource", default="GPIB0::18::INSTR",
                   help="VISA resource for --instrument b1500")
    p.add_argument("--device", default="dev32369", help="MFIA device id")
    p.add_argument("--host", default="mf-dev32369", help="MFIA data-server host")
    p.add_argument("--bitfile", default=DEFAULT_BITFILE, help="LED FPGA .lvbitx")
    p.add_argument("--led-resource", default="RIO0", help="NI-RIO resource")
    p.add_argument("--no-contact-check", action="store_true",
                   help="skip the MFIA contact pre-check")
    args = p.parse_args()

    log(f"PLAN: {args.plan}  (instrument: {args.instrument})")
    configs = load_plan(args.plan)
    log(f"{len(configs)} block(s) loaded")

    backend = connect_backend(args)
    log("starting campaign")

    n_ok = n_err = n_sweeps = 0
    try:
        for i, cfg in enumerate(configs):
            st = cfg.sweep_type.name
            if st == "C_V":
                axis = f"{cfg.cv_bias.start_v}..{cfg.cv_bias.stop_v}V"
            elif st == "I_V":
                axis = f"IV {cfg.iv.start_v}..{cfg.iv.stop_v}V"
            else:
                axis = f"bias={cfg.bias.values_v}"
            log(f"--- block {i + 1}/{len(configs)}  [{st}] {axis} ---")
            out_dir = Path(cfg.run.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
            fs = getattr(cfg.run, "filter_state", None)
            if fs is not None and args.instrument == "mfia":
                set_filter_state(backend, fs, log)
            elif args.instrument == "mfia":
                log("    switcher: UNTOUCHED (no filter_state in plan) — verify "
                    "hardware state is what this block expects!")
            led = PxiLedSource(bitfile=args.bitfile, resource=args.led_resource, use_cal=True)
            try:
                block_paths = []
                for result in CfExperiment(backend, cfg, led=led).run():
                    path = out_dir / make_filename(result.metadata)
                    if isinstance(result, IvResult):
                        write_iv_csv(result, path)
                    else:
                        write_sweep_csv(result, path)
                        block_paths.append(path)
                    n_sweeps += 1
                    log(f"    wrote {path.name}")
                # RUNTIME ROUGHNESS GATE (audit 2026-07-14): requeue a dirty
                # block ONCE instead of discovering it offline.
                lim = _block_gate_limit(cfg)
                if lim and not getattr(cfg, "_requeued", False):
                    dirty = [(p, _sweep_roughness(p)) for p in block_paths]
                    dirty = [(p, r) for p, r in dirty if r > lim]
                    if dirty:
                        for p, r in dirty:
                            log(f"    ROUGHNESS-GATE: {p.name} = {r:.1f}% "
                                f"(gate {lim:g}%) — block will requeue once")
                        cfg._requeued = True
                        configs.insert(i + 1, cfg)
                n_ok += 1
                log(f"  block {i + 1} OK")
            except Exception as e:
                n_err += 1
                log(f"  BLOCK {i + 1} FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        try:
            backend.disconnect()
        except Exception:
            pass
        log(f"DONE: {n_ok} block(s) ok, {n_err} failed, {n_sweeps} sweeps written")


if __name__ == "__main__":
    main()
