# Master Protocol v2 — plan for the five 2026-07-16 scope additions
Drafted overnight 2026-07-16 for morning review. Translates Lukas's requests:
(1) biased dark to 0.1 Hz, (2) lit-vs-bias "photogating" session, (3) AC drive-
amplitude sweeps, (4) fine 0–1 V bias points, (5) ×3 + error bars on low-f.
Feasibility measurements for (1) and (3) ran overnight on the rested p-Si —
results appended at the bottom.

---

## 1 · Biased dark extended to 0.1 Hz
**Design:** extend the manual filtered rung band downward: floor 0.1 Hz, 6 pts/dec
below 1 Hz (coarser than the 0 V dense grid — sub-Hz points cost 4–6 min each).
Single visit per rung per pass (visit-budget law), 240 s entry settle, 10 nA pin
territory (dynamic pinning handles).
**Cost:** ~30–45 min per rung per pass below 3.4 Hz.
**Proposed coverage (balances request 5):**
- 0 V + active-polarity ±1, ±2 (the transition-adjacent rungs): **×3 passes**
- ±4, ±7 both polarities: **×1**
- Adds ≈ 6–7 h per device.
**Risk:** sub-Hz stationarity at bias — the state must hold still for a 5-minute
point. Feasibility run answers this (below). Fallback if non-stationary: cap at
0.3 Hz for biased rungs (still 3× lower than current).

## 2 · Photogating vs bias (S5-photo) — the priority lit addition
**Design:** wideband (340 Hz–500 kHz, bypass, sweeper) + filtered spots
(34/107/340 Hz, manual) at each (rung × λ × intensity):
- Rungs: active polarity **+1, +2, +4** plus opposite-side **−2** as control
  (p-Si signs; mirror for others)
- λ: **470 / 625 / 850 nm** (span the response; 850 = the sub-gap question)
- Intensity: **3 % / 30 %** (dim = filter-compatible spots too; 30 % = wideband only)
- Dark pre/post brackets per (rung, λ); monotonic intensity; gentlest-first rung
  order; 15-min recovery between λ.
**Cost:** ≈ 3 h. Placement: after all dark sessions, before S3/S4 (dose ordering).
**This is the new dedicated session — S5-photo — runs even if S3/S4 get trimmed.**

## 3 · AC drive-amplitude sweeps (dark)
**Design:** new measurement type (`drive_sweep.py`, built + feasibility-tested):
at fixed (bias, f), step drive 30 → 283 mV RMS (7 log-ish steps), dynamic
multi-shift pinning, 15 s settle per amplitude, headroom rule uses per-amplitude
peak current.
- Biases: **0, +0.25, +0.5, +0.75, +1, +2, +4, +7** (active polarity — folds in
  request 4's transition region) **+ −2, −7** (opposite-side contrast)
- Frequencies: **100 Hz (filtered)** and **1 kHz (bypass)** per bias
- Order: LAST dark session of the day (large-amplitude drive = state workout;
  don't contaminate small-signal sessions after it).
**Cost:** ≈ 3–3.5 h. Filter burden guard: if |Z| collapses below ~100 kΩ at high
drive, the 100 Hz filtered leg switches to bypass automatically (flagged in data).
**Physics note:** 30 mV ≈ 1.6 kT/q → 283 mV ≈ 11 kT/q — deliberately spans
small→large signal; nonlinearity onset vs bias IS the measurement.

## 4 · Fine 0–1 V bias points
Two complementary implementations:
- **C-f rungs:** add **+0.25, +0.5, +0.75 V** (active polarity) to the S2 ladder —
  wideband + filtered band each, same single-visit rules. ≈ +45 min/pass.
- **C-V fine segment:** dedicated **0 → ±1 V at 50 mV steps** (21 pts/direction),
  100 Hz + 1 kHz, rested-0V-start halves. ≈ +25 min.
Density adjustable after first look at where the transition actually sits.

## 5 · ×3 passes with mean/error bars on low f
**Targeted ×3, not blanket:** the noise complaint lives in the manual filtered
bands, so repeats go where the bars matter:
- S1 low bands (0.1–340 Hz): **×3** (wideband stays ×1 — repeats at 0.05–2 %
  already; bars from method validation)
- S2 filtered rungs incl. sub-Hz extension: **×3 on 0/±1/±2**, ×1 elsewhere
- Drive sweeps / photogating / S3 / S4: ×1 (first-look sessions)
- 30-min spacing between repeat passes (revisit-budget compliant)
**Analysis:** per-point mean ± half-spread across passes → error-bar columns in
the compensated outputs (pipeline extension, ~1 h of code).

## p-Si-specific fix required (from tonight's C-V v3 verify)
p-Si low-bias is a ~33 MΩ near-resistor → Cp ill-conditioned exactly like n-Si's
conducting side, plus |Z| moves faster across bias than single-shift pinning
tracks (355 % headroom hit). Fixes going in: (a) multi-shift catch-up in
manual C-V (same 3-iteration loop as drive_sweep), (b) QC + analysis read Gp
where |phase| < 20° (report-G rule), (c) headroom screen per point in-run.

## Schedule impact (per device)
| Session bundle | v1 | v2 |
|---|---|---|
| S1 (×3 lows) + S2×2 polarities (+sub-Hz ext, fine rungs, ×3 low bands) | ~8 h | ~17 h |
| D5 C-V (+fine segment) + drive-amplitude session | ~3.5 h | ~7 h |
| S5-photo + S3 + S4 | ~9 h | ~12 h |
| **Total** | **~20 h** | **~36 h ≈ 2 nights + 1 day per device** |
Fleet: p-Si Thu–Fri, nitride Fri–Sat, optional n-Si v2 top-up (the new session
types only: sub-Hz biased, drive sweeps, S5-photo, fine rungs ≈ 14 h) weekend.

## Launched tonight without waiting (uncontroversial + wants the night window)
S1-dense on p-Si with ×3 low-band passes / ×1 wideband — request 5 applied to
the session that was already next in queue. Everything else awaits morning
sign-off of this document.
