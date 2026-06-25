#!/usr/bin/env python3
"""
AUTOMATED HEALTH PULL  (daily cron orchestrator)

Once per day, for every participant who is actively collecting on the Google
Health API, this:
  1. refreshes their access token (on-demand, via load_credentials)
  2. pulls the prior day's data (or, on first pull, up to 30 days back)
  3. saves raw/flat JSON via the tested retrieval functions
  4. records a PER-METRIC point count into collection_history.csv so the daily
     report can detect when a metric drops to zero (participant stopped wearing)

Participant selection:
  A participant is "collecting" (and thus pulled) iff a Google credential
  pickle  token_<study_id>.pickle  exists. Staff run the auth script daily until
  a participant pairs; once the pickle appears, they're automatically pulled.
  Rows still on api_version=fitbit, or without a pickle, are skipped.

Usage:
  python3 automated_health_pull.py            # all collecting participants
  python3 automated_health_pull.py P064       # single participant (testing)
"""

import os
import sys
import csv
import glob
import json
import traceback
from datetime import datetime, timedelta, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google_health_data_retrieval import (
    WORKING_DIR, DATA_TYPES,
    get_service, list_all_points, save_json,
    flatten_points, flatten_sleep, flatten_exercise, flatten_activity_level,
)
from google_health_authorization import load_credentials

# ============================================
# CONFIG
# ============================================

DEMO_ACCOUNTS = os.path.join(WORKING_DIR, "demo_accounts.csv")
HISTORY_FILE  = os.path.join(WORKING_DIR, "collection_history.csv")
LOG_DIR       = os.path.join(WORKING_DIR, "automation_logs")
FIRST_PULL_MAX_DAYS = 30   # safety cap on first-pull backfill

# The 8 essential metrics the daily report flags on (per-metric zero detection).
# (Other metrics are still pulled + counted, but the report highlights these.)
ESSENTIAL_METRICS = [
    "steps",
    "heart-rate",
    "heart-rate-variability",
    "oxygen-saturation",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
    "daily-sleep-temperature-derivations",
    "sleep",
]

os.makedirs(LOG_DIR, exist_ok=True)

# ============================================
# LOGGING
# ============================================

_log_lines = []
def log(msg, level="INFO"):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line)
    _log_lines.append(line)

def flush_log():
    path = os.path.join(LOG_DIR, f"pull_{datetime.now():%Y%m%d_%H%M%S}.log")
    with open(path, "w") as f:
        f.write("\n".join(_log_lines) + "\n")
    return path

# ============================================
# PARTICIPANT SELECTION
# ============================================

def collecting_participants(filter_id=None):
    """
    Return study_ids that have a Google credential pickle (=> collecting).
    Optionally restrict to a single id for testing.
    """
    ids = []
    for pk in sorted(glob.glob(os.path.join(WORKING_DIR, "token_*.pickle"))):
        sid = os.path.basename(pk)[len("token_"):-len(".pickle")]
        ids.append(sid)
    if filter_id:
        ids = [s for s in ids if s == filter_id]
    return ids

# ============================================
# HISTORY STORE  (per participant, per day, per metric counts)
# ============================================

HISTORY_FIELDS = ["pull_date", "study_id", "data_date", "metric", "point_count", "status"]
# status: ok | empty | error

def append_history(rows):
    exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)

def participant_has_history(study_id):
    if not os.path.exists(HISTORY_FILE):
        return False
    with open(HISTORY_FILE) as f:
        for row in csv.DictReader(f):
            if row["study_id"] == study_id:
                return True
    return False

# ============================================
# CORE PULL FOR ONE PARTICIPANT
# ============================================

def pull_participant(study_id):
    """Pull one participant. Returns list of history rows (one per metric)."""
    pull_date = date.today().isoformat()
    log(f"--- {study_id}: starting pull ---")

    # window: first pull = up to 30d back; otherwise just yesterday
    if participant_has_history(study_id):
        start = (date.today() - timedelta(days=1)).isoformat()
        end   = date.today().isoformat()
        log(f"{study_id}: incremental pull {start} (prior day)")
    else:
        start = (date.today() - timedelta(days=FIRST_PULL_MAX_DAYS)).isoformat()
        end   = date.today().isoformat()
        log(f"{study_id}: FIRST pull, backfilling {start}..{end} (<= {FIRST_PULL_MAX_DAYS}d)")

    # credentials / service (refreshes token on demand)
    try:
        creds = load_credentials(study_id)
        if creds is None:
            log(f"{study_id}: no credentials/pickle -- skipping", "ERROR")
            return [_hist(pull_date, study_id, start, m, 0, "error") for m in DATA_TYPES]
        service = get_service(study_id)
    except Exception as e:
        log(f"{study_id}: token/service FAILED: {e}", "ERROR")
        return [_hist(pull_date, study_id, start, m, 0, "error") for m in DATA_TYPES]

    rows = []
    for dtype in DATA_TYPES:
        try:
            points, errored = list_all_points(service, dtype, start, end, probe=False)
            if errored:
                log(f"{study_id}/{dtype}: API ERROR during pull", "ERROR")
                rows.append(_hist(pull_date, study_id, start, dtype, 0, "error"))
                continue
            n = len(points)
            if points:
                save_json(study_id, f"{start}_to_{end}_{dtype}_raw", points)
                # flatten with the right handler (saved for analysis convenience)
                if dtype == "sleep":
                    flat = flatten_sleep(points)
                elif dtype == "exercise":
                    flat = flatten_exercise(points)
                elif dtype == "activity-level":
                    flat = flatten_activity_level(points)
                else:
                    flat, _ = flatten_points(points, dtype)
                if flat:
                    save_json(study_id, f"{start}_to_{end}_{dtype}_flat", flat)
            status = "ok" if n > 0 else "empty"
            rows.append(_hist(pull_date, study_id, start, dtype, n, status))
            log(f"{study_id}/{dtype}: {n} points ({status})")
        except Exception as e:
            log(f"{study_id}/{dtype}: EXCEPTION {e}", "ERROR")
            rows.append(_hist(pull_date, study_id, start, dtype, 0, "error"))

    log(f"--- {study_id}: done ---")
    return rows

def _hist(pull_date, study_id, data_date, metric, count, status):
    return {"pull_date": pull_date, "study_id": study_id, "data_date": data_date,
            "metric": metric, "point_count": count, "status": status}

# ============================================
# MAIN
# ============================================

def main():
    filter_id = sys.argv[1] if len(sys.argv) > 1 else None
    log("=" * 50)
    log("AUTOMATED HEALTH PULL STARTED")
    if filter_id:
        log(f"(single-participant mode: {filter_id})")
    log("=" * 50)

    ids = collecting_participants(filter_id)
    if not ids:
        log("No collecting participants found (no token_*.pickle files).", "WARNING")
        flush_log()
        return

    log(f"Collecting participants: {len(ids)} -> {', '.join(ids)}")

    all_rows = []
    ok = err = 0
    for sid in ids:
        try:
            rows = pull_participant(sid)
            all_rows.extend(rows)
            if any(r["status"] == "error" for r in rows):
                err += 1
            else:
                ok += 1
        except Exception:
            err += 1
            log(f"{sid}: UNCAUGHT failure:\n{traceback.format_exc()}", "ERROR")

    if all_rows:
        append_history(all_rows)
        log(f"Wrote {len(all_rows)} history rows to {os.path.basename(HISTORY_FILE)}")

    log("=" * 50)
    log(f"PULL COMPLETE: {ok} clean, {err} with errors, {len(ids)} total")
    log("=" * 50)
    path = flush_log()
    print(f"\nLog: {path}")

if __name__ == "__main__":
    main()
