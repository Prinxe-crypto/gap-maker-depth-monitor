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
OPEN_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/open_positions.csv?v={int(time.time())}"
CLOSED_POSITIONS_URL = f"https://raw.githubusercontent.com/{GAP_MAKER_REPO}/main/closed_positions.csv?v={int(time.time())}"

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

    tokens, outcomes = get_poly_tokens(poly_slug)
    if tokens and outcomes:
        for i, tid in enumerate(tokens):
            side = outcomes[i]
            book = get_poly_book(tid)
            asks = book["asks"]
            prefix = "poly_up" if side == "Up" else "poly_down"

            result[f"{prefix}_best_ask"] = min([p for p, s in asks], default=None)
            result[f"{prefix}_size_055"] = round(size_at_or_better(asks, 0.55), 1)
            avg1k, _ = estimate_slippage(asks, 1000)
            avg3k, _ = estimate_slippage(asks, 3000)
            result[f"{prefix}_slip_1000"] = avg1k
            result[f"{prefix}_slip_3000"] = avg3k

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
            print(f"New OPEN: {row['asset']} | {row['kalshi_ticker']} | {row['direction']}")
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
        print(f"Saved {len(new_rows)} OPEN snapshots")

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
        key = f"{row.get('kalshi_ticker','')}_{row.get('direction','')}_{row.get('logged_at', row.get('close_time',''))}"
        current_keys.add(key)

        if key not in last_seen:
            print(f"New CLOSED: {row.get('asset')} | {row.get('kalshi_ticker')}")
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
        print(f"Saved {len(new_rows)} CLOSED snapshots")

    save_json_set(LAST_SEEN_CLOSED_FILE, current_keys)
    return new_rows


def generate_daily_report():
    print("\n--- Generating Daily Report ---")
    lines = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines.append(f"# Daily Depth & Performance Report — {today}\n")
    lines.append(f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    open_snaps = pd.read_csv(SNAPSHOT_FILE) if Path(SNAPSHOT_FILE).exists() else pd.DataFrame()
    closed_snaps = pd.read_csv(CLOSED_SNAPSHOT_FILE) if Path(CLOSED_SNAPSHOT_FILE).exists() else pd.DataFrame()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    if not open_snaps.empty and "snapshot_time" in open_snaps.columns:
        open_today = open_snaps[open_snaps["snapshot_time"] >= cutoff]
    else:
        open_today = pd.DataFrame()

    if not closed_snaps.empty and "snapshot_time" in closed_snaps.columns:
        closed_today = closed_snaps[closed_snaps["snapshot_time"] >= cutoff]
    else:
        closed_today = pd.DataFrame()

    lines.append("## Summary (Last 24 hours)\n")
    lines.append(f"- New OPEN snapshots: **{len(open_today)}**")
    lines.append(f"- New CLOSED snapshots: **{len(closed_today)}**\n")

    if not closed_today.empty and "profit" in closed_today.columns:
        total_profit = closed_today["profit"].sum()
        win_rate = (closed_today["profit"] > 0).mean() * 100
        avg_cost = closed_today["combined_cost"].mean()
        lines.append(f"- Total simulated profit: **${total_profit:.2f}**")
        lines.append(f"- Win rate: **{win_rate:.1f}%**")
        lines.append(f"- Average entry cost: **${avg_cost:.3f}**\n")

    lines.append("## Depth Available When Entries Happened\n")

    if not open_today.empty:
        lines.append("### On OPEN\n")
        lines.append("| Asset | Avg Size ≤0.55 (Up) | Avg Size ≤0.55 (Down) | Avg Slip $1k (Up) | Avg Slip $3k (Up) |")
        lines.append("|-------|---------------------|-----------------------|-------------------|-------------------|")

        for asset in sorted(open_today["asset"].dropna().unique()):
            subset = open_today[open_today["asset"] == asset]
            avg_up = subset["poly_up_size_055"].mean()
            avg_down = subset["poly_down_size_055"].mean()
            slip1k = subset["poly_up_slip_1000"].mean()
            slip3k = subset["poly_up_slip_3000"].mean()
            lines.append(f"| {asset} | {avg_up:.0f} | {avg_down:.0f} | {slip1k or '-'} | {slip3k or '-'} |")
        lines.append("")
    else:
        lines.append("_No open snapshots in the last 24 hours._\n")

    if not closed_today.empty:
        lines.append("### On CLOSE\n")
        lines.append("| Asset | Count | Avg Profit | Avg Size ≤0.55 (Up) | Avg Size ≤0.55 (Down) |")
        lines.append("|-------|-------|------------|---------------------|-----------------------|")

        for asset in sorted(closed_today["asset"].dropna().unique()):
            subset = closed_today[closed_today["asset"] == asset]
            count = len(subset)
            avg_profit = subset["profit"].mean() if "profit" in subset.columns else 0
            avg_up = subset["poly_up_size_055"].mean()
            avg_down = subset["poly_down_size_055"].mean()
            lines.append(f"| {asset} | {count} | ${avg_profit:.3f} | {avg_up:.0f} | {avg_down:.0f} |")
        lines.append("")

    report = "\n".join(lines)
    with open(DAILY_REPORT_FILE, "w") as f:
        f.write(report)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write("\n\n" + report)

    print("Daily report generated.")
    return report


def write_github_summary(open_snaps, closed_snaps):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    lines = []
    lines.append("## Depth Monitor — Latest Run\n")
    lines.append(f"**Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    lines.append("### New Open Positions\n")
    if not open_snaps:
        lines.append("_None_\n")
    else:
        lines.append("| Asset | Ticker | Dir | Cost | Up ≤0.55 | Down ≤0.55 | Slip $1k | Slip $3k |")
        lines.append("|-------|--------|-----|------|----------|------------|----------|----------|")
        for s in open_snaps:
            lines.append(
                f"| {s.get('asset')} | {s.get('kalshi_ticker')} | {s.get('direction')} | "
                f"${s.get('combined_cost')} | {s.get('poly_up_size_055') or '-'} | "
                f"{s.get('poly_down_size_055') or '-'} | {s.get('poly_up_slip_1000') or '-'} | "
                f"{s.get('poly_up_slip_3000') or '-'} |"
            )
        lines.append("")

    lines.append("### New Closed Trades\n")
    if not closed_snaps:
        lines.append("_None_\n")
    else:
        lines.append("| Asset | Ticker | Dir | Cost | Profit | Up ≤0.55 | Down ≤0.55 |")
        lines.append("|-------|--------|-----|------|--------|----------|------------|")
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
    generate_daily_report()

    print("\n=== Depth Monitor finished ===")


if __name__ == "__main__":
    main()
