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
- **Worked example:** [`examples/plan_blocks_A-E.yaml`](../examples/plan_blocks_A-E.yaml)
  — the Block A–E protocol, fully commented. Start there and edit.
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
| `amplitude_mv_rms` | `30` | AC test amplitude, **mV RMS**. |
| `terminal_mode` | `2-terminal` | `2-terminal` (±10 V bias) or `4-terminal` (±3 V). |
| `equiv_circuit` | `Cp\|\|Rp` | `Cp\|\|Rp` or `Cs-Rs`. |
| `device_settle_s` | `10` | **Short DC settle** after a light/bias step (the operating point; fast for OGT). Becomes the lit-step dwell and the C-f bias hold. |
| `dark_settle_s` | `10` | Dark-bracket dwell (drift/recovery check). |
| `settling_tcs` | `7` | **Per-point AC settling**, in demod time constants — instrument-set, unavoidable at low frequency. |
| `auto_bandwidth` | `true` | Let the MFIA pick the demod bandwidth per point. |
| `oversampling` | `1` | Demod samples averaged per point. `1` = none. Crank it (100s–1000s) to pull a few-pA signal out of the noise for high-Z / low-frequency sweeps — ZI's high-impedance recipe. Multiplies per-point time, so it's slow at low f. |
| `current_range_a` | `auto` | Fixed current-input range in **amps** (e.g. `1.0e-7` = 100 nA), or `auto`. Pin a sensitive range for high-Z / low-current sweeps where auto-range misbehaves at low f. A wide C-f spans many decades of impedance, so one fixed range can't fit it — pin it only on fixed-frequency C-V or narrow low-f-only blocks, and validate against a known reference. |
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
| `type` | **yes** | `c-f` or `c-v`. |
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
