"""Оркестратор: повторяет сделки цели с учётом размера, фильтров и лимитов.

Блокировки: на каждый токен своя (сделки по одному токену идут строго по очереди), плюс короткая общая
блокировка только на «решение» (лимиты, размер, резерв суммы). Своп и ожидание подтверждения идут БЕЗ общей
блокировки, поэтому стоп-лоссы и копирование других токенов не ждут чужую сделку."""
import asyncio
import logging
import time

from .chain.rpc import Rpc
from .constants import LAMPORTS, WSOL
from .errors import ConfirmTimeout, SkipTrade
from .notify import NullNotifier
from .risk.limits import apply_limits
from .storage.state import State
from .strategy.sizing import raw_size

log = logging.getLogger(__name__)
PENDING_DROP_SEC = 180   # столько ждём транзакцию без подтверждения, потом считаем, что она не прошла


class Trader:
    def __init__(self, cfg: dict, rpc: Rpc, engine, state: State, my_wallet: str,
                 notifier=None, journal=None, filters=None):
        self.cfg, self.rpc, self.engine, self.state = cfg, rpc, engine, state
        self.me = my_wallet
        self.dry = cfg["dry_run"]
        self.notifier = notifier or NullNotifier()
        self.journal, self.filters = journal, filters
        self.lock = asyncio.Lock()          # короткая: только решение о покупке
        self._mlocks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, float] = {}   # mint -> SOL, зарезервированные под идущую покупку
        self.ops = 0                        # растёт при каждом начале/конце свопа (для обнаружения пополнений)
        self._tasks: set = set()

    # ---- вспомогательное
    def notify(self, text: str):
        t = asyncio.create_task(self.notifier.send(text))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def wname(self, addr: str) -> str:
        return (self.cfg["target_opts"].get(addr, {}).get("name")) or (addr[:6] + "..") if addr else "—"

    def short(self, mint: str) -> str:
        return mint[:6] + ".."

    def _mlock(self, mint: str) -> asyncio.Lock:
        return self._mlocks.setdefault(mint, asyncio.Lock())

    def busy(self) -> bool:
        return bool(self._inflight or self.state.pending or any(l.locked() for l in self._mlocks.values()))

    async def sol_balance(self) -> float:
        return self.state.dry_sol if self.dry else await self.rpc.sol_balance(self.me)

    async def init(self):
        st = self.state
        mode = "dry" if self.dry else "live"
        if st.mode and st.mode != mode:
            if st.positions or st.pending:
                raise ValueError(
                    f"state.json создан в режиме «{st.mode}», а сейчас «{mode}», и в нём есть позиции. "
                    "Закройте их в прежнем режиме или удалите state.json (позиции из теста в реальном режиме "
                    "не существуют, и наоборот).")
            log.warning("режим сменился (%s -> %s): стартовый капитал и статистика начаты заново", st.mode, mode)
            st.start_equity, st.halted, st.realized, st.wallets = None, False, 0.0, {}
        st.mode = mode
        if not self.dry:
            try:
                await self.reconcile()
            except Exception as e:  # noqa: BLE001
                log.warning("сверка с кошельком не удалась: %s", e)
        if st.start_equity is None:
            st.start_equity = await self.sol_balance() + st.cost_total()
        st.save()
        log.info("режим: %s | sizing: %s | стартовый капитал: %.4f SOL | лимит просадки: %s%%",
                 "DRY_RUN" if self.dry else "LIVE", self.cfg["sizing"]["mode"],
                 st.start_equity, self.cfg["risk"].get("max_drawdown_pct"))

    # ---- копирование сделки цели
    async def copy(self, target: str, t: dict):
        cfg = self.cfg
        mint, name = t["mint"], self.wname(target)
        opts = cfg["target_opts"].get(target, {})
        if opts.get("enabled") is False:
            return log.info("skip: кошелёк %s выключен в настройках", name)
        if mint in cfg["blacklist"]:
            return log.info("skip: %s в blacklist", mint)

        if t["side"] == "sell":
            pos = self.state.positions.get(mint)
            if not pos:
                return log.info("skip: нет позиции по %s", self.short(mint))
            if cfg["sell_follow"] == "owner" and pos["target"] and pos["target"] != target:
                return log.info("skip: продаёт %s, а позицию открыл другой кошелёк", name)
            return await self.close(mint, t["frac"], f"продажа цели {name}")

        async with self._mlock(mint):
            await self._buy(target, t, name, opts)

    async def _buy(self, target: str, t: dict, name: str, opts: dict):
        st, cfg, risk = self.state, self.cfg, self.cfg["risk"]
        mint = t["mint"]
        async with self.lock:   # решение + резерв суммы; всё остальное — без общей блокировки
            w = st.wallet(target)
            if st.halted:
                return log.info("skip buy: копирование остановлено (лимит просадки)")
            if st.paused:
                return log.info("skip buy: бот на паузе")
            if w["paused"]:
                return log.info("skip buy: кошелёк %s на паузе (плохая статистика)", name)
            if t["sol"] < cfg["min_target_sol"]:
                return log.info("skip: покупка %.3f SOL < min_target_sol", t["sol"])
            if mint in st.positions and cfg["one_position_per_mint"]:
                return log.info("skip: позиция по %s уже есть", self.short(mint))
            unconfirmed = {p["mint"] for p in st.pending.values()}   # покупки, которые могли пройти
            if mint in unconfirmed and cfg["one_position_per_mint"]:
                return log.info("skip: прошлая покупка %s ещё не подтверждена", self.short(mint))
            limit = risk["max_open_positions"]   # 0 = без ограничения
            if limit and mint not in st.positions and mint not in unconfirmed \
                    and len(set(st.positions) | set(self._inflight) | unconfirmed) >= limit:
                return log.info("skip: достигнут max_open_positions")

            reserved = sum(self._inflight.values())
            bal = await self.sol_balance() - reserved
            sizing = {**cfg["sizing"], **{k: opts[k] for k in ("mode", "ratio", "fixed_sol", "percent") if k in opts}}
            spend = raw_size(sizing, t["sol"], bal)
            equity = bal + st.cost_total() + reserved
            held = st.positions.get(mint, {}).get("cost", 0.0)
            rk = {**risk, **({"max_sol_per_trade": opts["max_sol_per_trade"]} if "max_sol_per_trade" in opts else {})}
            spend, notes = apply_limits(spend, rk, bal, equity, held)
            for n in notes:
                log.info(n)
            if spend <= 0:
                return log.info("skip: размер после лимитов меньше min_trade_sol")
            self._inflight[mint] = spend

        try:
            await self._buy_execute(target, t, name, spend)
        except SkipTrade as e:
            log.info("skip %s: %s", self.short(mint), e)
            self._notify_skip(mint, str(e))
        finally:
            self._inflight.pop(mint, None)

    def _notify_skip(self, mint: str, reason: str):
        if self.cfg["telegram"].get("notify_skips"):
            self.notify(f"⛔ Пропустил {self.short(mint)}: {reason}")

    async def _buy_execute(self, target: str, t: dict, name: str, spend: float):
        st, cfg = self.state, self.cfg
        mint = t["mint"]
        spend_l = int(spend * LAMPORTS)
        # сколько токенов мы бы получили по цене цели (для проверки цены и фильтров)
        expected = None
        if t.get("tokens") and t["sol"] > 0:
            expected = int(spend_l * t["tokens"] / (t["sol"] * LAMPORTS))
        if self.filters:
            reason = await self.filters.check_buy(self.engine, mint, spend_l, expected)
            if reason:
                log.info("skip %s: %s", self.short(mint), reason)
                return self._notify_skip(mint, reason)
        f = cfg["filters"]
        dev = f["max_price_deviation_pct"] if f["enabled"] else 0
        min_out = int(expected * (1 - dev / 100)) if (expected and dev) else None

        self.ops += 1
        try:
            out = await self.engine.swap(WSOL, mint, spend_l, min_out=min_out, check_impact=True)
        except ConfirmTimeout as e:
            # Транзакция могла пройти: нельзя считать покупку неудачной. Запоминаем и сверяем позже.
            self._add_pending(e, mint, spend, target)
            return
        finally:
            self.ops += 1

        fill = await self._fill(getattr(out, "sig", None), mint)
        amount = fill["tokens"] if fill and fill["tokens"] > 0 else int(out)
        fees = (-fill["sol"] - spend) if fill else 0.0   # комиссии, чаевые, аренда токен-аккаунта
        if self.dry:
            st.dry_sol -= spend
        self._apply_buy(mint, amount, spend, fees, target)
        st.save()
        if self.journal:
            self.journal.write("BUY", f"сигнал {name}", target, mint, spend + fees, tx=getattr(out, "sig", None))
        log.info("BUY %s на %.4f SOL (+%.4f комиссии) (цель %s вложила %.3f)",
                 self.short(mint), spend, fees, name, t["sol"])
        self.notify(f"🟢 КУПИЛ {self.short(mint)} на {spend:.4f} SOL\nпо сигналу: {name} (он вложил {t['sol']:.3f} SOL)")

    def _apply_buy(self, mint: str, amount: int, spend: float, fees: float, target: str):
        st = self.state
        pos = st.positions.get(mint)
        if pos:  # докупка
            pos["amount"] += amount
            pos["initial"] += amount
            pos["cost"] += spend
            pos["fees"] += fees
        else:
            st.positions[mint] = State.new_position(amount, spend, target)
            st.positions[mint]["fees"] = fees

    # ---- реальные суммы исполнения (по факту, а не по котировке)
    async def _fill(self, sig, mint: str):
        """Что реально изменилось на кошельке: SOL (с комиссиями и арендой) и токены. None — не удалось узнать."""
        if self.dry or not sig:
            return None
        try:
            tx = await self.rpc.get_tx(sig, tries=4)
            meta = tx.get("meta") if tx else None
            if not meta:
                return None
            keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
            i = keys.index(self.me)
            sol = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS

            def held(bals):
                return sum(int(b["uiTokenAmount"]["amount"]) for b in bals or []
                           if b.get("owner") == self.me and b.get("mint") == mint)
            tokens = held(meta.get("postTokenBalances")) - held(meta.get("preTokenBalances"))
            return {"sol": sol, "tokens": tokens}
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось узнать фактическое исполнение %s: %s (считаю по котировке)", str(sig)[:8], e)
            return None

    # ---- покупки без подтверждения и сверка с кошельком
    def _add_pending(self, e: ConfirmTimeout, mint: str, spend: float, target: str):
        if not e.sig:
            log.error("покупка %s: нет подтверждения и нет подписи: %s", self.short(mint), e)
            return
        self.state.pending[e.sig] = {"mint": mint, "spend": spend, "target": target,
                                     "out": e.out or 0, "ts": time.time()}
        self.state.save()
        log.warning("покупка %s не подтверждена вовремя — проверю позже (https://solscan.io/tx/%s)",
                    self.short(mint), e.sig)
        self.notify(f"⚠ Покупка {self.short(mint)} не подтвердилась за таймаут. Проверяю в сети, "
                    f"позиция появится, если транзакция прошла.\nhttps://solscan.io/tx/{e.sig}")

    async def resolve_pending(self):
        st = self.state
        for sig, p in list(st.pending.items()):
            try:
                res = await self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
                s = res["value"][0]
            except Exception as e:  # noqa: BLE001
                log.warning("проверка %s: %s", sig[:8], e)
                continue
            age = time.time() - p["ts"]
            if s and s.get("err"):
                st.pending.pop(sig, None)
                st.save()
                log.info("pending %s: транзакция не прошла в сети (%s)", sig[:8], s["err"])
            elif s and s.get("confirmationStatus") in ("confirmed", "finalized"):
                async with self._mlock(p["mint"]):
                    await self._adopt_pending(sig, p)
            elif not s and age > PENDING_DROP_SEC:
                st.pending.pop(sig, None)
                st.save()
                log.info("pending %s: транзакция так и не появилась в сети, считаю неудачной", sig[:8])

    async def _adopt_pending(self, sig: str, p: dict):
        st, mint = self.state, p["mint"]
        tracked = st.positions.get(mint, {}).get("amount", 0)
        bal = await self.rpc.token_balance(self.me, mint)
        fill = await self._fill(sig, mint)
        if fill and fill["tokens"] > 0:
            amount = fill["tokens"]
        else:
            amount = bal - tracked if bal > tracked else int(p.get("out") or 0)
        if amount <= 0:
            st.pending.pop(sig, None)
            st.save()
            return log.warning("pending %s: транзакция прошла, но токенов не прибавилось", sig[:8])
        fees = (-fill["sol"] - p["spend"]) if fill else 0.0
        self._apply_buy(mint, amount, p["spend"], fees, p["target"])
        st.pending.pop(sig, None)
        st.save()
        if self.journal:
            self.journal.write("BUY", "подтверждена позже", p["target"], mint, p["spend"] + fees, tx=sig)
        log.warning("BUY %s учтена после проверки (+%d токенов)", self.short(mint), amount)
        self.notify(f"🟢 Покупка {self.short(mint)} всё-таки прошла — позиция учтена ({p['spend']:.4f} SOL)")

    async def reconcile(self):
        """При старте: добираем неподтверждённые покупки и находим токены на кошельке, о которых бот не знает."""
        await self.resolve_pending()
        if not self.cfg.get("adopt_unknown_tokens", True):
            return
        st, risk = self.state, self.cfg["risk"]
        pending_mints = {p["mint"] for p in st.pending.values()}
        for mint, amt in (await self.rpc.token_balances(self.me)).items():
            if mint == WSOL or mint in st.positions or mint in pending_mints or mint in self.cfg["blacklist"]:
                continue
            try:
                q = await self.engine.quote(mint, WSOL, amt)
                value = int(q["outAmount"]) / LAMPORTS
            except Exception:  # noqa: BLE001
                continue   # пыль / мусор без маршрута — не трогаем
            if value < risk["min_trade_sol"]:
                continue
            st.positions[mint] = State.new_position(amt, value, "")
            st.save()
            log.warning("найден токен вне учёта: %s на ~%.4f SOL — взял под защиту (стоп-лосс и т.д.)",
                        self.short(mint), value)
            self.notify(f"🔎 На кошельке найден токен {self.short(mint)} (~{value:.4f} SOL), которого не было в учёте. "
                        "Взял под управление: стоп-лосс и остальные правила действуют от текущей цены.")

    # ---- закрытие (полное/частичное)
    async def close(self, mint: str, frac: float, reason: str):
        async with self._mlock(mint):
            await self._close(mint, frac, reason)

    async def _close(self, mint: str, frac: float, reason: str):
        st = self.state
        pos = st.positions.get(mint)
        if not pos:
            return
        have = pos["amount"] if self.dry else await self.rpc.token_balance(self.me, mint)
        amount = int(have * min(frac, 1.0))
        if amount <= 0:
            st.positions.pop(mint, None)
            st.save()
            return
        self.ops += 1
        try:
            # лимит price impact на выход не действует: из позиции нужно уметь выходить
            out = await self.engine.swap(mint, WSOL, amount, check_impact=False)
        except ConfirmTimeout as e:
            log.warning("продажа %s не подтвердилась (%s); баланс сверится на следующей проверке", self.short(mint), e.sig)
            raise
        finally:
            self.ops += 1
        fill = await self._fill(getattr(out, "sig", None), WSOL)
        proceeds = fill["sol"] if fill else int(out) / LAMPORTS   # реально полученное, уже за вычетом комиссии
        portion = amount / have
        cost_part, fees_part = pos["cost"] * portion, pos["fees"] * portion
        pnl = proceeds - cost_part - fees_part
        st.realized += pnl
        pos["pnl_acc"] += pnl
        if self.dry:
            st.dry_sol += proceeds
        pos["amount"] = have - amount
        pos["cost"] -= cost_part
        pos["fees"] -= fees_part
        if portion >= 0.999 or pos["amount"] <= 0:
            st.positions.pop(mint, None)
            self._finish_position(pos)
        st.save()
        if self.journal:
            self.journal.write("SELL", reason, pos.get("target", ""), mint, proceeds, pnl=pnl,
                               tx=getattr(out, "sig", None))
        log.info("SELL %s %.0f%% [%s] pnl=%+.4f SOL | realized=%+.4f", self.short(mint), portion * 100,
                 reason, pnl, st.realized)
        icon = "🟩" if pnl >= 0 else "🟥"
        self.notify(f"{icon} ПРОДАЛ {self.short(mint)} ({portion * 100:.0f}%)\nпричина: {reason}\n"
                    f"результат: {pnl:+.4f} SOL | всего: {st.realized:+.4f} SOL")

    def _finish_position(self, pos: dict):
        """Итог по закрытой позиции -> статистика кошелька и авто-пауза плохих кошельков."""
        target = pos.get("target")
        if not target:
            return
        w, res, r = self.state.wallet(target), pos["pnl_acc"], self.cfg["risk"]
        w["realized"] += res
        w["trades"] += 1
        if res > 0:
            w["wins"] += 1
            w["streak"] = 0
        else:
            w["losses"] += 1
            w["streak"] += 1
        if w["paused"]:
            return
        why = None
        if r["wallet_max_loss_sol"] and -w["realized"] >= r["wallet_max_loss_sol"]:
            why = f"убыток от него достиг {-w['realized']:.3f} SOL"
        elif r["wallet_max_consecutive_losses"] and w["streak"] >= r["wallet_max_consecutive_losses"]:
            why = f"{w['streak']} убыточных сделок подряд"
        if why:
            w["paused"] = True
            log.warning("кошелёк %s поставлен на паузу: %s", self.wname(target), why)
            self.notify(f"⏸ Кошелёк {self.wname(target)} на паузе: {why}.\nВключить: /wallet resume {self.wname(target)}")
