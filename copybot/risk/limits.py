"""Лимиты на размер входа: на ордер, на долю капитала в монете, резерв SOL."""


def apply_limits(spend: float, risk: dict, balance: float, equity: float, held_cost: float):
    """-> (итоговый размер в SOL или 0, список заметок для лога)."""
    notes = []
    caps = {"баланс минус резерв": balance - risk["min_sol_reserve"]}
    if risk["max_sol_per_trade"]:        # 0 = лимит выключен
        caps["max_sol_per_trade"] = risk["max_sol_per_trade"]
    if risk["max_position_pct"]:         # 0 = лимит выключен
        caps["max_position_pct"] = equity * risk["max_position_pct"] / 100 - held_cost
    for name, cap in caps.items():
        if spend > cap:
            notes.append(f"размер урезан лимитом {name}: {spend:.4f} -> {max(cap, 0):.4f}")
            spend = max(cap, 0.0)
    if spend < risk["min_trade_sol"]:
        return 0.0, notes
    return spend, notes
