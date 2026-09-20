# fill_model.py

def calculate_vwap_fill(ask_ladder, target_shares=100, max_combined_cost=0.80):
    """
    Walks an order book ask ladder for a target size.
    
    Args:
        ask_ladder (list of dict): [{'price': float, 'size': float}, ...] sorted by price ASC.
        target_shares (float): Standardized target size (e.g. 100).
        max_combined_cost (float): Hard ceiling price limit (e.g. 0.80).
        
    Returns:
        dict: Execution breakdown containing fill status, VWAP, and unfilled counts.
    """
    remaining_needed = float(target_shares)
    total_cost = 0.0
    filled_shares = 0.0

    # Ensure ask ladder is sorted from cheapest ask upward
    sorted_asks = sorted(ask_ladder, key=lambda x: float(x['price']))

    for level in sorted_asks:
        price = float(level['price'])
        available_qty = float(level['size'])

        take_qty = min(remaining_needed, available_qty)
        total_cost += take_qty * price
        filled_shares += take_qty
        remaining_needed -= take_qty

        if remaining_needed <= 0:
            break

    vwap_price = total_cost / filled_shares if filled_shares > 0 else 0.0
    unfilled_shares = target_shares - filled_shares

    # Categorize execution outcome
    if filled_shares < target_shares:
        status = 'SKIPPED_INSUFFICIENT_DEPTH'
    elif vwap_price > max_combined_cost:
        status = 'SKIPPED_COST_EXCEEDED'
    else:
        status = 'FILLED'

    return {
        'filled_shares': filled_shares,
        'unfilled_shares': unfilled_shares,
        'vwap_price': round(vwap_price, 4),
        'status': status
    }
1
