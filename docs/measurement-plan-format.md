# mfia-cf measurement-plan format

A **plan** is one YAML file that defines a complete, multi-block measurement
campaign for `mfia-cf` — the device, shared instrument/timing defaults, and an
ordered list of **blocks** (each a C-f or C-V campaign). Loading a plan stages
every block into the overnight queue, in order; then you press **Run queue**.

A plan is self-contained and reproducible: the device id, output folder, every
axis, illumination sequence, and timing live in the file, so the same plan run
on two devices gives directly comparable data and the file *is* the record of
what was measured.

- **Run it:** in `mfia-cf`, connect the instrument → **Load plan…** → review the
  staged queue → **Run queue**. (You can also stage configs by hand and mix
  them with a loaded plan.)
- **Worked example:** [`examples/plan_2013-3_n-Si.yaml`](../examples/plan_2013-3_n-Si.yaml)
  — the current canonical protocol (spectral C-f at every bias + full C-V),
  commented with the hardware rationale. Start there and edit.
  ([`plan_blocks_A-E.yaml`](../examples/plan_blocks_A-E.yaml) is the older,
  simpler A–E walkthrough.)
- **Loader:** `mfia_ct.cf_plan.load_plan(path) -> list[CfConfig]` (pure, testable).

---

## Top-level structure

```yaml
device:      {id, substrate, notes}     # required: device.id
output_dir:  "<folder>"                 # required: where CSVs are written
defaults:    { ... }                    # optional: shared settings (below)
blocks:      [ { ... }, ... ]           # required: ≥1 block, run in order
```

| Key | Required | Meaning |
|-----|----------|---------|
| `device.id` | **yes** | Device identifier; appears in every output filename. |
| `device.substrate` | no | e.g. `n-Si`, `p-Si`. Recorded in the CSV header. |
| `device.notes` | no | Free text, recorded in the CSV header. |
| `output_dir` | **yes** | Destination folder for all CSVs (created if missing). C-f and C-V filenames don't collide, so one folder is fine. |

---

## `defaults` — shared, per-block overridable

Every key is optional; the table shows the built-in default. Any block may
override any of these inline (see *Blocks*).

| Key | Default | Meaning |
|-----|---------|---------|
| `amplitude_mv_rms` | `30` | **Dark** AC test amplitude, **mV RMS**. The `30` default is too small for the dark high-Z, low-f tail (noisy) — use **~100** for depletion-cap work. |
| `light_amplitude_mv_rms` | = `amplitude_mv_rms` | **Lit-step** AC amplitude, **mV RMS**. Under light the junction drops to low impedance, so step the drive **down (~40)** to stay small-signal across the curved C-V. Applied automatically per step (dark vs lit); unset → same as dark. |
| `compensation_enabled` | `true` | Apply the MFIA's stored Open/Short/Load compensation. Leave `true`; the compensation itself is built **manually in LabOne first** (see *Notes & limits*). |
| `terminal_mode` | `2-terminal` | `2-terminal` (±10 V bias) or `4-terminal` (±3 V). |
| `equiv_circuit` | `Cp\|\|Rp` | `Cp\|\|Rp` or `Cs-Rs`. |
| `device_settle_s` | `10` | **Short DC settle** after a light/bias step (the operating point; fast for OGT). Becomes the lit-step dwell and the C-f bias hold. |
| `dark_settle_s` | `10` | Dark-bracket dwell (drift/recovery check). |
| `settling_tcs` | `7` | **Per-point AC settling**, in demod time constants — instrument-set, unavoidable at low frequency. |
| `auto_bandwidth` | `true` | Let the MFIA pick the demod bandwidth per point. |
| `oversampling` | `1` | Demod samples averaged per point. `1` = none (the norm — auto range reads the high-Z low-f tail cleanly when wired right). A rarely-needed fallback for low-f SNR at small amplitude; multiplies per-point time, so it's slow at low f. |
| `current_range_a` | `auto` | Fixed current-input range in **amps** (e.g. `1.0e-7` = 100 nA), or `auto`. **`auto` for any wide (multi-decade) C-f** — a pinned range overloads at high f (the cap draws µA). **But MFIA auto-range only ratchets *up* and skips the most sensitive ranges**, so on a **narrow sub-kHz block** the high-Z low-f current is too small for it to find — there you **must pin** a sensitive range (e.g. `1.0e-8` = 10 nA) and validate against a reference. |
| `freq.start_hz` | `1` | C-f sweep low-frequency floor (Hz). |
| `freq.stop_hz` | `300000` | C-f sweep ceiling (Hz). |
| `freq.points_per_decade` | `10` | C-f log-sweep density. |
| `freq.log_spacing` | `true` | Log vs linear frequency spacing. |

### Settle timing — the three distinct waits

These map to three separate fields on purpose; sizing them is the difference
between a fast night and a wasted one:

1. **Device settle** (`device_settle_s`, short ~5–15 s) — waiting for the
   operating point after a light/bias step. OGT switching is sub-15 µs, so this
   is short.
2. **Per-point AC settling** (`settling_tcs`, ~5–10) — a 1 Hz point still needs
   several AC cycles (seconds); this is why low-frequency sweeps are slow
   regardless of device speed. Leave `auto_bandwidth: true`.
3. **Slow-trap tail** — the ~0.01 Hz arc implies a trap with a tens-of-seconds
   constant. Verify once with the live low-frequency readout: if the lowest-f
   reading still drifts after your nominal settle, bump `device_settle_s` to
   ~20–30 s **on the deep-anchor block only** (per-block override).

Dark bracketing inherits `dark_settle_s` (brief, ~10 s — a baseline check, not a
long wait).

---

## Blocks

Each block is one campaign and becomes one queued entry. Common keys:

| Key | Required | Meaning |
|-----|----------|---------|
| `name` | recommended | Label shown in the queue and in error messages. |
| `type` | **yes** | `c-f`, `c-v`, or `i-v` (`i-v` is **B1500-only** — the MFIA has no DC SMU). |
| `illumination` | **yes** | Illumination spec (below). |
| *overrides* | no | Any `defaults` key (incl. a nested `freq:`) set here applies to this block only — e.g. a longer `device_settle_s` or a lower `freq.start_hz`. |

### C-f block

```yaml
- name: "..."
  type: c-f
  bias: [-4, -2, 0, 1, 2, 4]    # list of DC bias points (V); the outer loop
  illumination: { ... }          # runs at each bias
```

`bias` is a list — one value for an anchor-bias block, several for a bias ladder.
The frequency sweep comes from `defaults.freq` (override with a block-level
`freq:`). Loop nesting: **bias → illumination → frequency sweep**.

### C-V block

```yaml
- name: "..."
  type: c-v
  frequencies: [1000, 100, 10, 1]          # held test frequencies (Hz); outer loop
  bias: {start_v: -4, stop_v: 4, points: 101}   # the swept axis
  illumination: { ... }
```

`frequencies` is the list of fixed AC test frequencies; the bias is swept at each.
List them **high→low** so the fast, reliable data lands first. Loop nesting:
**frequency → illumination → bias sweep**. (Total bias sweeps = frequencies ×
illumination steps — multi-frequency C-V with a big matrix gets large.)

### I-V block (B1500 only)

```yaml
- name: "..."
  type: i-v
  bias: {start_v: -2, stop_v: 2, points: 101}   # the swept SMU voltage axis
  i_compliance_a: 1.0e-3        # current compliance (A); default 0.01
  double_sweep: true            # forward then reverse (hysteresis); default false
  measure_range_a: auto         # fixed current-measure range (A) or auto; default auto
  hold_s: 0.0                   # settle before the first step
  delay_s: 0.0                  # per-step measurement delay
  illumination: { ... }         # one I-V curve per illumination step
```

A DC **I-V curve** measured by the B1500 SMU (the MFIA can't do this — a plan
with an `i-v` block only runs on the B1500). There is no test frequency and no
AC amplitude; the SMU sweeps the device voltage `start_v → stop_v` and records
current, limited by `i_compliance_a`. Loop nesting is just **illumination → the
SMU staircase**, so the dark brackets give the DC dark-current baseline and each
lit step a photo-I-V — a current-domain action spectrum, the DC analog of the
C-f/C-V one. `double_sweep: true` returns `2 × points` (forward then reverse).

---

## `illumination` — three modes

#### `dark_only`
```yaml
illumination: {dark_only: true}
```
A single dark sweep (no LED). Used for QA / baseline / dark references.

#### `mode: fixed` — an explicit step list, run verbatim
```yaml
illumination:
  mode: fixed
  steps:
    - dark                          # LED off
    - {wl: 625, intensity: 100}     # 625 nm at 100 %
    - {wl: 470, intensity: 30, settle_s: 15}   # optional per-step settle
```
Use for "one bright reference vs dark" blocks (the bias ladder). Each `dark`
step uses `dark_settle_s`; each lit step uses `device_settle_s` unless it sets
its own `settle_s`.

#### `mode: matrix` — wavelengths × intensities
```yaml
illumination:
  mode: matrix
  wavelengths: [385, 470, 505, 530, 590, 625, 740, 850]
  intensities: [1, 3, 10, 30, 100]   # default set, applied to every wavelength
  order: randomized                  # as-listed | reversed | randomized
  seed: 1                            # for randomized — makes the order reproducible
  dark_bracket: per-wavelength       # per-wavelength | interleaved | ends | none
```

- **`wavelengths`** — bare numbers use the block `intensities`. To vary the set
  per wavelength (e.g. anchors get the full ramp, others get only 100 %), use
  mappings:
  ```yaml
  wavelengths:
    - {nm: 625, intensities: [1, 3, 10, 30, 100]}   # anchor: full set
    - {nm: 850, intensities: [100]}                 # other: 100 % only
  ```
- **`order`** — `as-listed` (default), `reversed`, or `randomized`. Randomized
  uses `seed` so the realized order is fixed once loaded and is reproducible.
- **`dark_bracket`** — where dark sweeps go:
  - `per-wavelength` (default): a dark sweep **before and after each
    wavelength's intensity ramp** (recovery/drift check). This is the Block-B/E
    pattern.
  - `interleaved`: a dark sweep **after every lit step**.
  - `ends`: one dark at the very start and one at the very end.
  - `none`: no dark sweeps.
- **`light_settle_s` / `dark_settle_s`** — optional per-block overrides of the
  lit and dark dwells (else inherited from `defaults`).

---

## Notes & limits

- **Compensation is manual.** The plan can't drive LabOne open/short/load
  compensation — do it before loading. The QA dark sweep / dark baseline *are*
  run by the plan (as `dark_only` blocks).
- **Filenames** are the standard `MFIA_Cf_*` / `MFIA_CV_*` pattern, written to
  `output_dir`; the illumination label (e.g. `625nm_100pct`, `dark_pre_625`) is
  in each filename so the matrix/bracket structure is recoverable from the files.
- **Each block is an independent campaign** in the queue — if one fails it's
  skipped and the rest continue (overnight-safe).
- **Per block = one CfConfig.** A C-V block with a frequency list is still one
  block; the frequency sweep happens inside it.
- **Validation** names the offending block and field, e.g.
  `block 3 (C: bias ladder @ 625nm): a c-f block needs a non-empty 'bias' list`.

---

## Hardware setup the plan can't encode (do these first)

The plan is pure software; these bench/instrument conditions must be right or a
syntactically-valid plan still records garbage:

- **Data-server host.** The MFIA runs its **own embedded LabOne Data Server** —
  connect to `mf-<serial>:8004` (here `mf-dev32369`), **not** `localhost`,
  unless you deliberately run a Data Server on the PC. The headless runner and
  the GUI Instrument panel both take this host.
- **Wiring: DUT drive on HCUR, not HPOT.** The current-drive terminal is
  Hcur/Lcur; HPOT/LPOT are the *sense* pair. A DUT lead on HPOT reads **open**,
  not a device (this cost a day once).
- **Compensation first.** Build Open/Short (plus a load standard if you have one)
  in LabOne over the band you'll sweep (≈80 Hz–510 kHz) **before** loading the
  plan; `compensation_enabled` only toggles *applying* the stored compensation.
- **Biased sub-80 Hz is a hard wall.** At reverse bias the MFIA's single
  transimpedance input shares range between DC leakage and the AC signal, so
  below ~80 Hz the leakage swamps the measurement — that range is the B1500's
  job, not the MFIA's. The sub-kHz tail block is therefore **0 V only**.
- **Transients aren't a plan block.** Cp(t) through light on/off (capture cross-
  section, recovery τ) is the separate **`mfia-ct`** tool, not a `c-f`/`c-v` block.

### Choosing the analyzer: MFIA vs B1500

A plan is **instrument-agnostic** — the same file runs on either analyzer; you
pick one **per session** (the GUI's *Backend* dropdown, or the headless runner's
`--instrument`). The blocks stay the same; the backend interprets them.

- **Headless:** `--instrument mfia` (default) or
  `--instrument b1500 --resource GPIB0::17::INSTR`.
- **GUI:** *Backend* → *Real B1500*, enter the VISA resource, Connect.

B1500-specific facts the plan can't encode:

- **Transport is GPIB (address 17) or USB — not LAN.** The B1500's FLEX command
  set has no native socket listener; "network" control means a GPIB↔LAN gateway
  or IO-Libraries remote-GPIB, which is still just a different VISA resource
  string (e.g. `TCPIP0::<gateway>::gpib0,17::INSTR`).
- **MFCMU range:** frequency **1 kHz–5 MHz** (so a B1500 C-f plan sets
  `freq.start_hz: 1000`), AC level is **V RMS directly** (no √2) and caps at
  **250 mV** — the driver clamps to both.
- **Build the CMU correction (open/short/load) in EasyEXPERT first**, like MFIA
  compensation in LabOne; the plan only sequences the sweeps.
- **`i-v` blocks need an SMU** — they error on the MFIA.
- **Data is filed with an instrument prefix** (`MFIA_*` vs `B1500_*`) and the CSV
  header records `# instrument:`, so a device's MFIA and B1500 data don't collide
  and merge cleanly (the reason to run both: B1500 covers 500 kHz–5 MHz the MFIA
  can't). Worked example: [`examples/plan_b1500_2013-3_n-Si.yaml`](../examples/plan_b1500_2013-3_n-Si.yaml).

**Run headless** (no GUI), same plan, overnight-safe (one block failing doesn't
abort the rest), with a contact pre-check up front:

```bash
python scripts/run_plan_headless.py examples/plan_2013-3_n-Si.yaml
```
