# MFIA-Ct

Measurement GUIs for the Zurich Instruments MFIA (500 kHz / 5 MHz) impedance
analyzer, for semiconductor device photo-characterization. Three tools share
one package:

| Command | Purpose |
|---------|---------|
| `mfia-ct`     | **Photo-capacitance transient (C-t)** — hold DC bias, pulse the LED, record Cp(t)/Gp(t) through light-on/off transients. Optional Agilent 33250A function generator over GPIB. |
| `mfia-cf`     | **C-f / impedance-spectroscopy campaign** — automated bias × illumination × frequency-sweep loops via the LabOne Sweeper, one tidy CSV per sweep. Drives the 8-channel LED source for wavelength-resolved (action-spectrum) measurements. |
| `mfia-ledcal` | **Automated LED power calibration** — steps each LED channel, reads a Thorlabs PM16, writes the led_driver power calibration that `mfia-cf` consumes. |

Each tool runs fully against a **mock backend** (no hardware) for development
and dry-running an experiment plan.

## Repositories & dependencies

This project depends on a **second repo**, [`PXI-AWG`](https://github.com/Lukaspy/PXI-AWG),
which contains the `led_driver` package — the 8-channel LED source driven by an
NI PXI-7853R FPGA (it generates the analog modulation for a Mightex source with
dumb analog inputs). `mfia-cf` and `mfia-ledcal` import `led_driver`; `mfia-ct`
does not.

Instrument stack:
- **MFIA** — `zhinst` (LabOne Data Server running locally, default port 8004).
- **LED source** — `led_driver` (from PXI-AWG) + `nifpga` (real FPGA over PCIe).
- **PM16 power meter & 33250A** — `pyvisa` + `pyvisa-py` + `ThorlabsPM100`
  (PM16 over USBTMC; 33250A over GPIB needs NI-VISA + a GPIB adapter).

## Setup on a fresh machine

```bash
# 1. Clone both repos side by side
git clone git@github.com:<you>/MFIA-Ct.git
git clone git@github.com:Lukaspy/PXI-AWG.git

# 2. Make a venv
cd MFIA-Ct
python -m venv .venv
source .venv/bin/activate

# 3. Install the LED driver (editable) — provides `led_driver`.
#    Add [hardware] on the rig that has the PXI FPGA (pulls nifpga);
#    add [gui] only if you also want the standalone led_driver GUI.
pip install -e ../PXI-AWG                 # mock-capable, no FPGA needed
pip install -e "../PXI-AWG[hardware]"     # on the PCIe-connected rig

# 4. Install this package.
pip install -e ".[dev]"                   # mock only
pip install -e ".[dev,hardware]"          # MFIA + PM16 + 33250A drivers
```

> The editable install of PXI-AWG is what makes `led_driver` importable — there
> is no hidden path hack. If `led_driver` isn't importable, `mfia-cf`/`mfia-ledcal`
> surface a clear error telling you to `pip install -e ../PXI-AWG`.

### Real-hardware-only pieces (not on PyPI / not in the venv)

- **LabOne Data Server** for the MFIA (Zurich Instruments install).
- **NI-RIO driver** for the PXI-7853R, and **NI-VISA** if using the 33250A over GPIB.
- The FPGA bitfile (`.lvbitx`) lives in the PXI-AWG repo; point `mfia-ledcal` /
  `mfia-cf`'s LED-source field at it on the rig.

## Run

```bash
mfia-ct  --mock        # or --device dev32369
mfia-cf  --mock        # or --device dev32369
mfia-ledcal --mock     # or no flag for real LED + PM16
```

Instrument selection (mock vs real MFIA, device id, host, port) is also in the
GUI's Instrument panel, so a bare launch works too.

## Per-machine state (NOT in git)

- **led_driver config** `~/.config/led_driver/config.json` — channel→wavelength
  map and per-channel current limits. The code defaults are correct
  (Ch0=850 nm … Ch7=385 nm; 590 nm limited to 700 mA), so a fresh machine is
  safe out of the box. ⚠️ **Verify the physical wiring matches** (the LED in the
  channel the driver calls Ch0 should be the 850 nm one).
- **led_driver calibration** `~/.config/led_driver/calibration.json` — written by
  `mfia-ledcal`. **Calibration is physical**: regenerate it on the actual rig
  with the real fiber/geometry; don't copy it between machines.
- Measurement output (HDF5 / CSV) is gitignored — it lives outside the repo.

## Notes / gotchas baked into the tools

- **AC amplitude is V RMS** everywhere (matches the B1500 reference); the √2
  conversion to the MFIA's peak `output/amplitude` node happens once in the
  backend. Don't second-guess it.
- **Terminal mode**: 4-terminal caps DC bias at ±3 V; 2-terminal allows ±10 V.
  `mfia-cf` defaults to 2-terminal (the ±5 V bias matrix needs it); `mfia-ct`
  defaults 4-terminal. A guard warns if a bias exceeds the selected mode's limit.
  Match whatever terminal config your reference (B1500) data used.
- **Raw complex impedance** is always exported (Z, phase, G, B, Cp, Rp, Cs, Rs,
  Re, Im) — the equivalent-circuit dropdown is a display convenience and does
  not change the saved C-f data.
- `mfia-cf` stamps `optical_power_mw` per lit sweep from the LED calibration, so
  every CSV carries the power its photon-flux normalization needs.

## Project layout

```
src/mfia_ct/
  config.py          C-t experiment dataclasses (incl. TerminalMode).
  cf_config.py       C-f campaign config (sweep / bias / illumination).
  hardware.py        Real MFIA backend (zhinst): C-t streaming + C-f Sweeper.
  mock_hardware.py   Synthetic backend (C-t traces + Voigt-2-CPE C-f spectra).
  experiment.py      C-t orchestrator (continuous record + paced pulses).
  cf_experiment.py   C-f orchestrator (bias × illumination × sweep loops).
  fg33250a.py        Agilent 33250A function-generator driver (GPIB).
  led_source.py      LED source: PxiLedSource (wraps led_driver) + mock.
  pm16.py            Thorlabs PM16 power-meter abstraction + mock.
  led_calibration.py Automated LED power-calibration core.
  storage.py         C-t HDF5 save.
  cf_storage.py      C-f tidy-CSV-per-sweep writer.
  gui/instrument.py  Shared instrument-selection panel.
  gui/app.py         C-t GUI.
  gui/cf_app.py      C-f GUI.
  gui/cal_app.py     LED calibration GUI.
tests/               Mock-backed tests (pytest).
pm16_read.py         Standalone PM16 CLI streamer.
pm16_gui.py          Standalone PM16 readout GUI.
```

## Hardware references

MFIA user manual at `../documentation/ziMFIA_UserManual.pdf`:
§4.2 Advanced Impedance Measurements · §4.3 Compensation (run Open/Short before
a session) · §5.3 Impedance Analyzer Tab · §5.6 Sweeper Tab · §5.7 Data
Acquisition Tab · §5.10 Auxiliary Tab.
