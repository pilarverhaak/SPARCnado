#!/bin/bash
# ============================================================
# run_daily.sh  -  single daily entrypoint for cron
# Runs the health pull, then generates the daily report.
# One job, sequential, with a combined log.
# ============================================================

PYTHON="/usr/bin/python3"

DESKTOP="/Users/Perlislab/Desktop"
LOGDIR="/Users/Perlislab/Desktop/Sparkle/Fitbit/automation_logs"
STAMP="$(date +%Y%m%d_%H%M%S)"
CRONLOG="$LOGDIR/cron_${STAMP}.log"

mkdir -p "$LOGDIR"
cd "$DESKTOP" || { echo "cannot cd to $DESKTOP" >> "$CRONLOG"; exit 1; }

echo "=== run_daily start $(date) ===" >> "$CRONLOG"

# 1) pull all collecting participants
"$PYTHON" "$DESKTOP/automated_health_pull.py" >> "$CRONLOG" 2>&1
PULL_RC=$?
echo "pull exit code: $PULL_RC" >> "$CRONLOG"

# 2) generate the daily report (runs even if pull had per-participant errors)
"$PYTHON" "$DESKTOP/daily_report.py" >> "$CRONLOG" 2>&1
REPORT_RC=$?
echo "report exit code: $REPORT_RC" >> "$CRONLOG"

echo "=== run_daily end $(date) ===" >> "$CRONLOG"
exit 0
