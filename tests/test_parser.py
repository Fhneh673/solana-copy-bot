"""Разбор транзакций цели: python tests/test_parser.py  или  pytest tests/"""
import _helpers  # noqa: F401  (путь и заглушки)
from copybot.strategy.parser import parse_swap

W, MINT = "WALLET", "MINTXYZ"


def tx(pre_tok, post_tok, pre=2_000_000_000, post=999_995_000, meta=True, err=None):
    m = {"err": err, "fee": 5000, "preBalances": [pre], "postBalances": [post],
         "preTokenBalances": pre_tok, "postTokenBalances": post_tok} if meta else None
    return {"transaction": {"message": {"accountKeys": [{"pubkey": W}]}}, "meta": m}


def bal(amount):
    return [{"mint": MINT, "owner": W, "uiTokenAmount": {"amount": str(amount)}}]


def test_buy():
    t = parse_swap(tx([], bal(500)), W)
    assert t["side"] == "buy" and t["mint"] == MINT and abs(t["sol"] - 1.0) < 1e-9 and t["tokens"] == 500


def test_sell_fraction():
    t = parse_swap(tx(bal(1000), bal(400), pre=1_000_000_000, post=1_999_995_000), W)
    assert t["side"] == "sell" and abs(t["frac"] - 0.6) < 1e-9 and abs(t["sol"] - 1.0) < 1e-9


def test_meta_none_and_failed_tx_are_ignored():
    assert parse_swap(tx([], bal(1), meta=False), W) is None      # раньше KeyError/TypeError
    assert parse_swap(tx([], bal(1), err={"InstructionError": 1}), W) is None


def test_foreign_wallet_and_multi_token():
    assert parse_swap(tx([], bal(5)), "OTHER") is None
    two = bal(5) + [{"mint": "M2", "owner": W, "uiTokenAmount": {"amount": "9"}}]
    assert parse_swap(tx([], two), W) is None


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("OK: parser")
