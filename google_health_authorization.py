#!/usr/bin/env python3
"""
GOOGLE HEALTH API AUTHORIZATION SCRIPT

Purpose: Authorize study-owned accounts via the Google Health API
Method: OAuth 2.0 using Google's official client library
Usage: python3 google_health_authorization.py P001
       python3 google_health_authorization.py --status

Scopes: read-only for all data types we collect, PLUS write-only for the
data types we delete from Google's servers after extraction (activity,
health metrics, sleep). Write scopes are REQUIRED for dataPoints.batchDelete.

Requirements:
- client_secret.json in same directory
- Google Health API enabled in Google Cloud Console
- The write scopes below must also be declared on the OAuth consent screen
  (Data Access) and submitted for verification.

IMPORTANT: Scopes are fixed at consent time. Any account authorized under an
older (read-only-only) scope set must be RE-AUTHORIZED with this script before
it can delete data.
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

# ============================================
# CONFIGURATION
# ============================================

WORKING_DIR = "/Users/Perlislab/Desktop/Sparkle/Fitbit"
CREDENTIALS_FILE = "/Users/Perlislab/Desktop/client_secret.json"
DEMO_ACCOUNTS_FILE = os.path.join(WORKING_DIR, "demo_accounts.csv")

# Google Health API scopes.
#   READ-ONLY  -> retrieving data for all metrics we collect
#   WRITE-ONLY -> required by dataPoints.batchDelete to delete data we collect
# We request write scopes ONLY for data types we actually delete
# (activity_and_fitness, health_metrics_and_measurements, sleep). We do NOT
# request location/nutrition write scopes (not collected) or profile/settings
# write scopes (only read, never deleted).
SCOPES = [
    # --- read (retrieval) ---
    'https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly',
    'https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly',
    'https://www.googleapis.com/auth/googlehealth.sleep.readonly',
    'https://www.googleapis.com/auth/googlehealth.profile.readonly',
    'https://www.googleapis.com/auth/googlehealth.settings.readonly',
    # --- write (deletion via batchDelete) ---
    'https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly',
    'https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.writeonly',
    'https://www.googleapis.com/auth/googlehealth.sleep.writeonly',
]

# ============================================
# HELPER FUNCTIONS
# ============================================

def load_accounts():
    """Load demo_accounts.csv and add Google columns if needed"""
    if not os.path.exists(DEMO_ACCOUNTS_FILE):
        print(f"X Error: {DEMO_ACCOUNTS_FILE} not found!")
        sys.exit(1)

    df = pd.read_csv(DEMO_ACCOUNTS_FILE)

    new_columns = {
        'api_version': 'fitbit',
        'google_access_token': None,
        'google_refresh_token': None,
        'google_token_expiry': None,
        'google_user_id': None,
        'google_legacy_user_id': None,
    }
    for col, default in new_columns.items():
        if col not in df.columns:
            df[col] = default

    return df


def save_accounts(df):
    """Save demo_accounts.csv"""
    df.to_csv(DEMO_ACCOUNTS_FILE, index=False)
    print("Saved to demo_accounts.csv")


def get_user_identity(creds):
    """
    Get the account's user IDs from the Health API.

    Per the Google Health API v4 reference:
        getIdentity -> GET /v4/{name=users/*/identity}
    and the response contains 'healthUserId' (canonical) and 'legacyUserId'
    (the old Fitbit encoded id, useful for cross-referencing old-pipeline data).

    Returns (health_user_id, legacy_user_id). NEVER raises -- identity is a
    nice-to-have and must not block token saving.
    """
    from googleapiclient.discovery import build

    try:
        service = build('health', 'v4', credentials=creds)
        identity = service.users().getIdentity(name='users/me/identity').execute()

        health_user_id = identity.get('healthUserId')
        legacy_user_id = identity.get('legacyUserId')

        if health_user_id:
            print(f"Health User ID: {health_user_id}")
        if legacy_user_id:
            print(f"Legacy (Fitbit) User ID: {legacy_user_id}")
        if not health_user_id and not legacy_user_id:
            print("getIdentity returned no recognizable id fields.")
            print(f"   Raw response: {identity}")

        return health_user_id, legacy_user_id

    except Exception as e:
        print(f"Could not fetch user identity (continuing anyway): {e}")
        return None, None


# ============================================
# AUTHORIZATION FUNCTION
# ============================================

def authorize_participant(study_id):
    """Authorize a study-owned account using Google OAuth 2.0."""
    print("\n" + "=" * 60)
    print("  GOOGLE HEALTH API AUTHORIZATION")
    print(f"  Participant: {study_id}")
    print("=" * 60 + "\n")

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"X Error: {CREDENTIALS_FILE} not found!")
        print("\nDownload your OAuth credentials from Google Cloud Console:")
        print("  APIs & Services -> Credentials -> download OAuth client as JSON")
        print(f"  Save as {CREDENTIALS_FILE}")
        return False

    df = load_accounts()

    if study_id not in df['study_id'].values:
        print(f"X Error: {study_id} not found in demo_accounts.csv")
        return False

    idx = df[df['study_id'] == study_id].index[0]

    if pd.notna(df.loc[idx, 'google_refresh_token']) and df.loc[idx, 'api_version'] == 'google':
        print(f"{study_id} is already authorized with Google Health API.")
        print("Re-authorizing will refresh its granted scopes (needed if scopes changed).")
        response = input("Re-authorize anyway? (yes/no): ")
        if response.lower() != 'yes':
            print("X Authorization cancelled.")
            return False

    print("\nSTEP 1: Starting OAuth Authorization Flow")
    print("-" * 60)
    print("A browser window will open to sign in and grant permissions.")
    print("NOTE: the consent screen will now list WRITE/DELETE access in")
    print("addition to read access -- this is expected and required for deletion.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE,
            scopes=SCOPES,
            redirect_uri='http://localhost:8080/'
        )
        creds = flow.run_local_server(
            port=8080,
            success_message='Authorization successful! You can close this window.',
            open_browser=True
        )
        print("\nAuthorization successful!")
    except Exception as e:
        print(f"\nX OAuth flow failed: {e}")
        return False

    # ---- SAVE TOKENS FIRST (so an identity hiccup can't lose them) ----
    print("\nSTEP 2: Saving Tokens")
    print("-" * 60)

    token_expiry = datetime.now() + timedelta(seconds=3600)
    df.loc[idx, 'api_version'] = 'google'
    df.loc[idx, 'google_access_token'] = creds.token
    df.loc[idx, 'google_refresh_token'] = creds.refresh_token
    df.loc[idx, 'google_token_expiry'] = token_expiry.strftime('%Y-%m-%d %H:%M:%S')
    df.loc[idx, 'authorized'] = True
    df.loc[idx, 'collection_status'] = 'authorized'
    save_accounts(df)

    token_file = os.path.join(WORKING_DIR, f"token_{study_id}.pickle")
    with open(token_file, 'wb') as token:
        pickle.dump(creds, token)
    print(f"Saved credential pickle: token_{study_id}.pickle")

    # Record which scopes this credential was granted (so the delete script can
    # verify write access exists before attempting batchDelete).
    granted = list(getattr(creds, 'scopes', []) or [])
    has_write = any('.writeonly' in s for s in granted)
    print(f"Granted scopes include write/delete access: {has_write}")

    if not creds.refresh_token:
        print("WARNING: No refresh token received. Revoke access at")
        print("   https://myaccount.google.com/permissions and re-authorize")
        print("   to obtain one (needed for unattended refresh).")

    # ---- THEN fetch identity (non-fatal) and backfill if available ----
    print("\nSTEP 3: Fetching User Identity (optional)")
    print("-" * 60)
    health_user_id, legacy_user_id = get_user_identity(creds)
    if health_user_id or legacy_user_id:
        df = load_accounts()
        idx = df[df['study_id'] == study_id].index[0]
        if health_user_id:
            df.loc[idx, 'google_user_id'] = health_user_id
        if legacy_user_id:
            df.loc[idx, 'google_legacy_user_id'] = legacy_user_id
        save_accounts(df)

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("AUTHORIZATION COMPLETE")
    print("-" * 60)
    print(f"Participant: {study_id}")
    print("API Version: Google Health API")
    print(f"Health User ID: {health_user_id if health_user_id else '(not retrieved)'}")
    print(f"Legacy User ID: {legacy_user_id if legacy_user_id else '(not retrieved)'}")
    print(f"Write/Delete access granted: {has_write}")
    print(f"Refresh Token: {'SAVED' if creds.refresh_token else 'NOT RECEIVED'}")
    print(f"Token Expiry: {token_expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60 + "\n")
    return True


# ============================================
# UTILITY FUNCTIONS
# ============================================

def check_status():
    """Display authorization status for all Google-authorized accounts."""
    df = load_accounts()
    print("\n" + "=" * 60)
    print("  GOOGLE HEALTH API AUTHORIZATION STATUS")
    print("=" * 60 + "\n")

    google_accounts = df[(df['api_version'] == 'google') | pd.notna(df['google_refresh_token'])]
    if len(google_accounts) == 0:
        print("No accounts authorized with Google Health API yet.\n")
        return

    for _, row in google_accounts.iterrows():
        print(f"Participant: {row['study_id']}")
        print(f"  API Version: {row['api_version']}")
        print(f"  Health User ID: {row.get('google_user_id')}")
        print(f"  Legacy User ID: {row.get('google_legacy_user_id')}")
        print(f"  Has Refresh Token: {pd.notna(row['google_refresh_token'])}")
        print(f"  Token Expiry: {row['google_token_expiry']}")
        print()
    print("=" * 60 + "\n")


def load_credentials(study_id):
    """Load saved credentials for an account, refreshing if expired."""
    token_file = os.path.join(WORKING_DIR, f"token_{study_id}.pickle")
    if not os.path.exists(token_file):
        return None

    with open(token_file, 'rb') as token:
        creds = pickle.load(token)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        df = load_accounts()
        idx = df[df['study_id'] == study_id].index[0]
        df.loc[idx, 'google_access_token'] = creds.token
        df.loc[idx, 'google_token_expiry'] = (
            datetime.now() + timedelta(seconds=3600)
        ).strftime('%Y-%m-%d %H:%M:%S')
        save_accounts(df)
        with open(token_file, 'wb') as token:
            pickle.dump(creds, token)

    return creds


# ============================================
# MAIN
# ============================================

def main():
    print("\n" + "=" * 60)
    print("Google Health API Authorization Tool")
    print("=" * 60 + "\n")

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 google_health_authorization.py P001")
        print("  python3 google_health_authorization.py --status")
        print()
        sys.exit(1)

    if sys.argv[1] == '--status':
        check_status()
    else:
        authorize_participant(sys.argv[1])


if __name__ == "__main__":
    main()
