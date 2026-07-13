#!/bin/bash
# 2026-07-13 night: S2b-v3 (x3 passes) then S2a-v3 F-retake (x3), bias-major,
# settle-gated between sessions. Keeps Monday clear for the p-Si landing.
set -u
cd ~/Documents/MFIA-Ct
RUN=".venv/bin/python scripts/run_plan_headless.py"
log(){ echo "[$(date "+%m-%d %H:%M:%S")] $*"; }
gate(){ for t in 1 2 3 4; do
    log "settle gate attempt $t"
    .venv/bin/python /tmp/settle_gate.py && return 0
    sleep 1800
  done; log "gate gave up"; return 1; }
session(){ # $1=plan $2=tag
  for p in 1 2 3; do
    log "$2 pass $p/3"
    $RUN "$1" --no-contact-check || return 1
    [ $p -lt 3 ] && { log "45 min rest"; sleep 2700; }
  done
  .venv/bin/python /tmp/park_0V.py
}
log "resting 60 min before S2b-v3"
sleep 3600
gate || exit 2
session examples/2013-3_n-Si/publication/plan_S2b_v3.yaml "S2b-v3" || exit 1
log "S2b-v3 done; resting 90 min"
sleep 5400
gate || exit 2
session examples/2013-3_n-Si/publication/plan_S2a_v3.yaml "S2a-v3" || exit 1
log "NIGHT COMPLETE: S2b-v3 + S2a-v3"
