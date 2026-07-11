#!/bin/bash
# Fully-automated S1: three boustrophedon ladders, reed switcher does the swaps.
# Prereq: switcher installed+accepted, 24 V rail on, plans carry filter_state.
#   nohup bash scripts/S1_driver_auto.sh > ~/mfia_run_logs/S1_auto.log 2>&1 &
set -u
RUN=".venv/bin/python scripts/run_plan_headless.py"
U=examples/2013-3_n-Si/publication/plan_S1_0V_unfiltered.yaml
F=examples/2013-3_n-Si/publication/plan_S1_0V_filtered.yaml
log(){ echo "[$(date +%H:%M:%S)] $*"; }
rest(){ log "45 min inter-ladder rest"; sleep 2700; }
log "S1 AUTO ladder 1/3"; $RUN $U --no-contact-check || exit 1; $RUN $F --no-contact-check || exit 1; rest
log "S1 AUTO ladder 2/3"; $RUN $F --no-contact-check || exit 1; $RUN $U --no-contact-check || exit 1; rest
log "S1 AUTO ladder 3/3"; $RUN $U --no-contact-check || exit 1; $RUN $F --no-contact-check || exit 1
log "S1 COMPLETE - 3 ladders, 0 human swaps"
