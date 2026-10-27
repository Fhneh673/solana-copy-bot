"""Фоновый контроль: стоп-лосс, лесенка тейк-профита, трейлинг, время удержания, просадка портфеля."""
import asyncio
import logging
import time

from ..constants import LAMPORTS, WSOL
from ..errors import NoRoute

log = logging.getLogger(__name__)
FAIL_NOTIFY_SEC = 300      # не чаще раза в 5 минут сообщаем о повторяющейся ошибке по одной позиции
FLOW_MIN_SOL = 0.01        # изменение баланса без наших сделок больше этого = пополнение/вывод


class RiskMonitor:
    def __init__(self, trader, cfg: dict):
        self.t = trader
        self.risk = cfg["risk"]
        self.last_values: dict[str, float] = {}  # mint -> оценка позиции в SOL (для /positions)
        self.last_equity: float | None = None
        self._noroute: dict[str, int] = {}       # mint -> сколько проверок подряд нет маршрута продажи
        self._fail_at: dict[str, float] = {}
        self._prev_bal: tuple[float, int] | None = None

    # ---- оценка позиций
    async def _value(self, mint: str, pos: dict) -> float:
        t = self.t
        try:
            q = await t.engine.quote(mint, WSOL, pos["amount"])
            self._noroute.pop(mint, None)
            return int(q["outAmount"]) / LAMPORTS
        except NoRoute as e:
            n = self._noroute[mint] = self._noroute.get(mint, 0) + 1
            limit = self.risk.get("no_route_cycles", 3)
            if n >= limit:   # маршрута продажи нет несколько проверок подряд: токен ничего не стоит
                if n == limit:
                    log.error("нет маршрута продажи %s (%d проверок подряд) — оцениваю в 0: %s", mint[:6], n, e)
                    t.notify(f"🚨 {mint[:6]}..: маршрута продажи нет уже {n} проверок подряд. Оцениваю в 0 SOL "
                             "(возможен rug-pull). Бот продолжит пытаться продать.")
                return 0.0
        except Exception as e:  # noqa: BLE001
            log.warning("нет оценки %s: %s", mint[:6], e)   # сбой сети/API — берём последнюю известную цену
        return self.last_values.get(mint, pos["cost"])

    def _report_failure(self, mint: str, what: str, e: Exception):
        log.error("%s %s: %s", what, mint[:6], e)
        if time.time() - self._fail_at.get(mint, 0) > FAIL_NOTIFY_SEC:
            self._fail_at[mint] = time.time()
            self.t.notify(f"⚠ Не удалось {what} {mint[:6]}..: {str(e)[:200]}\nБудет повторная попытка.")

    # ---- одна позиция
    async def _manage(self, mint: str, value: float) -> bool:
        t, r, st = self.t, self.risk, self.t.state
        pos = st.positions.get(mint)
        if not pos or pos["cost"] <= 0:
            return False
        sl, trail = r.get("stop_loss_pct"), r.get("trailing_stop_pct")
        ratio = value / pos["cost"]  # = текущая цена / цена входа
        pnl = ratio - 1
        pos["peak"] = max(pos.get("peak", 1.0), ratio)
        held_min = (time.time() - pos["opened"]) / 60

        if sl and pnl <= -sl / 100:
            await t.close(mint, 1.0, f"STOP-LOSS {pnl:+.1%}")
        elif trail and pos["peak"] - 1 >= r["trailing_activate_pct"] / 100 \
                and ratio <= pos["peak"] * (1 - trail / 100):
            await t.close(mint, 1.0, f"TRAILING-STOP (макс {pos['peak'] - 1:+.0%}, сейчас {pnl:+.1%})")
        elif r.get("max_hold_minutes") and held_min >= r["max_hold_minutes"]:
            await t.close(mint, 1.0, f"МАКС. ВРЕМЯ {held_min:.0f} мин")
        else:
            for i, lv in enumerate(r.get("take_profit_levels", [])):
                if i in pos["tp_done"] or pnl < lv["pnl_pct"] / 100:
                    continue
                want = lv["sell_pct"] / 100 * pos["initial"]  # sell_pct считаем от размера входа
                frac = min(1.0, want / max(pos["amount"], 1))
                await t.close(mint, frac, f"TAKE-PROFIT +{lv['pnl_pct']:g}% ({pnl:+.1%})")
                pos["tp_done"].append(i)   # уровень засчитываем только ПОСЛЕ успешной продажи
                break  # один уровень за цикл
            else:
                return False
        self._fail_at.pop(mint, None)
        return True

    async def _close_all(self, reason: str):
        for mint in list(self.t.state.positions):
            try:
                await self.t.close(mint, 1.0, reason)
            except Exception as e:  # noqa: BLE001
                self._report_failure(mint, "закрыть", e)

    # ---- основной цикл проверки
    async def check(self):
        t, r, st = self.t, self.risk, self.t.state
        if not t.dry and st.pending:
            await t.resolve_pending()
        snap = {m: dict(p) for m, p in st.positions.items() if p["amount"] > 0}
        values = {m: await self._value(m, p) for m, p in snap.items()}   # котировки вне блокировок
        self.last_values = values

        acted = False
        for mint, value in values.items():
            try:
                acted |= await self._manage(mint, value)
            except Exception as e:  # noqa: BLE001   # одна зависшая позиция не отключает защиту остальных
                self._report_failure(mint, "продать", e)
        st.save()

        if st.halted and r.get("on_drawdown") == "close_all" and st.positions:
            await self._close_all("DRAWDOWN close_all")   # повторяется каждый цикл, пока позиции не закрыты
            return
        if acted:
            return  # equity пересчитаем на следующем цикле

        ops_before = t.ops
        bal = await t.sol_balance()
        self._detect_flow(bal, ops_before)
        equity = bal + sum(values.values())
        self.last_equity = equity
        dd = 1 - equity / st.start_equity if st.start_equity else 0
        log.info("equity=%.4f SOL (старт %.4f) просадка=%.1f%% позиций=%d realized=%+.4f",
                 equity, st.start_equity, dd * 100, len(st.positions), st.realized)

        lim = r.get("max_drawdown_pct")
        if lim and dd * 100 >= lim and not st.halted:
            st.halted = True
            st.save()
            log.error("!!! ЛИМИТ ПРОСАДКИ %.1f%% >= %s%% — копирование остановлено", dd * 100, lim)
            t.notify(f"🛑 ЛИМИТ ПРОСАДКИ: {dd * 100:.1f}% (лимит {lim}%). Новые покупки остановлены.\n"
                     "Возобновить: /resume ДА")
            if r.get("on_drawdown") == "close_all":
                await self._close_all("DRAWDOWN close_all")

    def _detect_flow(self, bal: float, ops_before: int):
        """Пополнение/вывод SOL искажает просадку. Если баланс изменился без единой нашей сделки — сдвигаем старт."""
        t, st = self.t, self.t.state
        if t.dry:
            return
        quiet = t.ops == ops_before and not t.busy()
        prev = self._prev_bal
        if prev and quiet and prev[1] == ops_before and st.start_equity is not None:
            delta = bal - prev[0]
            if abs(delta) > FLOW_MIN_SOL:
                st.start_equity += delta
                st.save()
                log.warning("обнаружено %s %.4f SOL — стартовый капитал скорректирован",
                            "пополнение" if delta > 0 else "вывод", abs(delta))
                t.notify(f"💱 Баланс изменился на {delta:+.4f} SOL без сделок бота (пополнение/вывод). "
                         "Стартовый капитал скорректирован, просадка считается без этого.")
        self._prev_bal = (bal, t.ops) if quiet else None

    async def run(self):
        while True:
            await asyncio.sleep(self.risk["check_interval_sec"])
            try:
                await self.check()
            except Exception as e:  # noqa: BLE001
                log.error("monitor: %s", e)
