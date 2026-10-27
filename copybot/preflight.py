"""Проверка перед стартом: всё ли подключается. Печатает понятные ✓ / ✗."""
import asyncio

from .constants import WSOL
from .logutil import redact

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def short(e: Exception) -> str:
    """Текст ошибки для консоли: сначала маскируем ключи (в тексте aiohttp бывает полный URL), потом обрезаем."""
    return redact(str(e))[:80]


async def preflight(cfg, session, rpc, engine, my_wallet: str, tg=None) -> bool:
    ok = True

    def line(good: bool, text: str, hint: str = ""):
        nonlocal ok
        print(("  ✓ " if good else "  ✗ ") + text)
        if not good:
            ok = False
            if hint:
                print("      → " + hint)

    print("Проверяю подключения…")
    try:
        await asyncio.wait_for(rpc.call("getSlot", []), 10)
        line(True, "RPC отвечает")
    except Exception as e:  # noqa: BLE001
        line(False, f"RPC не отвечает ({short(e)})",
             "проверьте ссылку и ключ RPC (или интернет) и запустите настройку заново")

    try:
        ws = await session.ws_connect(cfg["rpc_ws"], timeout=10)
        await ws.close()
        line(True, "WebSocket работает (слежение за кошельками)")
    except Exception as e:  # noqa: BLE001
        line(False, f"WebSocket не подключился ({short(e)})",
             "публичные RPC часто блокируют слежение — возьмите бесплатный ключ на helius.dev")

    try:
        q = await asyncio.wait_for(engine.quote(WSOL, USDC, 10_000_000), 10)
        line(True, f"Jupiter даёт котировки (0.01 SOL ≈ {int(q['outAmount']) / 1e6:.2f} USDC)")
    except Exception as e:  # noqa: BLE001
        line(False, f"Jupiter недоступен ({short(e)})",
             "проверьте интернет; для api.jup.ag нужен бесплатный ключ с portal.jup.ag в JUPITER_API_KEY")
    if "lite-api" in cfg["jupiter_url"]:
        print("  ⚠ используется lite-api.jup.ag: Jupiter объявил о его отключении (срок перенесён). "
              "Получите бесплатный ключ на portal.jup.ag и укажите JUPITER_API_KEY в .env")

    if tg:
        try:
            line(True, f"Telegram: бот @{await asyncio.wait_for(tg.check(), 10)}")
        except Exception as e:  # noqa: BLE001
            line(False, f"Telegram не работает ({short(e)})", "проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в файле .env")

    if not cfg["dry_run"]:
        try:
            bal = await rpc.sol_balance(my_wallet)
            line(bal >= 0.05, f"баланс кошелька бота: {bal:.4f} SOL",
                 "пополните кошелёк (нужно хотя бы 0.05 SOL, лучше больше)")
        except Exception as e:  # noqa: BLE001
            line(False, f"не удалось получить баланс ({short(e)})")
    return ok
