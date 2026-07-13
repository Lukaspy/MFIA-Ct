#!/bin/bash
# S2a positive-bias session, x3 passes for error bars (Lukas 2026-07-12:
# keep multi-pass banding despite time cost). Switcher handles all filter
# transitions (state-cache + make-before-break). ~6.5 h total.
set -u
RUN=".venv/bin/python scripts/run_plan_headless.py"
U=examples/2013-3_n-Si/publication/plan_S2a_pos_unfiltered.yaml
F=examples/2013-3_n-Si/publication/plan_S2a_pos_filtered.yaml
log(){ echo "[$(date +%H:%M:%S)] $*"; }
for p in 1 2 3; do
  log "S2a pass $p/3: unfiltered half"
  $RUN $U --no-contact-check || exit 1
  log "S2a pass $p/3: filtered half"
  $RUN $F --no-contact-check || exit 1
  [ $p -lt 3 ] && { log "45 min rest"; sleep 2700; }
done
log "S2a COMPLETE - 3 passes"
