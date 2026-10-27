"""Режимы размера входа (до применения лимитов риска)."""


def raw_size(sizing: dict, target_sol: float, balance: float) -> float:
    mode = sizing["mode"]
    if mode == "ratio":            # множитель от суммы цели (можно > 1)
        return target_sol * sizing["ratio"]
    if mode == "fixed":            # фиксированная сумма на сделку
        return sizing["fixed_sol"]
    if mode == "percent_balance":  # % от текущего свободного баланса
        return balance * sizing["percent"] / 100
    raise ValueError(f"неизвестный sizing.mode: {mode}")
