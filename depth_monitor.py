"""
Gap-Maker Depth & Liquidity Monitor
-----------------------------------
Companion script for Prinxe-crypto/Gap-maker

Features:
- Detects new open positions and snapshots full order books
- Also processes closed positions (final depth + realized vs expected)
- Writes a clean GitHub Step Summary table
- Never places orders or modifies Gap-maker files
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
CLOSED_SNAPSHOT_FILE = "closed_depth_snapshots.csv"
LAST_SEEN_OPEN_FILE = "last_seen_open.json"
LAST_SEEN_CLOSED_FILE = "last_seen_closed.json"

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

TEST_SIZES = [500, 1000, 2000, 3000, 5000]

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "depth-monitor/1.1"})


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


def load_json_set(path):
    if Path(path).exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_json_set(path, data):
    with open(path, "w") as f:
        json.dump(list(data), f)


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
        return None, None
    m = data[0] if isinstance(data, list) else data
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
    outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
    return tokens, outcomes


def get_kalshi_orderbook(ticker):
    data = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
    if not data:
        return {"yes": [], "no": []}
    ob = data.get("orderbook", data.get("orderbook_fp", {}))
    yes = [(float(p), float(s)) for p, s in ob.get("yes", ob.get("yes_dollars", []))]
    no  = [(float(p), float(s)) for p, s in ob.get("no", ob.get("no_dollars", []))]
    return {"yes": yes, "no": no}


def size_at_or_better(asks, max_price):
    return sum(size for price, size in asks if price <= max_price)


def estimate_slippage(asks, target_size):
    if not asks:
        return None, 0.0
    sorted_asks = sorted(asks, key=lambda x: x[0])
    remaining = float(target_size)
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
        return None, 0.0
    return round(cost / filled, 4), round(filled, 2)


def snapshot_market(poly_slug, kalshi_ticker):
    """Returns depth info for both platforms"""
    result = {
        "poly_up_best_ask": None,
        "poly_up_size_055": None,
        "poly_up_slip_1000": None,
        "poly_up_slip_3000": None,
        "poly_down_best_ask": None,
        "poly_down_size_055": None,
        "poly_down_slip_1000": None,
        "poly_down_slip_3000": None,
        "kalshi_yes_levels": 0,
        "kalshi_no_levels": 0,
    }

    # Polymarket
    tokens, outcomes = get_poly_tokens(poly_slug)
    if tokens and outcomes:
        for i, tid in enumerate(tokens):
            side = outcomes[i]  # "Up" or "Down"
            book = get_poly_book(tid)
            asks = book["asks"]
            prefix = "poly_up" if side == "Up" else "poly_down"

            result[f"{prefix}_best_ask"] = min([p for p, s in asks], default=None)
            result[f"{prefix}_size_055"] = size_at_or_better(asks, 0.55)
            avg1k, _ = estimate_slippage(asks, 1000)
            avg3k, _ = estimate_slippage(asks, 3000)
            result[f"{prefix}_slip_1000"] = avg1k
            result[f"{prefix}_slip_3000"] = avg3k

    # Kalshi
    kalshi_ob = get_kalshi_orderbook(kalshi_ticker)
    result["kalshi_yes_levels"] = len(kalshi_ob.get("yes", []))
    result["kalshi_no_levels"] = len(kalshi_ob.get("no", []))

    return result


def process_open_positions():
    print("\n--- Checking OPEN positions ---")
    try:
        open_df = pd.read_csv(OPEN_POSITIONS_URL)
    except Exception as e:
        print(f"Could not load open_positions: {e}")
        return []

    if open_df.empty:
        print("No open positions.")
        return []

    last_seen = load_json_set(LAST_SEEN_OPEN_FILE)
    current_keys = set()
    new_rows = []

    for _, row in open_df.iterrows():
        key = f"{row['kalshi_ticker']}_{row['direction']}_{row['logged_at']}"
        current_keys.add(key)

        if key not in last_seen:
            print(f"New OPEN entry: {row['asset']} | {row['kalshi_ticker']} | {row['direction']}")
            depth = snapshot_market(row["poly_slug"], row["kalshi_ticker"])

            record = {
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "type": "OPEN",
                "asset": row["asset"],
                "kalshi_ticker": row["kalshi_ticker"],
                "poly_slug": row["poly_slug"],
                "direction": row["direction"],
                "combined_cost": row["combined_cost"],
                "logged_at": row["logged_at"],
                **depth
            }
            new_rows.append(record)

    if new_rows:
        df = pd.DataFrame(new_rows)
        if Path(SNAPSHOT_FILE).exists():
            old = pd.read_csv(SNAPSHOT_FILE)
            df = pd.concat([old, df], ignore_index=True)
        df.to_csv(SNAPSHOT_FILE, index=False)
        print(f"Saved {len(new_rows)} new OPEN snapshots")

    save_json_set(LAST_SEEN_OPEN_FILE, current_keys)
    return new_rows


def process_closed_positions():
    print("\n--- Checking CLOSED positions ---")
    try:
        closed_df = pd.read_csv(CLOSED_POSITIONS_URL)
    except Exception as e:
        print(f"Could not load closed_positions: {e}")
        return []

    if closed_df.empty:
        print("No closed positions.")
        return []

    last_seen = load_json_set(LAST_SEEN_CLOSED_FILE)
    current_keys = set()
    new_rows = []

    for _, row in closed_df.iterrows():
        # Use a stable key
        key = f"{row.get('kalshi_ticker', '')}_{row.get('direction', '')}_{row.get('logged_at', row.get('close_time', ''))}"
        current_keys.add(key)

        if key not in last_seen:
            print(f"New CLOSED trade: {row.get('asset')} | {row.get('kalshi_ticker')}")
            depth = snapshot_market(row.get("poly_slug", ""), row.get("kalshi_ticker", ""))

            record = {
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "type": "CLOSED",
                "asset": row.get("asset"),
                "kalshi_ticker": row.get("kalshi_ticker"),
                "poly_slug": row.get("poly_slug"),
                "direction": row.get("direction"),
                "combined_cost": row.get("combined_cost"),
                "profit": row.get("profit"),
                "payout": row.get("payout"),
                "kalshi_outcome": row.get("kalshi_outcome"),
                "polymarket_outcome": row.get("polymarket_outcome"),
                **depth
            }
            new_rows.append(record)

    if new_rows:
        df = pd.DataFrame(new_rows)
        if Path(CLOSED_SNAPSHOT_FILE).exists():
            old = pd.read_csv(CLOSED_SNAPSHOT_FILE)
            df = pd.concat([old, df], ignore_index=True)
        df.to_csv(CLOSED_SNAPSHOT_FILE, index=False)
        print(f"Saved {len(new_rows)} new CLOSED snapshots")

    save_json_set(LAST_SEEN_CLOSED_FILE, current_keys)
    return new_rows


def write_github_summary(open_snaps, closed_snaps):
    """Write a nice markdown table to GitHub Step Summary"""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    lines = []
    lines.append("## Depth Monitor Summary\n")
    lines.append(f"**Run time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    # Open positions table
    lines.append("### New Open Positions Snapshotted\n")
    if not open_snaps:
        lines.append("_No new open positions detected._\n")
    else:
        lines.append("| Asset | Ticker | Dir | Cost | Poly Up ≤0.55 | Poly Down ≤0.55 | Slip $1k (Up) | Slip $3k (Up) |")
        lines.append("|-------|--------|-----|------|---------------|-----------------|---------------|---------------|")
        for s in open_snaps:
            lines.append(
                f"| {s.get('asset')} | {s.get('kalshi_ticker')} | {s.get('direction')} | "
                f"${s.get('combined_cost')} | {s.get('poly_up_size_055') or '-'} | "
                f"{s.get('poly_down_size_055') or '-'} | {s.get('poly_up_slip_1000') or '-'} | "
                f"{s.get('poly_up_slip_3000') or '-'} |"
            )
        lines.append("")

    # Closed positions table
    lines.append("### New Closed Trades Snapshotted\n")
    if not closed_snaps:
        lines.append("_No new closed trades detected._\n")
    else:
        lines.append("| Asset | Ticker | Dir | Cost | Profit | Poly Up ≤0.55 | Poly Down ≤0.55 |")
        lines.append("|-------|--------|-----|------|--------|---------------|-----------------|")
        for s in closed_snaps:
            lines.append(
                f"| {s.get('asset')} | {s.get('kalshi_ticker')} | {s.get('direction')} | "
                f"${s.get('combined_cost')} | ${s.get('profit')} | "
                f"{s.get('poly_up_size_055') or '-'} | {s.get('poly_down_size_055') or '-'} |"
            )
        lines.append("")

    with open(summary_file, "a") as f:
        f.write("\n".join(lines))


def main():
    print(f"=== Depth Monitor started at {datetime.now(timezone.utc).isoformat()} ===")

    open_snaps = process_open_positions()
    closed_snaps = process_closed_positions()

    write_github_summary(open_snaps, closed_snaps)

    print("\n=== Depth Monitor finished ===")


if __name__ == "__main__":
    main()
