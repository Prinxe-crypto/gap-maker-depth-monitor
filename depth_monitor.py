"""
Gap-Maker Depth & Liquidity Monitor
-----------------------------------
Companion script for Prinxe-crypto/Gap-maker.

- Does NOT place any orders
- Does NOT modify Gap-maker files
- Watches open_positions.csv for new entries
- When a new entry is detected, snapshots full order books
  (Polymarket + Kalshi) and calculates depth + estimated slippage
- Also runs on schedule to keep data fresh
"""

import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# ========== CONFIG ==========
GAP_MAKER_REPO = "Prinxe-crypto/Gap-maker"
OPEN_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/open_positions.csv"
CLOSED_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/closed_positions.csv"

SNAPSHOT_FILE = "depth_snapshots.csv"
LAST_SEEN_FILE = "last_seen_entries.json"

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Sizes we care about for slippage estimation
TEST_SIZES = [500, 1000, 2000, 3000, 5000]

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "depth-monitor/1.0"})


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            time.sleep(1.5 ** attempt)
    return None


def load_last_seen():
    if Path(LAST_SEEN_FILE).exists():
        with open(LAST_SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_last_seen(seen):
    with open(LAST_SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def get_poly_book(token_id):
    data = get_json(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if not data:
        return {"bids": [], "asks": []}
    return {
        "bids": [(float(x["price"]), float(x["size"])) for x in data.get("bids", [])],
        "asks": [(float(x["price"]), float(x["size"])) for x in data.get("asks", [])],
    }


def get_poly_tokens(slug):
    data = get_json(f"{GAMMA_BASE}/markets", params={"slug": slug})
    if not data:
        return None, None, None
    m = data[0] if isinstance(data, list) else data
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
    outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
    return tokens, outcomes, m


def get_kalshi_orderbook(ticker):
    data = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
    if not data:
        return {"yes": [], "no": []}
    # Kalshi returns yes/no bids
    ob = data.get("orderbook", data.get("orderbook_fp", {}))
    yes = [(float(p), float(s)) for p, s in ob.get("yes", ob.get("yes_dollars", []))]
    no  = [(float(p), float(s)) for p, s in ob.get("no", ob.get("no_dollars", []))]
    return {"yes": yes, "no": no}


def size_at_or_better(asks, max_price):
    """Total size available at price <= max_price"""
    return sum(size for price, size in asks if price <= max_price)


def estimate_slippage(asks, target_size):
    """Walk the ask book and return average fill price for target_size"""
    if not asks:
        return None, 0
    sorted_asks = sorted(asks, key=lambda x: x[0])
    remaining = target_size
    cost = 0.0
    filled = 0.0
    for price, size in sorted_asks:
        take = min(remaining, size)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        return None, 0
    return round(cost / filled, 4), round(filled, 2)


def snapshot_entry(row):
    """Take full depth snapshot for one open position"""
    asset = row["asset"]
    poly_slug = row["poly_slug"]
    kalshi_ticker = row["kalshi_ticker"]
    direction = row["direction"]
    combined_cost = float(row["combined_cost"])
    logged_at = row["logged_at"]

    print(f"  Snapshotting {asset} | {kalshi_ticker} | dir={direction} | cost={combined_cost}")

    # --- Polymarket ---
    tokens, outcomes, market = get_poly_tokens(poly_slug)
    poly_data = {}
    if tokens and outcomes:
        for i, tid in enumerate(tokens):
            side = outcomes[i]
            book = get_poly_book(tid)
            asks = book["asks"]
            poly_data[side] = {
                "best_ask": min([p for p, s in asks], default=None),
                "size_le_050": size_at_or_better(asks, 0.50),
                "size_le_052": size_at_or_better(asks, 0.52),
                "size_le_055": size_at_or_better(asks, 0.55),
                "size_le_058": size_at_or_better(asks, 0.58),
                "size_le_060": size_at_or_better(asks, 0.60),
            }
            # Slippage estimates
            for size in TEST_SIZES:
                avg_price, filled = estimate_slippage(asks, size)
                poly_data[side][f"slip_{size}"] = avg_price
                poly_data[side][f"filled_{size}"] = filled

    # --- Kalshi ---
    kalshi_ob = get_kalshi_orderbook(kalshi_ticker)

    snapshot = {
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "logged_at": logged_at,
        "asset": asset,
        "kalshi_ticker": kalshi_ticker,
        "poly_slug": poly_slug,
        "direction": direction,
        "combined_cost": combined_cost,
        "poly_up_best_ask": poly_data.get("Up", {}).get("best_ask"),
        "poly_up_size_le_055": poly_data.get("Up", {}).get("size_le_055"),
        "poly_down_best_ask": poly_data.get("Down", {}).get("best_ask"),
        "poly_down_size_le_055": poly_data.get("Down", {}).get("size_le_055"),
        "poly_up_slip_1000": poly_data.get("Up", {}).get("slip_1000"),
        "poly_down_slip_1000": poly_data.get("Down", {}).get("slip_1000"),
        "poly_up_slip_3000": poly_data.get("Up", {}).get("slip_3000"),
        "poly_down_slip_3000": poly_data.get("Down", {}).get("slip_3000"),
        "kalshi_yes_levels": len(kalshi_ob.get("yes", [])),
        "kalshi_no_levels": len(kalshi_ob.get("no", [])),
    }

    # Save raw full books too (optional, for deep analysis)
    snapshot["poly_raw"] = json.dumps(poly_data)
    return snapshot


def main():
    print(f"=== Depth Monitor started at {datetime.now(timezone.utc).isoformat()} ===")

    # Load current open positions from Gap-maker
    try:
        open_df = pd.read_csv(OPEN_POSITIONS_URL)
    except Exception as e:
        print(f"Could not load open_positions.csv: {e}")
        open_df = pd.DataFrame()

    if open_df.empty:
        print("No open positions found.")
        return

    last_seen = load_last_seen()
    current_keys = set()
    new_snapshots = []

    for _, row in open_df.iterrows():
        key = f"{row['kalshi_ticker']}_{row['direction']}_{row['logged_at']}"
        current_keys.add(key)

        if key not in last_seen:
            print(f"New entry detected: {key}")
            snap = snapshot_entry(row)
            new_snapshots.append(snap)

    # Save new snapshots
    if new_snapshots:
        snap_df = pd.DataFrame(new_snapshots)
        if Path(SNAPSHOT_FILE).exists():
            old = pd.read_csv(SNAPSHOT_FILE)
            snap_df = pd.concat([old, snap_df], ignore_index=True)
        snap_df.to_csv(SNAPSHOT_FILE, index=False)
        print(f"Saved {len(new_snapshots)} new depth snapshot(s)")

    # Update last seen
    save_last_seen(current_keys)
    print("=== Depth Monitor finished ===")


if __name__ == "__main__":
    main()
