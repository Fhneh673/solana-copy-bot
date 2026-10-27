"""Проверка Python-части гибрида с имитацией ядра:  python tests/test_rust_engine.py"""
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
except ImportError:  # для запуска без зависимостей
    m = types.ModuleType("aiohttp"); m.ClientSession = object; sys.modules["aiohttp"] = m

from copybot.config import load_config
from copybot.constants import WSOL
from copybot.engine.rust_engine import RustEngine
from copybot.storage.state import State
from copybot.trader import Trader


async def scenario():
    tmp = tempfile.mkdtemp()
    raw = json.load(open(os.path.join(ROOT, "config.example.json")))
    raw.update(rpc_http="https://rpc.example.test", rpc_ws="", state_file=os.path.join(tmp, "st.json"),
               targets=["So11111111111111111111111111111111111111112"])
    json.dump(raw, open(os.path.join(tmp, "c.json"), "w"))
    cfg = load_config(os.path.join(tmp, "c.json"))

    eng = RustEngine(cfg, [sys.executable, os.path.join(ROOT, "tests", "mock_core.py")])
    await eng.start()
    print("start ok, pubkey:", eng.pubkey)

    q = await eng.quote(WSOL, "MINTAAAAAA", 1_000_000)
    assert q["outAmount"] == "1000000000", q
    assert await eng.swap(WSOL, "MINTAAAAAA", 1_000_000) == 1_000_000_000
    try:
        await eng.swap(WSOL, "MINTAAAAAA", 1_000_000, min_out=10 ** 12)   # цена хуже ожидаемой
        raise SystemExit("ожидалась ошибка min_out")
    except RuntimeError as e:
        print("min_out проверяется ядром:", e)
    sw = await eng.swap(WSOL, "MINTAAAAAA", 1_000_000, min_out=1)
    assert sw.sig == "mocksig", sw.sig
    try:
        await eng.swap(WSOL, "FAIL", 1)
        raise SystemExit("ожидалась ошибка")
    except RuntimeError as e:
        print("ошибка ядра проброшена:", e)

    trader = Trader(cfg, None, eng, State(cfg["state_file"], 10.0), "dry-run")
    await trader.init()
    runner = asyncio.create_task(eng.run(trader))
    await asyncio.sleep(1.6)          # buy в +0.4с, sell в +1.0с
    runner.cancel()
    st = trader.state
    print("позиции:", st.positions, "| баланс:", round(st.dry_sol, 4), "| realized:", round(st.realized, 4))
    assert st.positions == {}, "позиция должна закрыться продажей цели"
    assert abs(st.dry_sol - 10.0) < 1e-9 and abs(st.realized) < 1e-9
    await eng.stop()
    print("OK: гибрид Python + (mock) ядро работает")


def test_hybrid_with_mock_core():
    asyncio.run(scenario())


if __name__ == "__main__":
    test_hybrid_with_mock_core()
