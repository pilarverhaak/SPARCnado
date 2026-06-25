#!/usr/bin/env python3
"""
DAILY REPORT  (component 2)

Reads collection_history.csv (written by automated_health_pull.py) and produces
a morning monitoring report. Its job: surface, per participant, whether each of
the 8 ESSENTIAL metrics is flowing -- and flag, with a running consecutive
zero-day count, any essential metric that has dropped to zero (participant may
have stopped wearing the device -> staff decide whether to call).

Special rule: skin temperature (daily-sleep-temperature-derivations) needs ~3
nights of wear to establish a baseline, so it is reported as
"baseline establishing" for a participant's first 3 collecting days and only
flagged if still absent on day 4+.

Option A semantics: the report treats each PULL row as one observation in
sequence. In steady state (one daily pull) this is a clean per-day series, which
is what the dropped-off detection needs. The initial multi-day backfill row is
treated as "collection start / baseline present".

Usage:
  python3 daily_report.py            # writes today's report
  python3 daily_report.py --print    # also prints to stdout
"""

import os
import sys
import csv
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from automated_health_pull import (
    WORKING_DIR, HISTORY_FILE, ESSENTIAL_METRICS,
)

REPORT_DIR = os.path.join(WORKING_DIR, "daily_reports")
os.makedirs(REPORT_DIR, exist_ok=True)

SKIN_TEMP = "daily-sleep-temperature-derivations"
BASELINE_DAYS = 3   # skin temp not flagged during first 3 collecting days

# ============================================
# LOAD HISTORY
# ============================================

def load_history():
    """Return list of rows (dicts) sorted by pull_date then study_id."""
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE) as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: (r["pull_date"], r["study_id"]))
    return rows

# ============================================
# ANALYZE
# ============================================

def analyze(rows):
    """
    Build, per participant:
      - ordered list of distinct pull_dates (their observation sequence)
      - per metric: status on the most recent pull, and consecutive zero-streak
    Returns dict: study_id -> {pull_dates, metrics:{metric:{last_status,
                  zero_streak, last_count}}, n_obs}
    """
    # group rows: study_id -> pull_date -> metric -> (count, status)
    # If the same (study, pull_date, metric) appears more than once (e.g. a
    # backfill pull followed by an incremental re-run on the SAME calendar day),
    # keep the observation with the MOST points so a good pull is never silently
    # overwritten by a later empty one. 'ok' beats 'empty' beats 'error' on ties.
    _rank = {"ok": 2, "empty": 1, "error": 0}
    by_pid = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        pid, d, m = r["study_id"], r["pull_date"], r["metric"]
        cnt, st = int(r["point_count"]), r["status"]
        existing = by_pid[pid][d].get(m)
        if existing is None:
            by_pid[pid][d][m] = (cnt, st)
        else:
            ex_cnt, ex_st = existing
            # prefer more points; break ties by status rank
            if (cnt, _rank.get(st, 0)) >= (ex_cnt, _rank.get(ex_st, 0)):
                by_pid[pid][d][m] = (cnt, st)

    result = {}
    for pid, by_date in by_pid.items():
        pull_dates = sorted(by_date.keys())
        n_obs = len(pull_dates)
        metrics = {}
        # union of all metrics seen for this participant
        all_metrics = set()
        for d in pull_dates:
            all_metrics.update(by_date[d].keys())

        for m in all_metrics:
            # walk pulls oldest->newest, track latest status & trailing zero streak
            last_count, last_status = 0, "missing"
            zero_streak = 0
            for d in pull_dates:
                if m in by_date[d]:
                    cnt, st = by_date[d][m]
                    last_count, last_status = cnt, st
                    if st == "error":
                        # errors don't count as zeros (different problem); reset
                        # the streak so we don't conflate "broke" with "not worn"
                        zero_streak = 0
                    elif cnt == 0:
                        zero_streak += 1
                    else:
                        zero_streak = 0
            metrics[m] = {
                "last_status": last_status,
                "last_count": last_count,
                "zero_streak": zero_streak,
            }
        result[pid] = {"pull_dates": pull_dates, "n_obs": n_obs, "metrics": metrics}
    return result

# ============================================
# RENDER
# ============================================

def render(analysis):
    today = datetime.now().strftime("%Y-%m-%d")
    L = []
    L.append("=" * 64)
    L.append(f"  DAILY COLLECTION REPORT  -  {today}")
    L.append("=" * 64)

    if not analysis:
        L.append("\nNo collection history yet (no participants pulled).")
        return "\n".join(L)

    # ---- summary line ----
    n = len(analysis)
    flagged = []
    for pid, a in analysis.items():
        if any(_is_flagged(pid, m, a) for m in ESSENTIAL_METRICS if m in a["metrics"]):
            flagged.append(pid)
    L.append(f"\nParticipants collecting: {n}")
    L.append(f"Participants with essential-metric flags: {len(flagged)}"
             + (f"  -> {', '.join(flagged)}" if flagged else ""))

    # ---- per-participant detail ----
    for pid in sorted(analysis.keys()):
        a = analysis[pid]
        L.append("\n" + "-" * 64)
        L.append(f"{pid}   ({a['n_obs']} pull(s); latest {a['pull_dates'][-1]})")
        L.append("-" * 64)

        # ESSENTIAL metrics block
        L.append("  ESSENTIAL METRICS:")
        for m in ESSENTIAL_METRICS:
            if m not in a["metrics"]:
                L.append(f"    {m:38s} (never pulled)")
                continue
            md = a["metrics"][m]
            label = _status_label(pid, m, a, md)
            L.append(f"    {m:38s} {label}")

        # OTHER metrics block (pulled + counted, not flagged, for context)
        others = sorted(set(a["metrics"].keys()) - set(ESSENTIAL_METRICS))
        if others:
            L.append("  OTHER METRICS:")
            for m in others:
                md = a["metrics"][m]
                cnt = md["last_count"]
                st = md["last_status"]
                note = f"{cnt} pts" if st != "error" else "ERROR"
                if st != "error" and cnt == 0 and md["zero_streak"] > 0:
                    note += f"  (zero x{md['zero_streak']})"
                L.append(f"    {m:38s} {note}")

    L.append("\n" + "=" * 64)
    L.append("Flag legend: 'ZERO xN' = metric empty for N consecutive pulls "
             "(possible non-wear). 'ERROR' = pull failed (technical, not non-wear).")
    L.append("=" * 64)
    return "\n".join(L)

def _is_flagged(pid, metric, a):
    """An essential metric is flagged if it has an active zero-streak (with the
    skin-temp baseline exception)."""
    if metric not in a["metrics"]:
        return False
    md = a["metrics"][metric]
    if md["last_status"] == "error":
        return True  # technical failure is worth surfacing too
    if md["zero_streak"] <= 0:
        return False
    if metric == SKIN_TEMP and a["n_obs"] <= BASELINE_DAYS:
        return False  # still establishing baseline
    return True

def _status_label(pid, m, a, md):
    if md["last_status"] == "error":
        return "[!] ERROR on last pull (technical failure -- not non-wear)"
    cnt = md["last_count"]
    streak = md["zero_streak"]
    if m == SKIN_TEMP and a["n_obs"] <= BASELINE_DAYS and cnt == 0:
        return f"baseline establishing (day {a['n_obs']} of {BASELINE_DAYS})"
    if cnt > 0:
        return f"OK  ({cnt} pts)"
    # zero
    if streak > 0:
        return f"[FLAG] ZERO x{streak}  <-- possible non-wear, review"
    return "0 pts"

# ============================================
# MAIN
# ============================================

def main():
    rows = load_history()
    analysis = analyze(rows)
    report = render(analysis)

    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(REPORT_DIR, f"{today}_report.txt")
    with open(path, "w") as f:
        f.write(report + "\n")

    if "--print" in sys.argv:
        print(report)
    print(f"\nReport saved: {path}")

if __name__ == "__main__":
    main()
