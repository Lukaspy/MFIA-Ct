#!/bin/bash
# S1 session driver — three boustrophedon ladders with 45-min rests.
# Ladder order minimizes filter swaps (3 instead of 6):
#   L1: UNFILTERED half -> [swap: filter IN]  -> FILTERED half
#   L2: FILTERED half   -> [swap: filter OUT] -> UNFILTERED half
#   L3: UNFILTERED half -> [swap: filter IN]  -> FILTERED half
# At each [swap] the driver STOPS and waits; after swapping hardware run:
#   touch /tmp/mfia_swap_done
# Rests between ladders are automatic (45 min). Run from ~/Documents/MFIA-Ct:
#   nohup bash scripts/S1_driver.sh > ~/mfia_run_logs/S1_driver.log 2>&1 &
set -u
PY=.venv/bin/python
RUN="$PY scripts/run_plan_headless.py"
U=examples/plan_S1_0V_unfiltered.yaml
F=examples/plan_S1_0V_filtered.yaml
log(){ echo "[$(date +%H:%M:%S)] $*"; }
wait_swap(){
  rm -f /tmp/mfia_swap_done
  log "==== SWAP FILTER: $1 — then: touch /tmp/mfia_swap_done ===="
  until [ -f /tmp/mfia_swap_done ]; do sleep 5; done
  rm -f /tmp/mfia_swap_done; log "swap confirmed, continuing"
}
rest(){ log "45 min inter-ladder rest"; sleep 2700; }

log "S1 ladder 1/3: unfiltered half"
$RUN $U                      || { log "L1U FAILED"; exit 1; }
wait_swap "IN (C-side to MFIA)"
log "S1 ladder 1/3: filtered half"
$RUN $F --no-contact-check   || { log "L1F FAILED"; exit 1; }
rest
log "S1 ladder 2/3: filtered half (boustrophedon — no swap needed)"
$RUN $F --no-contact-check   || { log "L2F FAILED"; exit 1; }
wait_swap "OUT (direct/barrel)"
log "S1 ladder 2/3: unfiltered half"
$RUN $U                      || { log "L2U FAILED"; exit 1; }
rest
log "S1 ladder 3/3: unfiltered half (no swap needed)"
$RUN $U                      || { log "L3U FAILED"; exit 1; }
wait_swap "IN (C-side to MFIA)"
log "S1 ladder 3/3: filtered half"
$RUN $F --no-contact-check   || { log "L3F FAILED"; exit 1; }
log "S1 SESSION COMPLETE — 3 ladders, 3 swaps. De-embed filtered files, run seam QA."
