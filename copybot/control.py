"""Команды управления ботом (используются Telegram-интерфейсом)."""

HELP = """Команды:
/status — состояние бота
/positions — открытые позиции
/wallets — статистика по кошелькам
/stats — общий результат
/pause — не открывать новые позиции
/resume — снять паузу (после остановки по просадке: /resume ДА)
/wallet pause <имя> — поставить кошелёк на паузу
/wallet resume <имя> — включить кошелёк
/rebase — считать текущий капитал стартовым (после пополнения/вывода)
/close <начало адреса токена> — закрыть позицию
/closeall ДА — закрыть все позиции"""


class Controller:
    def __init__(self, trader, monitor):
        self.t, self.m = trader, monitor

    async def handle(self, cmd: str, args: list[str]) -> str:
        cmd = cmd.lower().lstrip("/").split("@")[0]
        fn = {"start": self._help, "help": self._help, "status": self._status, "positions": self._positions,
              "wallets": self._wallets, "stats": self._stats, "pause": self._pause, "resume": self._resume,
              "wallet": self._wallet, "rebase": self._rebase, "close": self._close, "closeall": self._closeall}.get(cmd)
        if not fn:
            return "Не знаю такую команду. /help — список команд."
        return await fn(args)

    # ---- команды
    async def _help(self, args):
        return HELP

    async def _status(self, args):
        t, st = self.t, self.t.state
        eq = self.m.last_equity
        if eq is None:
            eq = await t.sol_balance() + st.cost_total()
        dd = (1 - eq / st.start_equity) * 100 if st.start_equity else 0
        flags = [x for x, on in (("ОСТАНОВЛЕН ПО ПРОСАДКЕ", st.halted), ("НА ПАУЗЕ", st.paused)) if on]
        return (f"Режим: {'ТЕСТ' if t.dry else 'РЕАЛЬНЫЙ'} | движок: {t.engine.name}\n"
                f"Капитал: {eq:.4f} SOL (старт {st.start_equity:.4f}, просадка {dd:.1f}%)\n"
                f"Позиций: {len(st.positions)} | зафиксировано: {st.realized:+.4f} SOL\n"
                f"Статус: {', '.join(flags) if flags else 'работает'}")

    async def _positions(self, args):
        t, st = self.t, self.t.state
        if not st.positions:
            return "Открытых позиций нет."
        rows = []
        for mint, p in st.positions.items():
            val = self.m.last_values.get(mint)
            pnl = f"{(val / p['cost'] - 1) * 100:+.1f}%" if val and p["cost"] > 0 else "—"
            rows.append(f"{mint[:8]}..  вложено {p['cost']:.4f} SOL  {pnl}  [{t.wname(p['target'])}]")
        return "Позиции:\n" + "\n".join(rows)

    async def _wallets(self, args):
        t = self.t
        rows = []
        for addr in t.cfg["targets"]:
            w = t.state.wallet(addr)
            rate = f"{w['wins'] / w['trades'] * 100:.0f}%" if w["trades"] else "—"
            rows.append(f"{t.wname(addr)}: сделок {w['trades']}, прибыльных {rate}, "
                        f"итог {w['realized']:+.4f} SOL{' ⏸ПАУЗА' if w['paused'] else ''}")
        return "Кошельки:\n" + "\n".join(rows)

    async def _stats(self, args):
        t, st = self.t, self.t.state
        trades = sum(w["trades"] for w in st.wallets.values())
        wins = sum(w["wins"] for w in st.wallets.values())
        rate = f"{wins / trades * 100:.0f}%" if trades else "—"
        best = max(st.wallets.items(), key=lambda kv: kv[1]["realized"], default=None)
        worst = min(st.wallets.items(), key=lambda kv: kv[1]["realized"], default=None)
        txt = f"Закрыто сделок: {trades}, прибыльных: {rate}\nИтог: {st.realized:+.4f} SOL"
        if best and trades:
            txt += (f"\nЛучший кошелёк: {t.wname(best[0])} ({best[1]['realized']:+.4f})"
                    f"\nХудший: {t.wname(worst[0])} ({worst[1]['realized']:+.4f})")
        return txt

    async def _pause(self, args):
        self.t.state.paused = True
        self.t.state.save()
        return "⏸ Пауза: новые позиции не открываются (открытые позиции продолжают сопровождаться)."

    async def _resume(self, args):
        st = self.t.state
        st.paused = False
        if st.halted:
            if args and args[0].lower() in ("да", "yes"):
                st.halted = False
                st.start_equity = None  # отсчёт просадки начинаем заново
                await self.t.init()
                st.save()
                return "▶ Возобновлено. Остановка по просадке снята, отсчёт просадки начат заново."
            st.save()
            return "Пауза снята, но бот остановлен по просадке. Чтобы возобновить покупки: /resume ДА"
        st.save()
        return "▶ Возобновлено."

    def _find_wallet(self, q: str):
        q = q.lower()
        for addr in self.t.cfg["targets"]:
            if addr.lower().startswith(q) or self.t.wname(addr).lower().rstrip(".") == q.rstrip("."):
                return addr
        return None

    async def _wallet(self, args):
        if len(args) < 2 or args[0] not in ("pause", "resume"):
            return "Использование: /wallet pause <имя> или /wallet resume <имя>"
        addr = self._find_wallet(args[1])
        if not addr:
            return "Такой кошелёк не найден. Список: /wallets"
        w = self.t.state.wallet(addr)
        w["paused"] = args[0] == "pause"
        if not w["paused"]:
            w["streak"] = 0
        self.t.state.save()
        return f"Кошелёк {self.t.wname(addr)}: {'на паузе ⏸' if w['paused'] else 'включён ▶'}"

    async def _close(self, args):
        if not args:
            return "Использование: /close <начало адреса токена>"
        st = self.t.state
        hits = [m for m in st.positions if m.lower().startswith(args[0].lower())]
        if len(hits) != 1:
            return "Позиция не найдена." if not hits else "Подходит несколько позиций, уточните адрес."
        try:
            await self.t.close(hits[0], 1.0, "ручное закрытие")
        except Exception as e:  # noqa: BLE001
            return f"Не удалось закрыть {hits[0][:8]}..: {e}"
        return f"Закрываю {hits[0][:8]}.."

    async def _closeall(self, args):
        if not args or args[0].lower() not in ("да", "yes"):
            return "Это закроет ВСЕ позиции. Подтвердите: /closeall ДА"
        n, errors = 0, []
        for mint in list(self.t.state.positions):   # ошибка по одной позиции не мешает закрыть остальные
            try:
                await self.t.close(mint, 1.0, "ручное закрытие всех")
                n += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{mint[:8]}..: {str(e)[:120]}")
        txt = f"Закрыто позиций: {n}"
        return txt + (f"\nНе закрылись ({len(errors)}):\n" + "\n".join(errors) if errors else "")

    async def _rebase(self, args):
        t, st = self.t, self.t.state
        st.start_equity = await t.sol_balance() + st.cost_total()
        st.save()
        return f"Стартовый капитал сброшен: {st.start_equity:.4f} SOL (просадка отсчитывается заново)."
