from fill_model import calculate_vwap_fill

"""
Gap-Maker Depth & Liquidity Monitor + Daily Report  (patched)
-------------------------------------------------------------
Fixes vs previous version:
  1. Kalshi books hold BIDS only. Ask for YES = 1 - best NO bid, ask for NO = 1 - best YES bid.
  2. Combined cost now uses the trade's real legs (A = Poly Up + Kalshi No, B = Poly Down + Kalshi Yes).
  3. Asks are sorted ascending before walking the ladder.
  4. Stale snapshots (taken long after logged_at) are tagged STALE_SNAPSHOT and excluded from the report.
  5. Failed book fetches are tagged ERROR_BOOK_UNAVAILABLE instead of looking like thin depth.
  6. No depth snapshot on CLOSED trades (expired books are useless).
  7. Combined VWAP is only computed when BOTH legs fill. Report only averages valid rows.
  8. Logs minutes_into_window so results can be split by entry timing.
"""

import os
import re
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

MAX_SNAPSHOT_AGE_SEC = 90   # older than this after logged_at => STALE_SNAPSHOT
VALID_STATUSES = ("FILLED", "SKIPPED_INSUFFICIENT_DEPTH", "SKIPPED_COST_EXCEEDED")

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "depth-monitor/1.3"})


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            time.sleep(1.5 ** attempt)          # 429 / 5xx: back off and retry
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
    """Returns (book, ok). ok=False means the fetch failed (not the same as an empty book)."""
    data = get_json(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if data is None:
        return {"bids": [], "asks": []}, False
    return {
        "bids": [(float(x["price"]), float(x["size"])) for x in data.get("bids", [])],
        "asks": [(float(x["price"]), float(x["size"])) for x in data.get("asks", [])],
    }, True


def get_poly_tokens(slug):
    data = get_json(f"{GAMMA_BASE}/markets", params={"slug": slug})
    if not data:
        return None, None
    m = data[0] if isinstance(data, list) else data
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
    outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
    return tokens, outcomes


def _levels(arr):
    """Normalise [[price, size], ...] to [(price_in_dollars, size)]. Legacy books use cents."""
    out = []
    for p, s in (arr or []):
        p, s = float(p), float(s)
        if p > 1.0:
            p /= 100.0
        out.append((p, s))
    return out


def kalshi_asks_from_book(ob):
    """Kalshi books contain BIDS only.
       To BUY YES you hit the best NO bid: ask_yes = 1 - no_bid.
       To BUY NO  you hit the best YES bid: ask_no  = 1 - yes_bid."""
    yes_bids = _levels(ob.get("yes_dollars") or ob.get("yes"))
    no_bids = _levels(ob.get("no_dollars") or ob.get("no"))
    yes_asks = sorted((round(1.0 - p, 4), s) for p, s in no_bids)
    no_asks = sorted((round(1.0 - p, 4), s) for p, s in yes_bids)
    return {"yes": yes_asks, "no": no_asks}


def get_kalshi_asks(ticker):
    """Returns (asks_dict, ok)."""
    data = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
    if data is None:
        return {"yes": [], "no": []}, False
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    return kalshi_asks_from_book(ob), True


def size_at_or_better(asks, max_price):
    return sum(size for price, size in asks if price <= max_price)


def _walk(asks, target_shares, max_combined_cost):
    asks = sorted(asks)  # cheapest first, always
    formatted = [{"price": p, "size": s} for p, s in asks]
    return calculate_vwap_fill(formatted, target_shares=target_shares, max_combined_cost=max_combined_cost)


def _full(res):
    """True if the walk filled the whole target (tolerates float dust like 1e-14)."""
    return bool(res) and res.get("unfilled_shares", 1) < 1e-6


def _num(v):
    return v if isinstance(v, (int, float)) else None


def parse_close_time(ticker):
    """KXBTC15M-26SEP201530-30 -> close 2026-09-20 15:30 America/New_York."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})-", ticker or "")
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    try:
        dt = datetime.strptime(f"{yy}{mon}{dd}{hhmm}", "%y%b%d%H%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def snapshot_market(poly_slug, kalshi_ticker, direction, target_shares=100, max_combined_cost=0.80):
    result = {
        "poly_up_best_ask": None, "poly_up_size_055": None, "poly_up_vwap": None, "poly_up_unfilled": None,
        "poly_down_best_ask": None, "poly_down_size_055": None, "poly_down_vwap": None, "poly_down_unfilled": None,
        "kalshi_yes_vwap": None, "kalshi_yes_unfilled": None,
        "kalshi_no_vwap": None, "kalshi_no_unfilled": None,
        "poly_leg": None, "kalshi_leg": None,
        "poly_leg_vwap": None, "kalshi_leg_vwap": None,
        "execution_status": "ERROR_BOOK_UNAVAILABLE",
        "combined_vwap": None,
    }

    # Direction A = Poly Up + Kalshi No ; Direction B = Poly Down + Kalshi Yes
    poly_prefix, kalshi_side = ("poly_up", "no") if str(direction).strip().upper() == "A" else ("poly_down", "yes")
    result["poly_leg"], result["kalshi_leg"] = poly_prefix, kalshi_side

    # --- 1. Polymarket ---
    tokens, outcomes = get_poly_tokens(poly_slug)
    poly_ok = bool(tokens and outcomes)
    poly_res = {}
    if poly_ok:
        for i, tid in enumerate(tokens):
            side = str(outcomes[i]).strip().lower()
            prefix = "poly_up" if side in ("up", "yes") else "poly_down"
            book, ok = get_poly_book(tid)
            poly_ok = poly_ok and ok
            asks = sorted(book["asks"])
            res = _walk(asks, target_shares, max_combined_cost)
            result[f"{prefix}_best_ask"] = asks[0][0] if asks else None
            result[f"{prefix}_size_055"] = round(size_at_or_better(asks, 0.55), 1)
            result[f"{prefix}_vwap"] = _num(res.get("vwap_price")) if _full(res) else None
            result[f"{prefix}_unfilled"] = res.get("unfilled_shares")
            poly_res[prefix] = res

    # --- 2. Kalshi ---
    kalshi_asks, kalshi_ok = get_kalshi_asks(kalshi_ticker)
    kalshi_res = {}
    for side in ("yes", "no"):
        res = _walk(kalshi_asks[side], target_shares, max_combined_cost)
        result[f"kalshi_{side}_vwap"] = _num(res.get("vwap_price")) if _full(res) else None
        result[f"kalshi_{side}_unfilled"] = res.get("unfilled_shares")
        kalshi_res[side] = res

    if not (poly_ok and kalshi_ok):
        return result   # status stays ERROR_BOOK_UNAVAILABLE

    # --- 3. Combined validation on the ACTUAL legs of this trade ---
    p_res = poly_res.get(poly_prefix)
    k_res = kalshi_res.get(kalshi_side)
    p_full = _full(p_res)
    k_full = _full(k_res)

    if not (p_full and k_full):
        result["execution_status"] = "SKIPPED_INSUFFICIENT_DEPTH"
        return result

    result["poly_leg_vwap"] = p_res["vwap_price"]
    result["kalshi_leg_vwap"] = k_res["vwap_price"]
    combined = p_res["vwap_price"] + k_res["vwap_price"]
    result["combined_vwap"] = round(combined, 4)
    result["execution_status"] = "SKIPPED_COST_EXCEEDED" if combined > max_combined_cost else "FILLED"
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
        if key in last_seen:
            continue

        print(f"New OPEN: {row['asset']} | {row['kalshi_ticker']} | {row['direction']}")
        now = datetime.now(timezone.utc)
        logged = pd.to_datetime(row["logged_at"], utc=True, errors="coerce")
        age_sec = (now - logged).total_seconds() if pd.notna(logged) else None
        close_dt = parse_close_time(row["kalshi_ticker"])

        depth = snapshot_market(row["poly_slug"], row["kalshi_ticker"], row["direction"])
        stale = (age_sec is None) or (age_sec > MAX_SNAPSHOT_AGE_SEC) or (close_dt is not None and now >= close_dt)
        if stale:
            depth["execution_status"] = "STALE_SNAPSHOT"

        minutes_into_window = None
        if close_dt is not None and pd.notna(logged):
            minutes_into_window = round(15 - (close_dt - logged.to_pydatetime()).total_seconds() / 60, 2)

        new_rows.append({
            "snapshot_time": now.isoformat(),
            "type": "OPEN",
            "asset": row["asset"],
            "kalshi_ticker": row["kalshi_ticker"],
            "poly_slug": row["poly_slug"],
            "direction": row["direction"],
            "combined_cost": row["combined_cost"],
            "logged_at": row["logged_at"],
            "snapshot_age_sec": None if age_sec is None else round(age_sec, 1),
            "minutes_into_window": minutes_into_window,
            **depth,
        })

    if new_rows:
        df = pd.DataFrame(new_rows)
        if Path(SNAPSHOT_FILE).exists():
            df = pd.concat([pd.read_csv(SNAPSHOT_FILE), df], ignore_index=True)
        df.to_csv(SNAPSHOT_FILE, index=False)
        print(f"Saved {len(new_rows)} OPEN snapshots")

    save_json_set(LAST_SEEN_OPEN_FILE, current_keys)
    return new_rows


def process_closed_positions():
    """Records outcomes only. No order-book snapshot: books are expired at close."""
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
        if key in last_seen:
            continue
        print(f"New CLOSED: {row.get('asset')} | {row.get('kalshi_ticker')}")
        new_rows.append({
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "type": "CLOSED",
            "asset": row.get("asset"),
            "kalshi_ticker": row.get("kalshi_ticker"),
            "poly_slug": row.get("poly_slug"),
            "direction": row.get("direction"),
            "combined_cost": row.get("combined_cost"),
            "logged_at": row.get("logged_at"),
            "profit": row.get("profit"),
            "payout": row.get("payout"),
            "kalshi_outcome": row.get("kalshi_outcome"),
            "polymarket_outcome": row.get("polymarket_outcome"),
        })

    if new_rows:
        df = pd.DataFrame(new_rows)
        if Path(CLOSED_SNAPSHOT_FILE).exists():
            df = pd.concat([pd.read_csv(CLOSED_SNAPSHOT_FILE), df], ignore_index=True)
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

    open_today = open_snaps[open_snaps["snapshot_time"] >= cutoff] if (not open_snaps.empty and "snapshot_time" in open_snaps.columns) else pd.DataFrame()
    closed_today = closed_snaps[closed_snaps["snapshot_time"] >= cutoff] if (not closed_snaps.empty and "snapshot_time" in closed_snaps.columns) else pd.DataFrame()

    lines.append("## Summary (Last 24 hours)\n")
    lines.append(f"- New OPEN snapshots: **{len(open_today)}**")
    lines.append(f"- New CLOSED trades: **{len(closed_today)}**")
    if not open_today.empty and "execution_status" in open_today.columns:
        counts = open_today["execution_status"].fillna("LEGACY_NO_STATUS").value_counts().to_dict()
        lines.append("- Snapshot statuses: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    lines.append("")

    if not closed_today.empty and "profit" in closed_today.columns:
        lines.append(f"- Total simulated profit: **${closed_today['profit'].sum():.2f}**")
        lines.append(f"- Win rate: **{(closed_today['profit'] > 0).mean() * 100:.1f}%**")
        lines.append(f"- Average entry cost: **${closed_today['combined_cost'].mean():.3f}**\n")

    lines.append("## Depth at Entry (valid snapshots only, target 100 contracts per leg)\n")
    valid = open_today[open_today["execution_status"].isin(VALID_STATUSES)] if (not open_today.empty and "execution_status" in open_today.columns) else pd.DataFrame()
    if not valid.empty and "poly_leg" in valid.columns:
        valid = valid[valid["poly_leg"].notna()]   # drop pre-patch rows (wrong legs / no leg VWAPs)

    if valid.empty:
        lines.append("_No valid (fresh, non-error) open snapshots in the last 24 hours._\n")
    else:
        lines.append("| Asset | N | Filled | Skipped depth | Avg mins into window | Poly leg VWAP | Kalshi leg VWAP | Combined VWAP | Logged cost |")
        lines.append("|-------|---|--------|---------------|----------------------|---------------|-----------------|---------------|-------------|")
        for asset in sorted(valid["asset"].dropna().unique()):
            s = valid[valid["asset"] == asset]
            full = s[s["combined_vwap"].notna()]
            fmt = lambda col, d=3: (f"{full[col].mean():.{d}f}" if (not full.empty and col in full.columns and full[col].notna().any()) else "n/a")
            mins = f"{s['minutes_into_window'].mean():.1f}" if "minutes_into_window" in s.columns and s["minutes_into_window"].notna().any() else "n/a"
            lines.append(
                f"| {asset} | {len(s)} | {(s['execution_status'] == 'FILLED').sum()} | "
                f"{(s['execution_status'] == 'SKIPPED_INSUFFICIENT_DEPTH').sum()} | {mins} | "
                f"{fmt('poly_leg_vwap')} | {fmt('kalshi_leg_vwap')} | {fmt('combined_vwap')} | {fmt('combined_cost')} |"
            )
        lines.append("")

    if not closed_today.empty:
        lines.append("## Closed trades\n")
        lines.append("| Asset | Count | Win rate | Avg Profit | Total Profit |")
        lines.append("|-------|-------|----------|------------|--------------|")
        for asset in sorted(closed_today["asset"].dropna().unique()):
            s = closed_today[closed_today["asset"] == asset]
            lines.append(f"| {asset} | {len(s)} | {(s['profit'] > 0).mean() * 100:.0f}% | ${s['profit'].mean():.3f} | ${s['profit'].sum():.2f} |")
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
    lines = ["## Depth Monitor — Latest Run\n",
             f"**Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n",
             "### New Open Positions\n"]
    if not open_snaps:
        lines.append("_None_\n")
    else:
        lines.append("| Asset | Ticker | Dir | Poly leg | Kalshi leg | Combined | Mins in | Status |")
        lines.append("|-------|--------|-----|----------|------------|----------|---------|--------|")
        f3 = lambda v: "n/a" if v is None or (isinstance(v, float) and v != v) else f"${v:.3f}"
        for s in open_snaps:
            lines.append(f"| {s.get('asset')} | {s.get('kalshi_ticker')} | {s.get('direction')} | "
                         f"{f3(s.get('poly_leg_vwap'))} | {f3(s.get('kalshi_leg_vwap'))} | {f3(s.get('combined_vwap'))} | "
                         f"{s.get('minutes_into_window')} | {s.get('execution_status')} |")
        lines.append("")
    lines.append("### New Closed Trades\n")
    if not closed_snaps:
        lines.append("_None_\n")
    else:
        lines.append("| Asset | Ticker | Dir | Cost | Profit |")
        lines.append("|-------|--------|-----|------|--------|")
        for s in closed_snaps:
            lines.append(f"| {s.get('asset')} | {s.get('kalshi_ticker')} | {s.get('direction')} | ${s.get('combined_cost')} | ${s.get('profit')} |")
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
