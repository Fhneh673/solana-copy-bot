"""Разбор транзакции цели: простой своп SOL <-> токен."""
from ..constants import LAMPORTS, WSOL


def parse_swap(tx: dict, wallet: str):
    """-> {side, mint, sol, frac} или None."""
    meta = tx.get("meta")
    if not meta or meta.get("err"):   # meta бывает None у ещё не обработанных транзакций
        return None
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    sol = meta["postBalances"][i] - meta["preBalances"][i]
    if i == 0:
        sol += meta["fee"]  # fee payer: убираем комиссию из дельты

    def owned(balances):
        out = {}
        for b in balances or []:
            if b.get("owner") == wallet:
                out[b["mint"]] = out.get(b["mint"], 0) + int(b["uiTokenAmount"]["amount"])
        return out

    pre, post = owned(meta.get("preTokenBalances")), owned(meta.get("postTokenBalances"))
    deltas = {m: (post.get(m, 0) - pre.get(m, 0), pre.get(m, 0))
              for m in set(pre) | set(post) if post.get(m, 0) != pre.get(m, 0)}
    sol += deltas.pop(WSOL, (0, 0))[0]  # wrapped SOL

    if len(deltas) != 1:  # несколько токенов / стейбл-ноги / не своп
        return None
    mint, (d, before) = next(iter(deltas.items()))
    if d > 0 and sol < 0:
        return {"side": "buy", "mint": mint, "sol": -sol / LAMPORTS, "frac": None, "tokens": d}
    if d < 0 and sol > 0:
        return {"side": "sell", "mint": mint, "sol": sol / LAMPORTS,
                "frac": min(1.0, -d / before) if before else 1.0, "tokens": -d}
    return None
