"""Jupiter-клиент на подставной сессии: python tests/test_jupiter.py  или  pytest tests/"""
import asyncio

import _helpers  # noqa: F401
from copybot.chain.jupiter import Jupiter
from copybot.errors import ConfirmTimeout, NoRoute, SkipTrade, TxFailed

WSOL = "So11111111111111111111111111111111111111112"
TOK = "TOKEN111111111111111111111111111111111111111"


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data


class Session:
    def __init__(self, data, status=200):
        self.data, self.status, self.calls = data, status, []

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params, headers))
        return Resp(self.data, self.status)


class Rpc:
    def __init__(self, value):
        self.value = value

    async def call(self, method, params):
        return {"value": [self.value]}


def cfg(**kw):
    c = {"dry_run": True, "jupiter_url": "https://api.test/swap/v1", "slippage_bps": 300,
         "max_price_impact_pct": 3, "jupiter_api_key": "KEY123", "confirm_timeout_sec": 0.05}
    c.update(kw)
    return c


def run(c):
    return asyncio.run(c)


def test_quote_sends_api_key():
    s = Session({"outAmount": "100", "priceImpactPct": "0"})
    run(Jupiter(s, None, cfg()).quote(WSOL, TOK, 5))
    url, params, headers = s.calls[0]
    assert url.endswith("/quote") and headers == {"x-api-key": "KEY123"} and params["amount"] == "5"
    s2 = Session({"outAmount": "100"})
    run(Jupiter(s2, None, cfg(jupiter_api_key="")).quote(WSOL, TOK, 5))
    assert s2.calls[0][2] == {}


def test_price_impact_limits_buys_but_not_sells():
    s = Session({"outAmount": "100", "priceImpactPct": "0.10"})   # 10% impact при лимите 3%
    j = Jupiter(s, None, cfg())
    try:
        run(j.swap(WSOL, TOK, 5))
        raise SystemExit("покупка с impact 10% должна быть пропущена")
    except SkipTrade:
        pass
    assert int(run(j.swap(TOK, WSOL, 5, check_impact=False))) == 100   # выход из позиции не блокируется


def test_min_out_is_skip_not_failure():
    j = Jupiter(Session({"outAmount": "100", "priceImpactPct": "0"}), None, cfg())
    try:
        run(j.swap(WSOL, TOK, 5, min_out=200))
        raise SystemExit("ожидался SkipTrade")
    except SkipTrade:
        pass


def test_no_route_and_auth_errors():
    try:
        run(Jupiter(Session({"error": "COULD_NOT_FIND_ROUTE"}), None, cfg()).quote(WSOL, TOK, 5))
        raise SystemExit("ожидался NoRoute")
    except NoRoute:
        pass
    try:
        run(Jupiter(Session({"error": "x"}, status=401), None, cfg()).quote(WSOL, TOK, 5))
        raise SystemExit("ожидалась ошибка ключа")
    except RuntimeError as e:
        assert "ключ" in str(e) and not isinstance(e, NoRoute)


def test_server_error_is_not_no_route():
    try:
        run(Jupiter(Session({"error": "upstream"}, status=503), None, cfg()).quote(WSOL, TOK, 5))
        raise SystemExit("ожидалась ошибка сервера")
    except NoRoute:
        raise SystemExit("HTTP 5xx нельзя путать с «нет маршрута»: иначе сбой API превратится в ложный стоп-лосс")
    except RuntimeError as e:
        assert "503" in str(e)


def test_confirm_timeout_keeps_signature():
    j = Jupiter(Session({}), Rpc(None), cfg())
    try:
        run(j._confirm("SIG1", 777))
        raise SystemExit("ожидался ConfirmTimeout")
    except ConfirmTimeout as e:
        assert e.sig == "SIG1" and e.out == 777        # по подписи покупку можно найти и учесть позже


def test_confirm_failed_and_ok():
    try:
        run(Jupiter(Session({}), Rpc({"err": {"x": 1}}), cfg())._confirm("S"))
        raise SystemExit("ожидался TxFailed")
    except TxFailed:
        pass
    run(Jupiter(Session({}), Rpc({"err": None, "confirmationStatus": "confirmed"}), cfg())._confirm("S"))


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("OK: jupiter")
