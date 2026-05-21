# MFIA-Ct

Photo-capacitance transient (C-t) measurement GUI for the Zurich Instruments
MFIA 500 kHz / 5 MHz impedance analyzer. Targeted at semiconductor device
characterization where you hold the DUT at constant DC bias, apply an optical
pulse, and record C(t) and G(t) through the light-on / light-off transients.

## What it does

- Configures the MFIA's Impedance Analyzer (IA) module — test frequency, AC
  drive amplitude, DC bias, equivalent circuit model.
- Continuously streams `Cp` and `Gp` from the IA `sample` node at the demod
  rate via `daq.subscribe` + `daq.poll`.
- A background thread software-paces an Aux Out as the optical pulse train,
  recording the wall-clock time of every pulse edge.
- Live-plots one continuous Cp(t) and Gp(t) curve with vertical markers at
  each pulse time, so pulse-to-pulse evolution (drift, fatigue, recovery) is
  immediately visible.
- Saves runs to HDF5: `stream/{t,cp,gp}` plus a `pulse_times` array, with the
  full config as JSON metadata.

## Architecture

There's no triggered acquisition. The MFIA records continuously; the host
software paces the LED pulses on Aux Out N and timestamps each edge. The
floor on pulse period is set by demod rate (sub-ms easily) and host-side
toggle jitter (~1 ms). For µs-precision pulse timing, drive the LED from an
external function generator instead and feed its sync into Aux In so it
gets recorded alongside Cp / Gp — the current GUI doesn't surface that path
but the hardware backend supports it.

Note: front-panel Aux Outputs are labeled **1–4**; the API addresses them as
indices **0–3**. The GUI dropdown shows the front-panel labels.

A mock hardware backend lets you run the entire GUI without a connected MFIA —
useful for development and dry-running an experiment plan before hitting the
bench.

## Install

```bash
# Without hardware (mock only — useful for GUI dev):
pip install -e ".[dev]"

# With the MFIA hardware backend:
pip install -e ".[dev,hardware]"
```

The `zhinst` package requires the LabOne Data Server to be running locally
(default port 8004). See the MFIA user manual §2.5–2.7 for setup.

## Run

```bash
# Mock backend (no MFIA needed):
mfia-ct --mock

# Real device:
mfia-ct --device DEV3000
```

## Project layout

```
src/mfia_ct/
  config.py          Dataclasses describing an experiment.
  hardware.py        Real MFIA backend via zhinst.
  mock_hardware.py   Synthetic backend that generates plausible C-t traces.
  acquisition.py     DAQ module wrapper — triggered, averaged acquisition.
  experiment.py      Orchestrates IA + DAQ + pulse generation.
  storage.py         HDF5 save with metadata.
  gui/app.py         PyQt6 main window.
tests/               Smoke tests against the mock backend.
```

## Hardware setup

See the MFIA user manual at `../documentation/ziMFIA_UserManual.pdf`. The
relevant sections for this tool:

- §4.2 Advanced Impedance Measurements (p.60)
- §4.3 Compensation (p.64) — run Open/Short before each session
- §5.3 Impedance Analyzer Tab (p.93)
- §5.7 Data Acquisition Tab (p.116)
- §5.10 Auxiliary Tab (p.132)

## Status

v0 — minimal end-to-end loop. Roadmap: optical-power sweep, on-the-fly
exponential fitting, temperature integration.
