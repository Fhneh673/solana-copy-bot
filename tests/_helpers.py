"""Общие заглушки для тестов (без сети)."""
import asyncio
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import aiohttp  # noqa: F401
except ImportError:  # запуск без установленных зависимостей
    m = types.ModuleType("aiohttp"); m.ClientSession = object; sys.modules["aiohttp"] = m

from copybot.chain.swapresult import SwapOut  # noqa: E402
from copybot.config import load_config  # noqa: E402
from copybot.constants import WSOL  # noqa: E402
from copybot.errors import ConfirmTimeout, NoRoute, SkipTrade  # noqa: E402
from copybot.control import Controller  # noqa: E402
from copybot.risk.filters import TokenFilters  # noqa: E402
from copybot.risk.monitor import RiskMonitor  # noqa: E402
from copybot.storage.journal import Journal  # noqa: E402
from copybot.storage.state import State  # noqa: E402
from copybot.trader import Trader  # noqa: E402

A = "So11111111111111111111111111111111111111112"   # кошелёк «alpha» (свои настройки)
B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # обычный кошелёк
price = {"v": 1.0}  # цена токена относительно цены входа


class FakeEngine:
    """1 лампорт = 1000 токенов при покупке; продажа по price['v']."""
    name = "fake"

    def __init__(self):
        self.fail_sell: set = set()     # токены, продажа которых падает с ошибкой
        self.no_route: set = set()      # токены, у которых пропал маршрут (квота -> NoRoute)
        self.timeout_buy = False        # покупка «зависает» без подтверждения
        self.delay: dict = {}           # токен -> задержка свопа, сек
        self.impact_checked: list = []  # check_impact каждого свопа

    async def quote(self, i, o, a):
        if "NOSELL" in (i, o):
            raise NoRoute("нет маршрута")
        if {i, o} & self.no_route:
            raise NoRoute("нет маршрута: rug")
        out = int(a * 1000) if i == WSOL else int(a / 1000 * price["v"])
        return {"outAmount": str(out)}

    async def swap(self, i, o, a, min_out=None, check_impact=True):
        self.impact_checked.append(check_impact)
        mint = o if i == WSOL else i
        if self.delay.get(mint):
            await asyncio.sleep(self.delay[mint])
        if i != WSOL and mint in self.fail_sell:
            raise RuntimeError("сеть недоступна")
        out = int((await self.quote(i, o, a))["outAmount"])
        if min_out and out < min_out:
            raise SkipTrade("цена уже хуже цены цели")
        if i == WSOL and self.timeout_buy:
            raise ConfirmTimeout("не подтвердилась", "SIGPEND", out)
        return SwapOut.make(out, "sig123")


class FakeRpc:
    bal = 777                                           # токенов на кошельке (для сверки)
    status = {"confirmationStatus": "confirmed", "err": None}
    MINTS = {"BADMINT": {"mintAuthority": "x", "freezeAuthority": None},
             "FREEZE": {"mintAuthority": None, "freezeAuthority": "y"}}

    async def call(self, method, params):
        if method == "getAccountInfo":
            info = self.MINTS.get(params[0], {"mintAuthority": None, "freezeAuthority": None})
            return {"value": {"data": {"parsed": {"info": info}}}}
        if method == "getSignatureStatuses":
            return {"value": [self.status]}
        raise RuntimeError("не реализовано в тесте: " + method)

    async def token_balance(self, owner, mint):
        return self.bal


class Rec:
    def __init__(self):
        self.msgs = []

    async def send(self, text):
        self.msgs.append(text)


def make(risk=None, filters=None, state_dir=None):
    d = state_dir or tempfile.mkdtemp()
    raw = json.load(open(os.path.join(ROOT, "config.example.json")))
    raw.update(rpc_http="https://rpc.example.test", rpc_ws="",
               state_file=os.path.join(d, "s.json"), journal_file=os.path.join(d, "t.csv"),
               targets=[{"address": A, "name": "alpha", "ratio": 0.5, "max_sol_per_trade": 1.0}, B])
    raw["risk"].update(risk or {})
    raw["filters"].update(filters or {})
    json.dump(raw, open(os.path.join(d, "c.json"), "w"))
    cfg = load_config(os.path.join(d, "c.json"))
    notes = Rec()
    eng = FakeEngine()
    tr = Trader(cfg, FakeRpc(), eng, State(cfg["state_file"], 10.0), "dry-run", notifier=notes,
                journal=Journal(cfg["journal_file"], True), filters=TokenFilters(cfg, FakeRpc()))
    return tr, RiskMonitor(tr, cfg), notes, cfg


def buy(mint, sol=1.0, tokens=10 ** 12):
    return {"side": "buy", "mint": mint, "sol": sol, "frac": None, "tokens": tokens}


def sell(mint, frac=1.0):
    return {"side": "sell", "mint": mint, "sol": 1.0, "frac": frac, "tokens": 1}
