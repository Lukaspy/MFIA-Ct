#!/bin/bash
# D1-v3: single-session, single-state definitive 0 V spectrum, x3 passes.
# Per pass: manual lows (dynamic pins) -> sweeper F 34-340 -> wideband U (bypass).
# ~3.2 h total. Run on a rested device (settle-verify first!).
set -u
RUN=".venv/bin/python scripts/run_plan_headless.py"
F=examples/2013-3_n-Si/publication/plan_S1v2_F340.yaml
U=examples/2013-3_n-Si/publication/plan_S1_0V_unfiltered.yaml
log(){ echo "[$(date +%H:%M:%S)] $*"; }
for p in 1 2 3; do
  log "D1v3 pass $p/3: manual lows"
  .venv/bin/python /tmp/manual_lowf_v3.py "V2P$p" || exit 1
  log "D1v3 pass $p/3: sweeper 34-340"
  $RUN $F --no-contact-check || exit 1
  log "D1v3 pass $p/3: wideband"
  $RUN $U --no-contact-check || exit 1
  [ $p -lt 3 ] && { log "45 min rest"; sleep 2700; }
done
log "D1v3 COMPLETE"
