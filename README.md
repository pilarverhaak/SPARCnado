# SPARCnado
Welcome to the world's okayest Fitbit-to-research-data pipeline: a collection of scripts that bravely venture into the Google Health API and return with Fitbit data. 

## Background

Built to migrate an existing Fitbit Web API study pipeline to the Google Health
API ahead of the September 2026 deprecation. 

--

## What it collects

**Essential metrics** (monitored; a drop to zero is flagged for staff review):
steps, heart rate (≈5-second resolution), heart rate variability (RMSSD),
intraday oxygen saturation, daily oxygen saturation, daily respiratory rate,
nightly skin-temperature derivations, and sleep (with stages).

**Also pulled** (stored, not flagged): distance, active zone minutes, activity
level, active minutes, daily HRV, daily resting heart rate, exercise sessions.

> Note on a few metrics: intraday SpO2 is noisy — the **daily** SpO2 summary is
> the reliable one. Skin-temp baseline needs ~3 days of wear before it
> populates (`NaN` until then). Some data types only exist as daily summaries,
> not intraday (respiratory rate, skin temp).

---

## How it works

```
            ┌─ staff run authorization daily until a participant pairs ─┐
            │                                                           │
   google_health_authorization.py  ──►  writes token_<id>.pickle        │
                                              │                         │
   (cron, once daily) run_daily.sh ───────────┼─────────────────────────┘
            │                                  │
            ├─► automated_health_pull.py       │  for each participant WITH a pickle:
            │       • refresh token (on demand, ~1/day — not hourly)
            │       • first pull: backfill up to 30 days; then: prior day only
            │       • save raw + flattened JSON per metric
            │       • append per-metric point counts to collection_history.csv
            │
            └─► daily_report.py
                    • reads collection_history.csv
                    • per essential metric: consecutive zero-day streak
                    • skin-temp: "baseline establishing" for first 3 days
                    • writes daily_reports/<date>_report.txt
```

A participant enters the pull pipeline automatically once their
`token_<id>.pickle` exists — no manual status change needed.

---

## Files

| File | Role |
|------|------|
| `google_health_authorization.py` | OAuth 2.0 authorization for one account; saves tokens + credential pickle. Run by staff daily per pending participant. |
| `google_health_data_retrieval.py` | All API retrieval logic: per-type filters, pagination, flattening. Importable; also runnable standalone for one participant. |
| `automated_health_pull.py` | Daily orchestrator: selects collecting participants, pulls, writes history. |
| `daily_report.py` | Reads history, computes zero-day flags, writes the morning report. |
| `run_daily.sh` | Single cron entrypoint: runs the pull, then the report. |

### Generated at runtime (not in repo)
`token_*.pickle`, `collection_history.csv`, `fitbit_data_*/`,
`automation_logs/`, `daily_reports/`.

---

## Setup

### Requirements
- **Python 3.12** recommended (3.9 works but is end-of-life; see note below)
- Packages:
  ```bash
  pip3 install google-auth google-auth-oauthlib google-api-python-client pandas
  ```
- A Google Cloud project with the **Google Health API** enabled and an OAuth
  **Desktop** client. Download its JSON as `client_secret.json`.

### Paths
Scripts assume:
- working dir: `/Users/<user>/Desktop/Sparkle/Fitbit`
- scripts + `client_secret.json` on the Desktop

Adjust `WORKING_DIR` / `CREDENTIALS_FILE` at the top of the scripts if different.

### Scopes
Read scopes for all collected metrics, plus **write scopes** (required only for
deletion). Write/delete access is granted at authorization time; tokens minted
before write scopes were added must be re-authorized to gain it.

---

## Usage

**Authorize a participant** (staff, daily until they pair):
```bash
python3 google_health_authorization.py P064
python3 google_health_authorization.py --status   # who's authorized
```

**Manual pull / report** (testing):
```bash
python3 automated_health_pull.py P064     # one participant
python3 automated_health_pull.py          # all collecting participants
python3 daily_report.py --print
```

**Schedule the daily job** (`crontab -e`):
```cron
0 6 * * * /Users/Perlislab/Desktop/run_daily.sh
```
Set `PYTHON=` inside `run_daily.sh` to the output of `which python3`.

---

## Operating notes 

- **Read the report every morning.** The system's whole value is catching when a
  participant stops wearing the device. A *missing* report in `daily_reports/`
  is itself the alarm that cron died.
- **macOS prerequisites:** cron needs **Full Disk Access**
  (System Settings → Privacy & Security → Full Disk Access → `/usr/sbin/cron`),
  and the Mac must be awake at run time
  (`sudo pmset repeat wakeorpoweron MTWRFSU 06:00:00`).
- **After any macOS update, re-verify both.** OS updates have silently revoked
  cron's permissions before.
- **`empty` vs `error`:** a metric reading `empty` means the pull worked but
  there was no data (possible non-wear → flagged). `error` means the API call
  failed (technical problem → not a non-wear signal). The report distinguishes
  these; don't conflate them.

---

## Data deletion

Per-point API deletion (`batchDelete`) is **only supported for a few data types**
(sleep, exercise, and other user-created records) — it does **not** work for the
sensor streams and daily summaries this study collects. Removing all of a
participant's data therefore means **deleting the entire study-owned Google
account** (one account per participant, never reused), done manually. 

---

## Known limitations / TO-DO

- **Python 3.9 is end-of-life.** Upgrade to 3.12 before onboarding real
  participants: install 3.12, reinstall the packages against it, point
  `PYTHON=` in `run_daily.sh` at it, re-run an end-to-end test.
- **Security assessment** (restricted scopes) is required before go-live and is
  the critical-path item — it will scrutinize where token pickles and health
  data are stored.
- **Single point of failure:** one Mac, local credential files, no off-machine
  backup. Consider how data and tokens are protected and backed up.
- History counts are per *pull*, not split by calendar date on the first backfill
  pull. Steady-state daily pulls are one-day-per-row and unaffected. Raw data
  files retain true per-point timestamps regardless.

---





