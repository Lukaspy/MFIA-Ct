"""Quick photo-C-t transient test: CW LED pulse via the PXI 8-ch driver.

Records Cp(t)/Gp(t) at a fixed probe frequency + DC bias while software-
sequencing one Mightex channel in CW mode:

    [dark baseline] -> (LED on, hold) -> (LED off, recovery) -> ... x repeats

The dynamics of interest are seconds-to-minutes, so ~ms software timing on
the CW edges is more than adequate (no AWG / 33250A needed). Single-threaded:
LED commands and MFIA polling share one loop (zhinst client is not
thread-safe), same pattern as CtExperiment.

Output: one self-describing CSV (header + time_s, cp_f, gp_s, led_state)
compatible with the analysis repo's loaders, plus the tool-native .h5.

Examples:
    # mock end-to-end dry run (no hardware):
    python scripts/run_ct_pulse.py --mock --baseline-s 5 --on-s 8 --off-s 15

    # real quick test, 2013-3 at -2 V, 625 nm 100%:
    python scripts/run_ct_pulse.py --mfia dev5168 --device-id 2013-3 \
        --bias -2 --wavelength 625 --pct 100 \
        --baseline-s 120 --on-s 120 --off-s 900 --repeats 2
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfia_ct.config import (  # noqa: E402
    AcquisitionSettings,
    CtConfig,
    DemodSettings,
    EquivCircuit,
    IASettings,
    PulseSettings,
    PulseSource,
    TerminalMode,
)
from mfia_ct.led_source import MockLedSource, PxiLedSource  # noqa: E402
from mfia_ct.storage import save_run  # noqa: E402

DEFAULT_BITFILE = ("/home/lukas/Documents/PXI-AWG/"
                   "leddriverfpga_FPGATarget_LEDDriverFPGA_gPdx1Ep2TLE.lvbitx")


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true", help="mock MFIA + mock LED")
    p.add_argument("--mfia", default=None, help="MFIA device id, e.g. dev5168")
    p.add_argument("--host", default="localhost", help="LabOne data-server host")
    # sample / conditions
    p.add_argument("--device-id", default="2013-3")
    p.add_argument("--substrate", default="n-Si")
    p.add_argument("--bias", type=float, default=-2.0, help="DC bias (V)")
    p.add_argument("--freq", type=float, default=10e3,
                   help="probe frequency (Hz); 10 kHz keeps Cp AND Gp valid "
                        "in both dark and lit states")
    p.add_argument("--amplitude", type=float, default=0.1,
                   help="AC amplitude V RMS, constant through the transient")
    p.add_argument("--current-range", default="1e-4",
                   help="fixed current range in A (default 100 uA fits dark AND "
                        "lit at 10 kHz); 'auto' = settle-then-freeze")
    # pulse schedule
    p.add_argument("--wavelength", type=float, default=625.0)
    p.add_argument("--pct", type=float, default=100.0)
    p.add_argument("--baseline-s", type=float, default=120.0,
                   help="dark baseline before the first pulse")
    p.add_argument("--on-s", type=float, default=120.0, help="CW hold with LED on")
    p.add_argument("--off-s", type=float, default=900.0,
                   help="dark recovery after each pulse (the money data)")
    p.add_argument("--repeats", type=int, default=2,
                   help="pulses run back-to-back; 2nd starts from the partially-"
                        "recovered state (history-dependence check)")
    # acquisition pacing
    p.add_argument("--sample-rate", type=float, default=50.0,
                   help="demod sample rate (Sa/s); transients are seconds-scale")
    p.add_argument("--tc", type=float, default=0.02,
                   help="demod time constant (s)")
    # LED plumbing
    p.add_argument("--bitfile", default=DEFAULT_BITFILE)
    p.add_argument("--led-resource", default="RIO0")
    p.add_argument("--no-cal", action="store_true",
                   help="drive raw percent (skip led_driver power calibration)")
    p.add_argument("--outdir", default=None,
                   help="default: ~/Documents/capacitance_measurements/<device-id>_ct")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    current_range = None if str(args.current_range).lower() == "auto" \
        else float(args.current_range)

    cfg = CtConfig(
        ia=IASettings(
            frequency_hz=args.freq,
            ac_amplitude_v=args.amplitude,
            dc_bias_v=args.bias,
            equiv_circuit=EquivCircuit.CP_RP,
            terminal_mode=TerminalMode.TWO_TERMINAL,
            current_range_a=current_range,
            light_ac_amplitude_v=None,      # ONE amplitude through the transient
            compensation_enabled=True,
        ),
        demod=DemodSettings(
            time_constant_s=args.tc,
            filter_order=4,
            sample_rate_hz=args.sample_rate,
        ),
        pulse=PulseSettings(
            source=PulseSource.LED_8CH,
            led_wavelength_nm=args.wavelength,
            led_intensity_pct=args.pct,
            pulse_width_s=args.on_s,        # informs the mock's transient shape
            period_s=args.on_s + args.off_s,
            n_pulses=args.repeats,
        ),
        acq=AcquisitionSettings(poll_interval_s=0.5),
        notes=f"CW pulse test: baseline {args.baseline_s}s, "
              f"{args.repeats}x(on {args.on_s}s / off {args.off_s}s)",
    )

    # ---- instruments ----
    if args.mock:
        from mfia_ct.mock_hardware import MockMFIA
        backend = MockMFIA()
        led = MockLedSource()
        log("MOCK mode: MockMFIA + MockLedSource")
    else:
        if not args.mfia:
            print("error: --mfia DEVID required (or use --mock)", file=sys.stderr)
            return 2
        from mfia_ct.hardware import MFIA
        backend = MFIA(args.mfia, server_host=args.host)
        log(f"connecting MFIA {args.mfia} via {args.host} ...")
        backend.connect()
        led = PxiLedSource(bitfile=args.bitfile, resource=args.led_resource,
                           use_cal=not args.no_cal)

    led.connect()
    led.all_off()
    log(f"LED source: {led.idn()}")
    power_mw = led.predicted_power_mw(args.wavelength, args.pct)
    current_ma = led.current_ma_for(args.wavelength, args.pct)

    log("configuring impedance analyzer ...")
    backend.configure_impedance(cfg)
    # The IA impedance stream rate lives on imps/N/demod/rate, NOT on
    # demods/N/rate (which configure_impedance sets) — without this the
    # stream stays pinned at the instrument default (~13.4 kSa/s) no matter
    # what sample-rate is asked for. Write it directly; read back the
    # granted rate (LabOne quantizes).
    if not args.mock and hasattr(backend, "daq"):
        node = f"/{backend.device}/imps/0/demod/rate"
        try:
            backend.daq.set([(node, args.sample_rate)])
            backend.daq.sync()
            granted = backend.daq.getDouble(node)
            log(f"stream rate: asked {args.sample_rate:g}, granted {granted:g} Sa/s")
        except Exception as e:
            log(f"imps demod rate node not writable ({e}); stream at default")

    # ---- schedule (stream time base = seconds since start_continuous) ----
    on_edges = [args.baseline_s + i * (args.on_s + args.off_s)
                for i in range(args.repeats)]
    off_edges = [t + args.on_s for t in on_edges]
    total_s = args.baseline_s + args.repeats * (args.on_s + args.off_s)
    log(f"schedule: baseline {args.baseline_s:.0f}s; ON at "
        f"{[f'{t:.0f}' for t in on_edges]}; total {total_s:.0f}s "
        f"(~{total_s/60:.1f} min)")

    backend.start_continuous()
    t0 = time.monotonic()
    actual_on: list[float] = []
    actual_off: list[float] = []
    ts, cps, gps = [], [], []
    next_on = 0
    next_off = 0
    last_status = 0.0

    try:
        while True:
            now = time.monotonic() - t0
            if now >= total_s and next_off >= args.repeats:
                break

            # LED edges (ms-scale timing is fine for seconds-scale physics)
            if next_on < args.repeats and now >= on_edges[next_on]:
                led.set_intensity(args.wavelength, args.pct)
                t_edge = time.monotonic() - t0
                actual_on.append(t_edge)
                if hasattr(backend, "record_pulse"):
                    backend.record_pulse(t_edge)
                log(f"LED ON  ({args.wavelength:.0f} nm @ {args.pct:g}%)  t={t_edge:.1f}s")
                next_on += 1
            if next_off < len(actual_on) and now >= off_edges[next_off]:
                led.all_off()
                t_edge = time.monotonic() - t0
                actual_off.append(t_edge)
                log(f"LED OFF                       t={t_edge:.1f}s")
                next_off += 1

            chunk = backend.poll_continuous(cfg.acq.poll_interval_s)
            if chunk is not None and chunk.t.size:
                ts.append(chunk.t)
                cps.append(chunk.cp)
                gps.append(chunk.gp)
                if now - last_status > 10.0:
                    log(f"  t={chunk.t[-1]:7.1f}s  Cp={chunk.cp[-1]*1e9:9.3f} nF  "
                        f"Gp={chunk.gp[-1]*1e6:9.3f} uS")
                    last_status = now
    except KeyboardInterrupt:
        log("interrupted — saving what we have")
    finally:
        try:
            led.all_off()
            led.disconnect()
        except Exception:
            pass
        try:
            backend.stop_continuous()
        except Exception:
            pass

    if not ts:
        log("no samples recorded — nothing to save")
        return 1

    t = np.concatenate(ts)
    cp = np.concatenate(cps)
    gp = np.concatenate(gps)

    # led_state from the actual command edges
    led_state = np.zeros_like(t)
    for i, t_on in enumerate(actual_on):
        t_off = actual_off[i] if i < len(actual_off) else np.inf
        led_state[(t >= t_on) & (t < t_off)] = 1.0

    # ---- save ----
    outdir = Path(args.outdir) if args.outdir else Path(
        "~/Documents/capacitance_measurements").expanduser() / f"{args.device_id}_ct"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = (f"MFIA_Ct_{args.device_id}_Vb{args.bias:+g}_{args.wavelength:.0f}nm_"
            f"{args.pct:g}pct_on{args.on_s:.0f}s_{stamp}")

    csv_path = outdir / f"{base}.csv"
    hdr = {
        "device_id": args.device_id,
        "substrate_type": args.substrate,
        "sweep_type": "C-t",
        "bias_v_commanded": args.bias,
        "probe_frequency_hz": args.freq,
        "amplitude_vrms": args.amplitude,
        "current_range_a": args.current_range,
        "wavelength_nm": args.wavelength,
        "led_intensity_pct": args.pct,
        "led_current_ma": "" if current_ma is None else f"{current_ma:.1f}",
        "optical_power_mw": "" if power_mw is None else f"{power_mw:.6f}",
        "baseline_s": args.baseline_s,
        "on_s": args.on_s,
        "off_s": args.off_s,
        "repeats": args.repeats,
        "led_on_times_s": ";".join(f"{x:.3f}" for x in actual_on),
        "led_off_times_s": ";".join(f"{x:.3f}" for x in actual_off),
        "demod_rate_hz": args.sample_rate,
        "demod_tc_s": args.tc,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "notes": cfg.notes,
    }
    with open(csv_path, "w") as f:
        for k, v in hdr.items():
            f.write(f"# {k}: {v}\n")
        f.write("time_s,cp_f,gp_s,led_state\n")
        for i in range(t.size):
            f.write(f"{t[i]:.4f},{cp[i]:.8e},{gp[i]:.8e},{int(led_state[i])}\n")
    log(f"wrote {csv_path}")

    h5_path = outdir / f"{base}.h5"
    save_run(h5_path, cfg, t, cp, gp, actual_on,
             extra={"led_off_times_s": ";".join(f"{x:.3f}" for x in actual_off)})
    log(f"wrote {h5_path}")

    # ---- quick verdict ----
    dark = led_state == 0
    pre = dark & (t < (actual_on[0] if actual_on else np.inf))
    lit = led_state == 1
    if pre.any() and lit.any():
        g0 = np.median(gp[pre])
        g1 = np.median(gp[lit][-max(1, lit.sum() // 10):])  # last 10% of lit
        gend = np.median(gp[-max(1, t.size // 100):])
        log(f"baseline Gp={g0*1e6:.3f} uS | lit plateau Gp={g1*1e6:.3f} uS "
            f"(x{g1/max(g0,1e-30):.1f}) | end-of-record Gp={gend*1e6:.3f} uS "
            f"({(gend-g0)/max(g1-g0,1e-30)*100:.1f}% of light step still present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
