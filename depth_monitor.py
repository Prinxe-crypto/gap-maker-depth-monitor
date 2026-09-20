"""
Gap-Maker Depth & Liquidity Monitor + Daily Report
--------------------------------------------------
Companion for Prinxe-crypto/Gap-maker

- Snapshots full order books on new OPEN and CLOSED positions
- Calculates depth & estimated slippage
- Writes clean GitHub summary tables
- Generates a Daily Performance + Depth Report
"""

import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ========== CONFIG ==========
GAP_MAKER_REPO = "Prinxe-crypto/Gap-maker"
OPEN_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/open_positions.csv"
CLOSED_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/closed_positions.csv"

SNAPSHOT_FILE = "depth_snapshots.csv"
CLOSED_SNAPSHOT_FILE = "closed_depth_snapshots.csv"
LAST_SEEN_OPEN_FILE = "last_seen_open.json"
LAST_SEEN_CLOSED_FILE = "last_seen_closed.json"
DAILY_REPORT_FILE = "daily_depth_report.md"

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

TEST_SIZES = [500, 1000, 2000, 3000, 5000]

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "depth-monitor/1.2"})


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
