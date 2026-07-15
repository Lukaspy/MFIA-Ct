#!/bin/bash
# ============================================================================
# MASTER PROTOCOL — one button per device. All components individually
# verified 2026-07-14 (see lab notebook: method map, settle laws, screens).
#   usage: master_protocol.sh <device_plans_dir> <out_root> [n_passes]
#   e.g.:  master_protocol.sh examples/2013-3_n-Si/publication \
#            ~/Documents/capacitance_measurements 3
# Sequence: [operator: land + scout + comp already done] ->
#   S1-dense xN -> S2a (U-sweeper + F-manual) xN -> S2b xN ->
#   D5 C-V (1k anchor + 100/10/1 Hz manual + lit 1k) -> S3 action (overnight
#   slot) -> S4 intensity (LAST). Settle-gate before every session; park after.
# ============================================================================
set -u
PLANS=$1; OUTROOT=$2; NPASS=${3:-3}
cd ~/Documents/MFIA-Ct
RUN=".venv/bin/python scripts/run_plan_headless.py"
PY=".venv/bin/python"
log(){ echo "[$(date "+%m-%d %H:%M:%S")] MASTER: $*"; }
gate(){ for t in 1 2 3 4; do log "settle gate ($1) attempt $t"
    $PY /tmp/settle_gate.py && return 0; sleep 1800; done
  log "GATE GAVE UP before $1 — halting for human"; exit 2; }
park(){ $PY /tmp/park_0V.py; }
rest(){ log "rest $1 min"; sleep $(($1*60)); }

DEV=$(basename $(dirname $PLANS))   # e.g. 2013-3_n-Si
export MFIA_DEVICE_ID=${DEV%%_*}
export MFIA_SUBSTRATE=${DEV#*_}
export MFIA_S1_OUT=$OUTROOT/${MFIA_DEVICE_ID}_S1_0V
export MFIA_S3_BIAS=${MFIA_S3_BIAS:--2.0}   # override per device (photo-active sign!)
ARC_SIDE=${MFIA_ARC_SIDE:-neg}   # which polarity gets the 3.4 Hz arc-closure floor
[ "$ARC_SIDE" = "pos" ] && { FLOOR_POS=3.4; FLOOR_NEG=34; } || { FLOOR_POS=34; FLOOR_NEG=3.4; }
mkdir -p $MFIA_S1_OUT
log "=== MASTER PROTOCOL START: $DEV, ${NPASS}x passes ==="

# --- S1: definitive 0 V dark (dense manual lows 0.1-340 + wideband) ---
gate S1
for p in $(seq 1 $NPASS); do
  log "S1 pass $p/$NPASS: manual filtered 0.1-340 Hz"
  $PY /tmp/manual_lowf_v4.py "S1P$p" || exit 1
  log "S1 pass $p/$NPASS: wideband"
  $RUN $PLANS/plan_S1_0V_unfiltered.yaml --no-contact-check || exit 1
  [ $p -lt $NPASS ] && rest 30
done
park; rest 20

# --- S2a: positive rungs ---
gate S2a
for p in $(seq 1 $NPASS); do
  log "S2a pass $p/$NPASS: wideband rungs"
  $RUN $PLANS/plan_S2a_U.yaml --no-contact-check || exit 1
  log "S2a pass $p/$NPASS: manual filtered rungs"
  $PY /tmp/manual_rungs.py $OUTROOT/${MFIA_DEVICE_ID}_S2a_pos "1,2,4,7" "S2aP$p" $FLOOR_POS || exit 1
  [ $p -lt $NPASS ] && rest 30
done
park; rest 20

# --- S2b: negative rungs (filtered band extends to 3.4 Hz: arc closure) ---
gate S2b
for p in $(seq 1 $NPASS); do
  log "S2b pass $p/$NPASS: wideband rungs"
  $RUN $PLANS/plan_S2b_U.yaml --no-contact-check || exit 1
  log "S2b pass $p/$NPASS: manual filtered rungs (3.4-340)"
  $PY /tmp/manual_rungs.py $OUTROOT/${MFIA_DEVICE_ID}_S2b_neg "-1,-2,-4,-7" "S2bP$p" $FLOOR_NEG || exit 1
  [ $p -lt $NPASS ] && rest 30
done
park; rest 20

# --- D5: C-V suite ---
gate D5
log "D5: 1 kHz anchor C-V (dark, both directions)"
$RUN $PLANS/plan_D5_anchor1k.yaml --no-contact-check || exit 1
for f in 100 10 1; do
  n=71
  log "D5: manual C-V $f Hz ($n pts/direction)"
  $PY /tmp/manual_cv_v2.py $f $OUTROOT/${MFIA_DEVICE_ID}_D5_cv $n 7.0 "CV${f}Hz" || exit 1
done
log "D5: lit 1 kHz C-V (dim matrix)"
$RUN $PLANS/plan_D5_lit1k.yaml --no-contact-check || exit 1
park; rest 30

# --- S3: action spectrum (long; schedule so this lands overnight) ---
gate S3
log "S3: wideband spectral matrix (runner)"
$RUN $PLANS/plan_S3_wideband.yaml --no-contact-check || exit 1
log "S3: filtered lambda spots (manual)"
$PY /tmp/manual_lit_spots.py $OUTROOT/${MFIA_DEVICE_ID}_S3_action || exit 1
park; rest 30

# --- S4: intensity series — ALWAYS LAST ---
gate S4
log "S4: intensity ladders"
$RUN $PLANS/plan_S4_intensity.yaml --no-contact-check || exit 1
park
log "=== MASTER PROTOCOL COMPLETE: $DEV ==="
log "operator next: comp standards (if not done), unmount / next device"
