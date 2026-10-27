"""Загрузка и проверка настроек: python tests/test_config.py  или  pytest tests/"""
import json
import os
import tempfile

from _helpers import ROOT  # noqa: F401
from copybot.config import API_URL, LITE_URL, load_config

A = "So11111111111111111111111111111111111111112"


def cfg_from(**over):
    raw = json.load(open(os.path.join(ROOT, "config.example.json")))
    raw.update(rpc_http="https://rpc.example.test", rpc_ws="", targets=[A])
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    json.dump(raw, open(path, "w"))
    return load_config(path)


def bad(**over):
    try:
        cfg_from(**over)
    except ValueError as e:
        return str(e)
    raise SystemExit(f"ожидалась ошибка для {over}")


def test_example_is_valid():
    assert cfg_from()["risk"]["stop_loss_pct"] == 30


def test_typo_is_reported_with_hint():
    msg = bad(risk={"stop_loss": 30})
    assert "stop_loss" in msg and "stop_loss_pct" in msg
    assert "slipage_bps" in bad(slipage_bps=100)


def test_wrong_types_and_ranges():
    assert "числом" in bad(risk={"stop_loss_pct": "30"})
    assert "true или false" in bad(dry_run="yes")
    assert "допустимо" in bad(slippage_bps=-5)
    assert "допустимо" in bad(risk={"max_drawdown_pct": 250})
    assert "списком" in bad(blacklist="abc")
    assert "неизвестные настройки кошелька" in bad(targets=[{"address": A, "ratioo": 1}])


def test_take_profit_levels_validated():
    assert "take_profit_levels" in bad(risk={"take_profit_levels": [{"pnl_pct": 50}]})
    assert "take_profit_levels" in bad(risk={"take_profit_levels": [{"pnl_pct": 50, "sell_pct": 150}]})


def test_jupiter_endpoint_choice():
    assert cfg_from()["jupiter_url"] == LITE_URL
    c = cfg_from(jupiter_api_key="k" * 20)
    assert c["jupiter_url"] == API_URL and c["jupiter_api_key"] == "k" * 20
    assert cfg_from(jupiter_url="https://example.test/swap/v1")["jupiter_url"] == "https://example.test/swap/v1"


def test_per_wallet_overrides_are_validated():
    assert "ratio" in bad(targets=[{"address": A, "ratio": "0.5"}])
    assert "ratio" in bad(targets=[{"address": A, "ratio": -1}])
    assert "mode" in bad(targets=[{"address": A, "mode": "bogus"}])
    assert "enabled" in bad(targets=[{"address": A, "enabled": "no"}])
    assert "percent" in bad(targets=[{"address": A, "percent": 500}])
    ok = cfg_from(targets=[{"address": A, "name": "x", "ratio": 0.5, "mode": "fixed", "enabled": False}])
    assert ok["target_opts"][A]["ratio"] == 0.5


def test_damaged_state_file_gives_readable_error():
    from copybot.storage.state import State
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    with open(path, "w") as f:
        f.write("{not json")
    try:
        State(path, 10.0)
        raise SystemExit("ожидалась ошибка повреждённого state.json")
    except ValueError as e:
        assert "повреждён" in str(e)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("OK: config")
