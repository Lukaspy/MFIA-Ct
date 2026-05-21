# MFIA-Ct

Photo-capacitance transient (C-t) measurement GUI for the Zurich Instruments
MFIA 500 kHz / 5 MHz impedance analyzer. Targeted at semiconductor device
characterization where you hold the DUT at constant DC bias, apply an optical
pulse, and record C(t) and G(t) through the light-on / light-off transients.

## What it does

- Configures the MFIA's Impedance Analyzer (IA) module — test frequency, AC
  drive amplitude, DC bias, equivalent circuit model.
- Triggers the Data Acquisition (DAQ) module on a TTL edge — either generated
  by the MFIA's own Aux Out (internal pulse mode) or supplied by an external
  laser/LED driver (external trigger mode).
- Subscribes to `Cp` and `Gp` from the IA `sample` node, records aligned
  segments around each trigger, averages across repetitions.
- Live-plots C(t) and G(t) traces in PyQt6 + pyqtgraph.
- Saves runs to HDF5 with full instrument settings as metadata.

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
