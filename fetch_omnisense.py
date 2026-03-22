#!/usr/bin/env python3
"""
Fetch Omnisense sensor data using requests.Session.
Input dates must be in dd/mm/yyyy format.
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests  # requires: pip install requests

# ── Configuration ─────────────────────────────────────────────────────────────
SITE_NBR = "152865"
BASE = "https://omnisense.com"
OUTPUT_DIR = Path("data/omnisense")
LEGACY_DIR = OUTPUT_DIR / "legacy"
EARLIEST_DATA = "2026-01-25"          # first day with sensor data
DEFAULT_LOOKBACK_DAYS = 90

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE,
}

def rotate_legacy():
    existing = sorted(OUTPUT_DIR.glob("omnisense_*.csv"))
    if not existing:
        return
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)
    for p in existing:
        shutil.move(str(p), str(LEGACY_DIR / p.name))
        print(f"  Archived {p.name} → legacy/")

def main():
    username = os.environ.get("OMNISENSE_USERNAME", "")
    password = os.environ.get("OMNISENSE_PASSWORD", "")
    if not username or not password:
        print("ERROR: OMNISENSE_USERNAME and OMNISENSE_PASSWORD must be set.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--full-history", action="store_true",
                        help=f"Fetch from {EARLIEST_DATA} to today")
    parser.add_argument("--debug", action="store_true",
                        help="Save HTML responses for debugging")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_eat = now_utc + timedelta(hours=3)
    today_str = now_eat.strftime("%Y-%m-%d")
    now_tag = now_utc.strftime("%Y%m%d_%H%M")

    if args.full_history:
        start_date = EARLIEST_DATA
    else:
        # Default: go back 90 days, but never before the earliest data
        start_candidate = (now_eat - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        start_date = max(start_candidate, EARLIEST_DATA)

    # Convert to dd/mm/yyyy (server expects this format for input)
    start_ddmmyyyy = f"{start_date[8:10]}/{start_date[5:7]}/{start_date[0:4]}"
    end_ddmmyyyy   = f"{today_str[8:10]}/{today_str[5:7]}/{today_str[0:4]}"

    print(f"Omnisense fetch — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Date range: {start_date} → {today_str}")
    print(f"  Form dates (dd/mm/yyyy): {start_ddmmyyyy} → {end_ddmmyyyy}")

    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: Login
    print("\n[1/4] Logging in...")
    login_data = {
        "target": "",
        "userId": username,
        "userPass": password,
        "btnAct": "Log-In",
    }
    resp = session.post(f"{BASE}/user_login.asp", data=login_data,
                        headers={"Referer": f"{BASE}/user_login.asp"},
                        allow_redirects=True)
    if resp.status_code != 200:
        print(f"ERROR: Login failed (HTTP {resp.status_code})", file=sys.stderr)
        sys.exit(1)
    if "User Log-In" in resp.text and "userId" in resp.text:
        print("ERROR: Login returned login page – invalid credentials or session issue.", file=sys.stderr)
        sys.exit(1)
    print("  Login successful.")

    # Step 2: Visit download page (establishes site context)
    dnld_url = f"{BASE}/dnld_rqst.asp?siteNbr={SITE_NBR}"
    print("\n[2/4] Visiting download page...")
    resp = session.get(dnld_url, headers={"Referer": f"{BASE}/site_select.asp"})
    if resp.status_code != 200:
        print(f"ERROR: Could not load download page (HTTP {resp.status_code})", file=sys.stderr)
        sys.exit(1)
    print(f"  Page size: {len(resp.text)} bytes")
    if args.debug:
        Path("dnld_rqst.html").write_text(resp.text, encoding="utf-8")
        print("  Saved download page HTML to dnld_rqst.html")

    # Step 3: POST the download form
    print("\n[3/4] Submitting data export request...")
    form_data = {
        "siteNbr": SITE_NBR,
        "sensorId": "",
        "gwayId": "",
        "dateFormat": "SE",          # SE gives yyyy-mm-dd hh:mm:ss in CSV
        "dnldFrDate": start_ddmmyyyy,
        "dnldToDate": end_ddmmyyyy,
        "averaging": "N",
        "btnAct": "Submit",
    }
    resp = session.post(f"{BASE}/dnld_rqst5.asp", data=form_data,
                        headers={"Referer": dnld_url})
    if resp.status_code != 200:
        print(f"ERROR: Form submission failed (HTTP {resp.status_code})", file=sys.stderr)
        sys.exit(1)
    print(f"  Response size: {len(resp.text)} bytes")

    # Save response for debugging if --debug
    if args.debug:
        Path("dnld_rqst5_response.html").write_text(resp.text, encoding="utf-8")
        print("  Saved response HTML to dnld_rqst5_response.html")

    # Parse download link – handles escaped quotes like go(\'/path/file.csv\')
    match = re.search(r"go\(\s*\\'([^']+)\\'\)", resp.text)
    if not match:
        # Also try unescaped version just in case (some servers may not escape)
        match = re.search(r"go\(\s*'([^']+)'\)", resp.text)

    if not match:
        print("WARNING: Could not find download link in response.", file=sys.stderr)
        print("\nDone (no data downloaded).")
        sys.exit(0)

    csv_path = match.group(1)
    if not csv_path.startswith("/fileshare/images/"):
        print(f"ERROR: Unexpected download path: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # Extract row count from the same JavaScript block (optional, for display)
    row_match = re.search(r"(\d+) rows of data", resp.text)
    row_count = row_match.group(1) if row_match else "?"
    print(f"  Download ready: {row_count} rows → {csv_path}")

    # Step 4: Download CSV
    print("\n[4/4] Downloading CSV...")
    csv_url = f"{BASE}{csv_path}"
    resp = session.get(csv_url, headers={"Referer": f"{BASE}/dnld_rqst5.asp"}, stream=True)
    if resp.status_code != 200:
        print(f"ERROR: CSV download failed (HTTP {resp.status_code})", file=sys.stderr)
        sys.exit(1)

    csv_data = resp.content
    if len(csv_data) < 100:
        print(f"ERROR: Downloaded file too small ({len(csv_data)} bytes)", file=sys.stderr)
        sys.exit(1)
    csv_text = csv_data.decode("utf-8", errors="replace")
    if "sensor_desc" not in csv_text:
        print("ERROR: Not an Omnisense CSV", file=sys.stderr)
        print(csv_text[:500], file=sys.stderr)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rotate_legacy()
    out_path = OUTPUT_DIR / f"omnisense_{now_tag}.csv"
    out_path.write_bytes(csv_data)
    size_mb = len(csv_data) / (1024 * 1024)
    print(f"  Wrote {size_mb:.1f} MB → {out_path}")
    print("\nDone.")

if __name__ == "__main__":
    main()
