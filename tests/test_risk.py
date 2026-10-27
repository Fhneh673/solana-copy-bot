"""Тесты торговой логики без сети: python tests/test_risk.py  или  pytest tests/"""
import asyncio
import csv
import os
import time

from _helpers import A, B, Controller, FakeRpc, buy, make, price, sell
from copybot.errors import NoRoute  # noqa: F401
from copybot.storage.state import State


def run(coro):
    price["v"] = 1.0                                   # каждый тест начинается с чистого состояния «сети»
    FakeRpc.bal, FakeRpc.status = 777, {"confirmationStatus": "confirmed", "err": None}
    try:
        return asyncio.run(coro)
    finally:
        price["v"] = 1.0


def test_sizing_filters_guard():
    async def go():
        tr, mon, notes, cfg = make()
        cfg["telegram"]["notify_skips"] = True
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        assert abs(st.positions["M1"]["cost"] - 0.1) < 1e-9                       # ratio 0.1 -> 0.1 SOL
        await tr.copy(A, buy("M2"))
        assert abs(st.positions["M2"]["cost"] - 0.5) < 1e-9                       # своя ratio 0.5 у кошелька alpha
        await tr.copy(B, buy("M3", tokens=2 * 10 ** 12))                          # цена в 2 раза хуже цены цели
        assert "M3" not in st.positions                                           # это skip, а не авария
        await asyncio.sleep(0.01)
        assert any("Пропустил" in m for m in notes.msgs)
        for bad in ("BADMINT", "FREEZE", "NOSELL"):                               # фильтры токенов
            await tr.copy(B, buy(bad))
            assert bad not in st.positions, bad
        price["v"] = 0.5                                                           # «купил-продал» съест 50%
        await tr.copy(B, buy("M5"))
        price["v"] = 1.0
        assert "M5" not in st.positions
        cfg["target_opts"][B]["enabled"] = False                                   # выключенный кошелёк
        await tr.copy(B, buy("M6"))
        assert "M6" not in st.positions
    run(go())


def test_sell_follow_and_stats():
    async def go():
        tr, mon, notes, cfg = make()
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        await tr.copy(A, sell("M1"))                                               # продаёт не владелец позиции
        assert "M1" in st.positions
        await tr.copy(B, sell("M1"))
        assert "M1" not in st.positions and st.wallet(B)["trades"] == 1
        await asyncio.sleep(0.01)
        assert any("ПРОДАЛ" in m for m in notes.msgs) and any("КУПИЛ" in m for m in notes.msgs)
        rows = list(csv.reader(open(cfg["journal_file"], encoding="utf-8")))
        assert rows[0][0] == "time" and [r[2] for r in rows[1:]] == ["BUY", "SELL"]
        assert tr.engine.impact_checked == [True, False]                           # лимит impact только на покупку
    run(go())


def test_tp_ladder_and_sl():
    async def go():
        tr, mon, notes, cfg = make({"take_profit_levels": [{"pnl_pct": 50, "sell_pct": 30},
                                                           {"pnl_pct": 100, "sell_pct": 40}], "stop_loss_pct": 30})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        init = st.positions["M1"]["initial"]
        price["v"] = 1.6
        await mon.check()
        assert st.positions["M1"]["tp_done"] == [0] and abs(st.positions["M1"]["amount"] - 0.7 * init) < 2
        await mon.check()                                                          # уровень уже сработал — повтора нет
        assert st.positions["M1"]["tp_done"] == [0]
        price["v"] = 2.2
        await mon.check()
        assert st.positions["M1"]["tp_done"] == [0, 1] and abs(st.positions["M1"]["amount"] - 0.3 * init) < 2
        price["v"] = 0.5
        await mon.check()                                                          # стоп-лосс по остатку
        assert "M1" not in st.positions
    run(go())


def test_trailing_and_maxhold():
    async def go():
        tr, mon, _, _ = make({"trailing_stop_pct": 10, "trailing_activate_pct": 20, "stop_loss_pct": 0,
                              "take_profit_pct": 0, "max_hold_minutes": 60})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        price["v"] = 1.5
        await mon.check()
        assert "M1" in st.positions and st.positions["M1"]["peak"] > 1.49
        price["v"] = 1.3                                                           # откат > 10% от максимума
        await mon.check()
        assert "M1" not in st.positions
        price["v"] = 1.0
        await tr.copy(B, buy("M2"))
        st.positions["M2"]["opened"] -= 4000                                       # держим дольше 60 минут
        await mon.check()
        assert "M2" not in st.positions
    run(go())


def test_wallet_autopause():
    async def go():
        tr, mon, notes, _ = make({"wallet_max_consecutive_losses": 2, "stop_loss_pct": 30, "take_profit_pct": 0})
        st = tr.state
        await tr.init()
        for m in ("M1", "M2"):
            price["v"] = 1.0
            await tr.copy(B, buy(m))
            price["v"] = 0.5
            await mon.check()
        price["v"] = 1.0
        assert st.wallet(B)["paused"] and st.wallet(B)["streak"] == 2
        await tr.copy(B, buy("M3"))
        assert "M3" not in st.positions
        await asyncio.sleep(0.01)
        assert any("на паузе" in m for m in notes.msgs)
    run(go())


def test_drawdown_halt():
    async def go():
        tr, mon, notes, _ = make({"max_drawdown_pct": 25})
        st = tr.state
        await tr.init()
        st.dry_sol = 5.0
        await mon.check()
        assert st.halted
        await tr.copy(B, buy("M1"))
        assert "M1" not in st.positions
        await asyncio.sleep(0.01)
        assert any("ЛИМИТ ПРОСАДКИ" in m for m in notes.msgs)
    run(go())


def test_controller():
    async def go():
        tr, mon, _, _ = make()
        st = tr.state
        await tr.init()
        c = Controller(tr, mon)
        await tr.copy(B, buy("MINTXYZ"))
        assert "ТЕСТ" in await c.handle("/status", [])
        assert "MINTXYZ" in await c.handle("/positions", [])
        assert "alpha" in await c.handle("/wallets", [])
        assert "Не знаю" in await c.handle("/foo", [])
        await c.handle("/pause", [])
        await tr.copy(B, buy("M2"))
        assert st.paused and "M2" not in st.positions
        await c.handle("/resume", [])
        await c.handle("/wallet", ["pause", "alpha"])
        assert st.wallet(A)["paused"]
        await c.handle("/wallet", ["resume", "alpha"])
        assert not st.wallet(A)["paused"]
        assert "ДА" in await c.handle("/closeall", [])                              # без подтверждения не закрывает
        assert "MINTXYZ" in st.positions
        await c.handle("/closeall", ["ДА"])
        assert not st.positions
        st.halted = True
        assert "/resume ДА" in await c.handle("/resume", [])
        await c.handle("/resume", ["ДА"])
        assert not st.halted
    run(go())


# ---------------------------------------------------------------- сбои продажи и защита остальных позиций
def test_failing_sell_does_not_disable_other_positions():
    async def go():
        tr, mon, notes, _ = make({"stop_loss_pct": 30})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        await tr.copy(B, buy("M2"))
        tr.engine.fail_sell.add("M1")                       # первая позиция не продаётся
        price["v"] = 0.5
        await mon.check()
        assert "M2" not in st.positions                     # стоп-лосс по второй всё равно сработал
        assert "M1" in st.positions
        await asyncio.sleep(0.01)
        assert any("Не удалось" in m for m in notes.msgs)
    run(go())


def test_take_profit_level_retried_after_failure():
    async def go():
        tr, mon, _, _ = make({"take_profit_levels": [{"pnl_pct": 50, "sell_pct": 50}], "stop_loss_pct": 0})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        tr.engine.fail_sell.add("M1")
        price["v"] = 1.6
        await mon.check()
        assert st.positions["M1"]["tp_done"] == []          # продано 0% — уровень не сгорел
        tr.engine.fail_sell.clear()
        await mon.check()
        assert st.positions["M1"]["tp_done"] == [0]
    run(go())


def test_drawdown_close_all_is_retried():
    async def go():
        tr, mon, _, _ = make({"max_drawdown_pct": 25, "on_drawdown": "close_all", "stop_loss_pct": 0,
                              "take_profit_pct": 0})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        await tr.copy(B, buy("M2"))
        tr.engine.fail_sell.add("M1")
        st.dry_sol = 5.0
        await mon.check()
        assert st.halted and "M2" not in st.positions and "M1" in st.positions
        tr.engine.fail_sell.clear()
        await mon.check()                                   # флаг halted уже стоит, но закрытие повторяется
        assert not st.positions
    run(go())


def test_token_without_route_is_valued_at_zero():
    async def go():
        tr, mon, notes, _ = make({"stop_loss_pct": 30})
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("RUG"))
        tr.engine.no_route.add("RUG")
        await mon.check()
        await mon.check()
        assert mon.last_values["RUG"] > 0                   # пока это может быть сбой Jupiter — по последней цене
        await mon.check()                                   # 3-я проверка подряд без маршрута
        assert mon.last_values["RUG"] == 0
        assert "RUG" in st.positions                        # продать не удалось (маршрута нет) — но попытка была
        await asyncio.sleep(0.01)
        assert any("маршрута продажи нет" in m for m in notes.msgs)
    run(go())


# ---------------------------------------------------------------- лимиты, режимы, блокировки, сверка
def test_zero_disables_limits():
    async def go():
        tr, mon, _, _ = make({"max_sol_per_trade": 0, "max_position_pct": 0, "max_open_positions": 0})
        await tr.init()
        await tr.copy(B, buy("M1"))
        assert "M1" in tr.state.positions                   # раньше 0 блокировал любые покупки
        for i in range(12):                                 # и число позиций не ограничено
            await tr.copy(B, buy(f"X{i}"))
        assert len(tr.state.positions) == 13
    run(go())


def test_mode_mismatch_with_positions_is_refused():
    async def go():
        tr, *_ = make()
        st = tr.state
        st.mode = "live"
        st.positions["X"] = State.new_position(1, 0.1, "")
        st.save()
        tr2, *_ = make(state_dir=os.path.dirname(st.path))
        try:
            await tr2.init()
            raise SystemExit("ожидался отказ: режим state.json не совпадает")
        except ValueError:
            pass
    run(go())


def test_mode_switch_without_positions_resets_equity():
    async def go():
        tr, *_ = make()
        st = tr.state
        st.mode, st.start_equity, st.realized = "live", 99.0, 5.0
        st.save()
        tr2, *_ = make(state_dir=os.path.dirname(st.path))
        await tr2.init()
        assert tr2.state.start_equity == 10.0 and tr2.state.realized == 0.0 and tr2.state.mode == "dry"
    run(go())


def test_slow_buy_does_not_block_exits():
    async def go():
        tr, mon, _, _ = make()
        st = tr.state
        await tr.init()
        await tr.copy(B, buy("M1"))
        tr.engine.delay["SLOW"] = 0.6
        slow = asyncio.create_task(tr.copy(B, buy("SLOW")))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await tr.close("M1", 1.0, "стоп")                   # раньше ждало бы всю чужую сделку
        assert time.monotonic() - t0 < 0.3 and "M1" not in st.positions
        await slow
        assert "SLOW" in st.positions
    run(go())


def test_unconfirmed_buy_is_reconciled():
    async def go():
        tr, mon, notes, _ = make()
        st = tr.state
        await tr.init()
        tr.engine.timeout_buy = True
        await tr.copy(B, buy("M1"))
        assert "M1" not in st.positions and "SIGPEND" in st.pending           # транзакция могла пройти
        FakeRpc.status = None                                                  # пока в сети не видна
        await tr.resolve_pending()
        assert "SIGPEND" in st.pending and "M1" not in st.positions
        FakeRpc.status = {"confirmationStatus": "confirmed", "err": None}      # прошла
        await tr.resolve_pending()
        assert not st.pending and st.positions["M1"]["amount"] == 777         # учтено по факту баланса
        tr.engine.timeout_buy = False
        await tr.copy(B, buy("M2"))
        tr.engine.timeout_buy = True
        await tr.copy(B, buy("M3"))
        FakeRpc.status = {"confirmationStatus": None, "err": {"InstructionError": 1}}  # не прошла
        await tr.resolve_pending()
        assert "M3" not in st.positions and not st.pending
    run(go())


def test_unconfirmed_buy_blocks_duplicate_and_counts_as_slot():
    async def go():
        tr, mon, notes, cfg = make(risk={"max_open_positions": 2})
        st = tr.state
        await tr.init()
        tr.engine.timeout_buy = True
        await tr.copy(B, buy("M1"))                      # не подтвердилась: лежит в pending
        tr.engine.timeout_buy = False
        assert "SIGPEND" in st.pending
        await tr.copy(A, buy("M1"))                      # вторая покупка того же токена, пока первая «висит»
        assert "M1" not in st.positions                  # дубль не открылся
        await tr.copy(B, buy("M2"))                      # занято 2 слота из 2: M1 (pending) + M2
        await tr.copy(B, buy("M3"))
        assert "M2" in st.positions and "M3" not in st.positions
    run(go())


def test_state_roundtrip_keeps_mode_and_pending():
    async def go():
        tr, *_ = make()
        await tr.init()
        tr.state.pending["S"] = {"mint": "M", "spend": 0.1, "target": B, "out": 1, "ts": time.time()}
        tr.state.save()
        st2 = State(tr.state.path, 10.0)
        assert st2.mode == "dry" and "S" in st2.pending
    run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ✓", name)
    print("OK: торговая логика")
