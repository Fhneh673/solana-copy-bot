"""Запуск бота.   python main.py            — обычный запуск
                  python main.py --check    — только проверить подключения"""
import argparse
import asyncio
import logging
import os
import sys

import aiohttp

from copybot.chain.rpc import Rpc
from copybot.chain.wallet import load_keypair
from copybot.config import describe, load_config, load_dotenv
from copybot.control import Controller
from copybot.engine import create_engine
from copybot.engine.python_engine import PythonEngine
from copybot.logutil import RedactFilter, redact, register_secret
from copybot.notify.telegram import Telegram
from copybot.preflight import preflight
from copybot.risk.filters import TokenFilters
from copybot.risk.monitor import RiskMonitor
from copybot.storage.journal import Journal
from copybot.storage.state import State
from copybot.trader import Trader

BANNER = """
╔══════════════════════════════════════════╗
║        Solana Copy Bot — копитрейдинг     ║
╚══════════════════════════════════════════╝"""


async def supervise(engine, trader, cfg, session, rpc, kp):
    """Запускает движок. Если ядро Rust упало (в режиме auto) — переключается на Python."""
    try:
        await engine.run(trader)
    except Exception as e:  # noqa: BLE001
        if engine.name != "rust" or cfg["engine"] == "rust":
            raise
        print(f"\n⚠ Ядро Rust остановилось ({redact(str(e))}). Переключаюсь на Python-движок.")
        py = PythonEngine(cfg, session, rpc, kp)
        trader.engine = py
        await py.run(trader)


async def amain(args) -> int:
    load_dotenv()
    cfg = load_config(args.config)
    for secret in (cfg["rpc_http"], cfg["rpc_ws"], cfg["jupiter_api_key"], *cfg["send_rpcs"]):
        register_secret(secret)
    print(BANNER)
    print("Что будет делать бот:")
    print(describe(cfg))
    print()

    kp = None if cfg["dry_run"] else load_keypair()
    me = str(kp.pubkey()) if kp else "dry-run"

    async with aiohttp.ClientSession() as session:
        rpc = Rpc(session, cfg["rpc_http"])
        engine, note = await create_engine(cfg, session, rpc, kp)
        try:
            if engine.name == "rust":
                print("Движок: Rust ⚡ (быстрое ядро)")
            else:
                print("Движок: Python")
                if note:
                    print(f"  ℹ {note}")
            if kp and engine.name == "rust" and engine.pubkey and engine.pubkey != me:
                raise ValueError("ядро Rust и Python используют разные ключи — проверьте PRIVATE_KEY")
            print()

            tg = None
            if cfg["telegram"]["enabled"]:
                token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
                if not token or not chat:
                    raise ValueError("Telegram включён, но в .env нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. "
                                     "Запустите настройку заново.")
                allowed = list(cfg["telegram"].get("allowed_user_ids") or []) + \
                    [u for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()]
                tg = Telegram(token, chat, session, allowed)

            if not await preflight(cfg, session, rpc, engine, me, tg):
                print("\nЕсть проблемы (см. ✗ выше). Исправьте и запустите снова.")
                return 1
            if args.check:
                print("\nВсё готово к работе.")
                return 0

            if not cfg["dry_run"] and not args.yes:
                print("\n⚠ РЕАЛЬНЫЙ режим: бот будет тратить настоящие деньги.")
                if input("Напишите ДА, чтобы продолжить: ").strip().lower() not in ("да", "yes"):
                    print("Отменено.")
                    return 0

            state = State(cfg["state_file"], cfg["dry_run_balance_sol"])
            trader = Trader(cfg, rpc, engine, state, me, notifier=tg,
                            journal=Journal(cfg["journal_file"], cfg["dry_run"]),
                            filters=TokenFilters(cfg, rpc))
            await trader.init()
            monitor = RiskMonitor(trader, cfg)
            jobs = [supervise(engine, trader, cfg, session, rpc, kp), monitor.run()]
            if tg:
                jobs.append(tg.poll(Controller(trader, monitor)))
                await tg.send(f"🤖 Бот запущен ({'ТЕСТ' if cfg['dry_run'] else 'РЕАЛЬНЫЙ режим'}, "
                              f"движок {engine.name}). Команды: /help")
            print("\nБот запущен и ждёт сделок. Остановить: Ctrl+C.\n")
            await asyncio.gather(*jobs)
        finally:
            await engine.stop()
    return 0


def run():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    p = argparse.ArgumentParser()
    p.add_argument("config", nargs="?", default="config.json")
    p.add_argument("--check", action="store_true", help="только проверить подключения")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение в реальном режиме")
    args = p.parse_args()

    if not os.path.exists(args.config):
        print(f"Не найден файл настроек «{args.config}».\n"
              "Запустите мастер настройки:  python setup_wizard.py  (или start.bat / start.sh)")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactFilter())   # токены и ключи не попадают в лог, даже внутри текста исключений
    try:
        sys.exit(asyncio.run(amain(args)))
    except ValueError as e:
        print(f"\nОшибка: {redact(str(e))}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено. Открытые позиции сохранены в state.json.")


if __name__ == "__main__":
    run()
