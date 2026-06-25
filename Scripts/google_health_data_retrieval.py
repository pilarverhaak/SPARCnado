#!/usr/bin/env python3
"""
GOOGLE HEALTH API DATA RETRIEVAL  (v2 -- built against confirmed response shape)

Confirmed from live probing of P064:
  - dataPoints.list(parent='users/me/dataTypes/{type}') works with NO time args.
  - Response: { "dataPoints": [ {dataSource, <type>:{interval, <value>}}, ... ],
                "nextPageToken": "..." }
  - Each point's time lives at  <type>.interval.startTime  (RFC3339, UTC)
    and a structured local time at <type>.interval.civilStartTime.
  - Values are strings, under a per-type key (steps -> count).

Strategy: the `list` time-window query parameter name is not yet confirmed, so
we pull ALL points per type (paginating on nextPageToken) and filter by date
CLIENT-SIDE using interval.startTime. Correct, if slightly heavier. Once the
server-side filter param is known it can be added as an optimization.

USAGE:
  python3 google_health_data_retrieval.py P064
  python3 google_health_data_retrieval.py P064 --start 2026-06-01 --end 2026-06-23
  python3 google_health_data_retrieval.py P064 --probe        # dump first raw point per type
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from google_health_authorization import load_credentials, WORKING_DIR
except Exception as e:
    print(f"X Could not import from google_health_authorization.py: {e}")
    sys.exit(1)

from googleapiclient.discovery import build

# ============================================
# CONFIGURATION
# ============================================

DATA_TYPES = [
    # --- INTERVAL types (filter: {type}.interval.start_time) ---
    "steps",                 # 1-min, value: count
    "distance",              # 1-min, value: millimeters
    "active-zone-minutes",   # value: activeZoneMinutes + heartRateZone
    "activity-level",        # enum activityLevelType per interval
    "active-minutes",        # bonus activity metric
    # --- SAMPLE types (filter: {type}.sample_time.physical_time) ---
    "heart-rate",            # 5-SECOND resolution; value: beatsPerMinute
    "heart-rate-variability",# ~5-min RMSSD; value: rootMeanSquareOfSuccessiveDifferencesMilliseconds
    "oxygen-saturation",     # intraday SpO2 (NOISY); value: percentage
    # --- DAILY types (filter: {type}.date) ---
    "daily-oxygen-saturation",            # averagePercentage (trustworthy SpO2)
    "daily-heart-rate-variability",       # averageHeartRateVariabilityMilliseconds, entropy
    "daily-respiratory-rate",             # breathsPerMinute
    "daily-sleep-temperature-derivations",# nightlyTemperatureCelsius (+ baseline, may be NaN)
    "daily-resting-heart-rate",           # beatsPerMinute
    # --- SESSION types (nested -> dedicated flatteners) ---
    "sleep",                 # stages[] progression
    "exercise",              # metricsSummary (incl. active calories)
]

# Filter-field PATTERN per type. Three kinds confirmed by live probing:
#   interval -> "{prefix}.interval.start_time"   (RFC-3339)
#   sample   -> "{prefix}.sample_time.physical_time" (RFC-3339)
#   date     -> "{prefix}.date"                  (ISO date)
FILTER_KIND = {
    "steps": "interval", "distance": "interval", "active-zone-minutes": "interval",
    "activity-level": "interval", "active-minutes": "interval",
    "heart-rate": "sample", "heart-rate-variability": "sample",
    "oxygen-saturation": "sample",
    "daily-oxygen-saturation": "date", "daily-heart-rate-variability": "date",
    "daily-respiratory-rate": "date", "daily-sleep-temperature-derivations": "date",
    "daily-resting-heart-rate": "date",
    "sleep": "interval", "exercise": "interval",
}

# Map path-id -> the snake_case prefix used inside the filter expression.
FILTER_PREFIX = {
    "active-zone-minutes": "active_zone_minutes",
    "activity-level": "activity_level",
    "active-minutes": "active_minutes",
    "heart-rate": "heart_rate",
    "heart-rate-variability": "heart_rate_variability",
    "oxygen-saturation": "oxygen_saturation",
    "daily-oxygen-saturation": "daily_oxygen_saturation",
    "daily-heart-rate-variability": "daily_heart_rate_variability",
    "daily-respiratory-rate": "daily_respiratory_rate",
    "daily-sleep-temperature-derivations": "daily_sleep_temperature_derivations",
    "daily-resting-heart-rate": "daily_resting_heart_rate",
}

# Sleep & exercise: hard pageSize cap 25. HR/SpO2 sample types are HIGH volume
# (5-sec HR ~ thousands/day) so pagination is essential.
SMALL_PAGE_TYPES = {"sleep", "exercise"}


# Per-type value extractor. We've CONFIRMED 'steps' -> count (string).
# Others are best-guess keys; raw JSON is always saved regardless, and the
# flattener logs when it can't find the expected value key so we can fix it
# type-by-type as we see real responses.
VALUE_KEYS = {
    # interval types
    "steps": "count",
    "distance": "millimeters",
    "active-zone-minutes": "activeZoneMinutes",
    "active-minutes": "minutes",
    # sample types
    "heart-rate": "beatsPerMinute",
    "heart-rate-variability": "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
    "oxygen-saturation": "percentage",
    # daily types
    "daily-respiratory-rate": "breathsPerMinute",
    "daily-resting-heart-rate": "beatsPerMinute",
    "daily-oxygen-saturation": "averagePercentage",
    "daily-heart-rate-variability": "averageHeartRateVariabilityMilliseconds",
    "daily-sleep-temperature-derivations": "nightlyTemperatureCelsius",
    # activityLevel(enum), sleep, exercise -> dedicated handling
}

# The JSON payload key inside each data point differs from the path id.
PAYLOAD_KEY = {
    "heart-rate": "heartRate",
    "heart-rate-variability": "heartRateVariability",
    "oxygen-saturation": "oxygenSaturation",
    "daily-oxygen-saturation": "dailyOxygenSaturation",
    "daily-heart-rate-variability": "dailyHeartRateVariability",
    "daily-respiratory-rate": "dailyRespiratoryRate",
    "daily-sleep-temperature-derivations": "dailySleepTemperatureDerivations",
    "daily-resting-heart-rate": "dailyRestingHeartRate",
    "active-zone-minutes": "activeZoneMinutes",
    "activityLevel": "activityLevel",
    "activeMinutes": "activeMinutes",
}

def payload_key(dt):
    return PAYLOAD_KEY.get(dt, dt)

PAGE_SIZE = 1000
SAFETY_PAGE_CAP = 200



# ============================================
# PER-TYPE FILTER + PAGE SIZE  (from confirmed `filter` param spec)
# ============================================
# The list `filter` field is AIP-160 syntax and the field name is PREFIXED with
# the data type. Most types filter on interval.start_time; sleep and exercise
# are special-cased per the API spec. Underscores in the data-type prefix:
# the API uses snake_case in filter fields (e.g. active_zone_minutes), while the
# parent path uses hyphens (active-zone-minutes).

# Map hyphenated path name -> snake_case filter prefix


# Sleep and exercise have a hard page-size cap of 25 (default and max).
SMALL_PAGE_TYPES = {"sleep", "exercise"}

def filter_prefix(data_type):
    return FILTER_PREFIX.get(data_type, data_type)

def build_filter(data_type, start_date, end_date):
    """Build AIP-160 filter using the confirmed per-type pattern."""
    pfx = filter_prefix(data_type)
    kind = FILTER_KIND.get(data_type, "interval")
    # sleep filters on end_time, exercise on civil_start_time (special cases)
    if data_type == "sleep":
        f = "sleep.interval.end_time"
        return f'{f} >= "{start_date}T00:00:00Z" AND {f} < "{end_date}T00:00:00Z"'
    if data_type == "exercise":
        f = "exercise.interval.civil_start_time"
        return f'{f} >= "{start_date}" AND {f} < "{end_date}"'
    if kind == "sample":
        f = f"{pfx}.sample_time.physical_time"
        return f'{f} >= "{start_date}T00:00:00Z" AND {f} < "{end_date}T00:00:00Z"'
    if kind == "date":
        f = f"{pfx}.date"
        return f'{f} >= "{start_date}" AND {f} < "{end_date}"'
    # default: interval
    f = f"{pfx}.interval.start_time"
    return f'{f} >= "{start_date}T00:00:00Z" AND {f} < "{end_date}T00:00:00Z"'

def page_size_for(data_type):
    return 25 if data_type in SMALL_PAGE_TYPES else 10000

def get_service(study_id):
    creds = load_credentials(study_id)
    if creds is None:
        print(f"X No saved credentials for {study_id}. Authorize first.")
        sys.exit(1)
    return build("health", "v4", credentials=creds)


def output_dir(study_id):
    d = os.path.join(WORKING_DIR, f"fitbit_data_{study_id}")
    os.makedirs(d, exist_ok=True)
    return d


def save_json(study_id, label, obj):
    path = os.path.join(output_dir(study_id), f"{label}.json")
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    return path


# ============================================
# PROFILE / SETTINGS
# ============================================

def fetch_profile_and_settings(service, study_id):
    for name, method in (("profile", "getProfile"), ("settings", "getSettings")):
        try:
            obj = getattr(service.users(), method)(name=f"users/me/{name}").execute()
            save_json(study_id, name, obj)
            print(f"  {name}: ok")
        except Exception as e:
            print(f"  {name}: FAILED ({e})")


# ============================================
# RETRIEVAL  (list -> paginate -> client-side date filter)
# ============================================

def point_start_date(point, data_type):
    """Pull YYYY-MM-DD from <type>.interval.startTime; fall back to sampleTime."""
    payload = point.get(data_type) if isinstance(point, dict) else None
    if not isinstance(payload, dict):
        # try any nested dict that has an interval/sampleTime
        for v in (point.values() if isinstance(point, dict) else []):
            if isinstance(v, dict) and ("interval" in v or "sampleTime" in v):
                payload = v
                break
    if not isinstance(payload, dict):
        return None
    iv = payload.get("interval") or {}
    ts = iv.get("startTime")
    if not ts:
        st = payload.get("sampleTime") or {}
        ts = st.get("physicalTime")
    if ts and len(ts) >= 10:
        return ts[:10]   # 'YYYY-MM-DD'
    return None


def list_all_points(service, data_type, start_date, end_date, probe=False):
    """Page through data points for a type, using SERVER-SIDE date filter."""
    parent = f"users/me/dataTypes/{data_type}"
    flt = build_filter(data_type, start_date, end_date)
    psize = page_size_for(data_type)
    out = []
    page_token = None
    pages = 0
    while True:
        kwargs = {"parent": parent, "pageSize": psize, "filter": flt}
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().dataTypes().dataPoints().list(**kwargs).execute()
        except Exception as e:
            print(f"    [{data_type}] list FAILED: {e}")
            return out, True   # errored
        pts = resp.get("dataPoints", []) if isinstance(resp, dict) else []
        if probe and pages == 0 and pts:
            print(f"    [{data_type}] first raw point:")
            print("    " + json.dumps(pts[0], indent=2)[:1500].replace("\n", "\n    "))
        out.extend(pts)
        page_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
        pages += 1
        if not page_token or pages >= SAFETY_PAGE_CAP:
            break
    return out, False


def flatten_points(points, data_type):
    """
    Flatten to tidy rows. Handles the THREE confirmed time-shapes:
      - interval types: payload.interval.startTime
      - sample types:   payload.sampleTime.physicalTime
      - daily types:    payload.date {year,month,day}
    Uses payload_key() since the JSON key differs from the path id, and
    coerces "NaN" strings (e.g. skin-temp baseline) to None.
    """
    vkey = VALUE_KEYS.get(data_type)
    pkey = payload_key(data_type)
    kind = FILTER_KIND.get(data_type, "interval")
    rows = []
    skipped = 0
    for p in points:
        payload = p.get(pkey)
        if not isinstance(payload, dict):
            skipped += 1
            continue

        if kind == "date":
            d = payload.get("date") or {}
            date_str = f"{d.get('year','????'):04d}-{d.get('month',0):02d}-{d.get('day',0):02d}" if d else None
            ts_utc = None
            civil = d
        elif kind == "sample":
            st = payload.get("sampleTime") or {}
            ts_utc = st.get("physicalTime")
            date_str = ts_utc[:10] if ts_utc else None
            civil = st.get("civilTime")
        else:  # interval
            iv = payload.get("interval") or {}
            ts_utc = iv.get("startTime")
            date_str = ts_utc[:10] if ts_utc else None
            civil = iv.get("civilStartTime")

        val = payload.get(vkey) if vkey else None
        if isinstance(val, str) and val.strip().lower() == "nan":
            val = None  # skin-temp baseline etc. before 30-day warmup

        rows.append({
            "date": date_str,
            "time_utc": ts_utc,
            "civil": civil,
            "value": val,
            "value_key": vkey,
            "device": (p.get("dataSource") or {}).get("device", {}).get("displayName"),
        })
    return rows, skipped


# ============================================
# MAIN
# ============================================


# ============================================
# DEDICATED FLATTENERS (nested session types)
# ============================================

def flatten_sleep(points):
    """One row per sleep STAGE -> highest-resolution progression through the night."""
    rows = []
    for p in points:
        sleep = p.get("sleep") or {}
        session_iv = sleep.get("interval") or {}
        session_start = session_iv.get("startTime")
        session_end = session_iv.get("endTime")
        stype = sleep.get("type")
        name = p.get("name")
        device = (p.get("dataSource") or {}).get("device", {}).get("displayName")
        for stg in (sleep.get("stages") or []):
            rows.append({
                "session_name": name,
                "session_start": session_start,
                "session_end": session_end,
                "sleep_type": stype,
                "stage": stg.get("type"),          # AWAKE / LIGHT / DEEP / REM
                "stage_start": stg.get("startTime"),
                "stage_end": stg.get("endTime"),
                "utc_offset": stg.get("startUtcOffset"),
                "device": device,
            })
    return rows


def flatten_exercise(points):
    """One row per exercise session with the metricsSummary unpacked."""
    rows = []
    for p in points:
        ex = p.get("exercise") or {}
        iv = ex.get("interval") or {}
        ms = ex.get("metricsSummary") or {}
        rows.append({
            "name": p.get("name"),
            "exercise_type": ex.get("exerciseType"),
            "display_name": ex.get("displayName"),
            "start": iv.get("startTime"),
            "end": iv.get("endTime"),
            "active_duration": ex.get("activeDuration"),
            "calories_kcal": ms.get("caloriesKcal"),
            "distance_mm": ms.get("distanceMillimeters"),
            "steps": ms.get("steps"),
            "avg_hr_bpm": ms.get("averageHeartRateBeatsPerMinute"),
            "avg_pace_s_per_m": ms.get("averagePaceSecondsPerMeter"),
            "device": (p.get("dataSource") or {}).get("device", {}).get("displayName"),
        })
    return rows


def flatten_activity_level(points):
    """activity-level carries an enum (activityLevelType) per interval."""
    rows = []
    for p in points:
        al = p.get("activityLevel") or {}
        iv = al.get("interval") or {}
        rows.append({
            "start": iv.get("startTime"),
            "end": iv.get("endTime"),
            "activity_level": al.get("activityLevelType"),
            "device": (p.get("dataSource") or {}).get("device", {}).get("displayName"),
        })
    return rows


def retrieve(study_id, start_date, end_date, probe):
    print("\n" + "=" * 60)
    print("  GOOGLE HEALTH DATA RETRIEVAL")
    print(f"  Participant: {study_id}")
    print(f"  Window: {start_date} to {end_date}  (client-side filtered)")
    print("=" * 60 + "\n")

    service = get_service(study_id)

    print("Profile & settings:")
    fetch_profile_and_settings(service, study_id)

    print("\nData points by type:")
    summary = {}
    for dtype in DATA_TYPES:
        print(f"  - {dtype} ...")
        in_window, errored = list_all_points(service, dtype, start_date, end_date, probe=probe)

        if errored:
            print(f"      \u26a0\ufe0f  {dtype}: CALL ERRORED -- this is NOT 'no data'. "
                  f"Fix needed before trusting any zero here.")
        if in_window:
            save_json(study_id, f"{start_date}_to_{end_date}_{dtype}_raw", in_window)
            # route to the right flattener
            if dtype == "sleep":
                rows = flatten_sleep(in_window)
            elif dtype == "exercise":
                rows = flatten_exercise(in_window)
            elif dtype == "activity-level":
                rows = flatten_activity_level(in_window)
            else:
                rows, _ = flatten_points(in_window, dtype)
            if rows:
                save_json(study_id, f"{start_date}_to_{end_date}_{dtype}_flat", rows)
                # warn if a plain value type came back without a usable value
                if dtype not in ("sleep", "exercise", "activity-level") and \
                   not any(r.get("value") is not None for r in rows):
                    print(f"      (value key '{VALUE_KEYS.get(dtype)}' not found -- raw saved, flatten TBD)")
        elif not errored:
            print(f"      (confirmed empty: API returned no {dtype} points in window)")

        summary[dtype] = {"in_window": len(in_window)}

    print("\n" + "-" * 60)
    print(f"{'type':28s} {'in-window':>10s}")
    for dtype, s in summary.items():
        print(f"{dtype:28s} {s['in_window']:>10d}")
    print("-" * 60)
    grand = sum(s["in_window"] for s in summary.values())
    print(f"Total points in window: {grand}")
    if grand == 0:
        print("No data in window. Try a wider --start/--end (device last synced ~Jun 5).")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("study_id")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    end_date = args.end or (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now().date() - timedelta(days=30)).strftime("%Y-%m-%d")
    retrieve(args.study_id, start_date, end_date, args.probe)


if __name__ == "__main__":
    main()
