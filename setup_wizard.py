"""Мастер настройки: задаёт вопросы по-русски и создаёт config.json (и .env с ключами)."""
import getpass
import json
import os
import sys
import urllib.request

from copybot.config import ADDR_RE, derive_ws

CONFIG, ENV, STATE = "config.json", ".env", "state.json"
STEPS = 7


def ask(prompt, default=None):
    suffix = f"  [Enter = {default}]" if default not in (None, "") else ""
    v = input(f"{prompt}{suffix}\n> ").strip()
    return v if v else ("" if default is None else str(default))


def ask_num(prompt, default, lo=0.0, hi=None):
    while True:
        raw = ask(prompt, default).replace(",", ".")
        try:
            v = float(raw)
            if v < lo or (hi is not None and v > hi):
                raise ValueError
            return v
        except ValueError:
            rng = f"от {lo:g}" + (f" до {hi:g}" if hi is not None else "")
            print(f"  Введите число ({rng}).")


def yes(prompt, default="нет"):
    return ask(prompt + " (да/нет)", default).lower() in ("да", "д", "y", "yes")


def head(n, title):
    print(f"\n━━━ Шаг {n} из {STEPS}: {title} ━━━")


def set_env(key, value):
    """Записывает KEY=VALUE в .env, не трогая остальные строки."""
    lines = []
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            lines = f.read().splitlines()
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass


def has_env(key):
    if not os.path.exists(ENV):
        return False
    with open(ENV, encoding="utf-8") as f:
        return any(l.startswith(key + "=") for l in f)


def detect_chat_id(token):
    """Находит chat_id по последнему сообщению боту (нужно сначала написать боту любое слово)."""
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        for upd in reversed(data.get("result", [])):
            chat = (upd.get("message") or {}).get("chat")
            if chat:
                return str(chat["id"])
    except Exception:  # noqa: BLE001
        pass
    return None


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print("\n  Мастер настройки Solana Copy Bot")
    print("  Отвечайте на вопросы. Если не уверены — просто жмите Enter (значения по умолчанию безопасные).")

    if os.path.exists(CONFIG) and not yes(f"\nФайл {CONFIG} уже есть. Перезаписать?", "нет"):
        print("Хорошо, ничего не меняю.")
        return 0

    # 1. RPC
    head(1, "Подключение к сети Solana (RPC)")
    print("  Нужна ссылка RPC. Лучше бесплатная от Helius (helius.dev → создать ключ):\n"
          "  https://mainnet.helius-rpc.com/?api-key=ВАШ_КЛЮЧ\n"
          "  Публичная ссылка по умолчанию медленная и часто не годится для слежения.")
    rpc = ask("Ссылка RPC", "https://api.mainnet-beta.solana.com")

    # 2. Кошельки
    head(2, "За кем следим")
    print("  Вставляйте адреса кошельков трейдеров по одному. После адреса можно через пробел\n"
          "  написать имя (например: 7xKX...abc Вася) — так проще читать логи. Пустая строка — закончить.")
    wallets, seen = [], set()
    while True:
        raw = input(f"Кошелёк #{len(wallets) + 1}\n> ").strip()
        if not raw:
            if wallets:
                break
            print("  Нужен хотя бы один адрес.")
            continue
        addr, _, name = raw.partition(" ")
        if not ADDR_RE.match(addr):
            print("  Это не похоже на адрес Solana (32–44 символа: буквы и цифры). Попробуйте ещё раз.")
            continue
        if addr not in seen:
            seen.add(addr)
            wallets.append({"address": addr, "name": name.strip()} if name.strip() else addr)

    # 3. Размер сделки
    head(3, "Сколько копировать")
    print("  1 — долю от его сделки (например 10% от того, что вложил он)\n"
          "  2 — всегда одну и ту же сумму в SOL\n"
          "  3 — процент от моего свободного баланса")
    choice = ""
    while choice not in ("1", "2", "3"):
        choice = ask("Выберите 1, 2 или 3", "1")
    if choice == "1":
        pct = ask_num("Какую долю его сделки копировать, в %? (10 = десятую часть, 100 = 1 в 1)", 10, 0.1)
        sizing = {"mode": "ratio", "ratio": pct / 100}
    elif choice == "2":
        sizing = {"mode": "fixed", "fixed_sol": ask_num("Сколько SOL на каждую покупку?", 0.1, 0.001)}
    else:
        sizing = {"mode": "percent_balance",
                  "percent": ask_num("Сколько % от моего баланса на каждую покупку?", 2, 0.1, 100)}

    # 4. Защита
    head(4, "Защита денег")
    print("  Можно просто нажимать Enter — значения разумные. 0 = выключить пункт.")
    risk = {}
    risk["max_sol_per_trade"] = ask_num("Максимум SOL на одну покупку", 0.2, 0.001)
    risk["max_position_pct"] = ask_num("Максимум % капитала в одной монете", 20, 1, 100)
    risk["stop_loss_pct"] = ask_num("Стоп-лосс: продать всё, если позиция упала на … %", 30, 0, 99)
    risk["take_profit_pct"] = ask_num("Тейк-профит: зафиксировать, если позиция выросла на … %", 100, 0)
    if risk["take_profit_pct"]:
        risk["tp_sell_fraction"] = ask_num("   Какую часть позиции продавать при тейк-профите, в %", 50, 1, 100) / 100
    risk["trailing_stop_pct"] = ask_num("Трейлинг-стоп: продать, если цена откатила от максимума на … %", 0, 0, 99)
    risk["max_drawdown_pct"] = ask_num("Защита портфеля: перестать покупать, если общий убыток достиг … %", 25, 0, 99)
    risk["wallet_max_consecutive_losses"] = int(ask_num(
        "Ставить кошелёк на паузу после … убыточных сделок подряд", 5, 0))

    # 5. Фильтры токенов
    head(5, "Фильтры токенов")
    print("  Перед покупкой бот проверяет токен: нет ли у создателя прав «допечатать» или «заморозить»\n"
          "  токены, можно ли токен продать и не убежала ли цена. Это защищает от части скама,\n"
          "  но добавляет доли секунды к каждой покупке.")
    filters_on = yes("Включить фильтры токенов?", "да")

    # 6. Telegram
    head(6, "Telegram (необязательно)")
    print("  Бот сможет присылать уведомления о сделках и принимать команды (/status, /pause, /closeall…).")
    tg_on = False
    if yes("Подключить Telegram?", "нет"):
        print("  1) В Telegram найдите @BotFather → /newbot → скопируйте токен.\n"
              "  2) Напишите вашему новому боту любое сообщение.")
        token = getpass.getpass("Вставьте токен бота (ввод не отображается): ").strip()
        chat = detect_chat_id(token) if token else None
        if chat:
            print(f"  Нашёл ваш чат: {chat}")
        else:
            chat = ask("Не удалось определить автоматически. Введите ваш chat_id (число; узнать: @userinfobot)")
        if token and chat:
            set_env("TELEGRAM_BOT_TOKEN", token)
            set_env("TELEGRAM_CHAT_ID", chat)
            tg_on = True
        else:
            print("  Telegram пропущен.")

    # 7. Режим
    head(7, "Режим работы")
    print("  1 — ТЕСТ: бот работает, но деньги не тратит (имитация). Рекомендуется начать с этого.\n"
          "  2 — РЕАЛЬНЫЙ: настоящие сделки с вашего кошелька.")
    mode = ""
    while mode not in ("1", "2"):
        mode = ask("Выберите 1 или 2", "1")
    dry = mode == "1"
    virt = 10.0
    if dry:
        virt = ask_num("Сколько SOL «виртуально» дать боту для теста?", 10, 0.1)
    else:
        print("\n  ⚠ Используйте ОТДЕЛЬНЫЙ кошелёк с небольшой суммой, а не основной.\n"
              "  Приватный ключ (base58, как экспортирует Phantom) будет сохранён в файле .env\n"
              "  рядом с ботом. Никому не показывайте этот файл.")
        key = "keep" if has_env("PRIVATE_KEY") and yes("В .env уже есть ключ. Оставить его?", "да") else ""
        while not key:
            key = getpass.getpass("Вставьте приватный ключ (ввод не отображается): ").strip()
            try:
                from solders.keypair import Keypair
                print(f"  Кошелёк бота: {Keypair.from_base58_string(key).pubkey()}")
            except ImportError:
                print("  Библиотека solders не установлена — запустите через start.bat / start.sh.")
                return 1
            except Exception:  # noqa: BLE001
                print("  Ключ не подошёл, проверьте, что скопировали его целиком.")
                key = ""
        if key != "keep":
            set_env("PRIVATE_KEY", key)

    cfg = {"dry_run": dry, "dry_run_balance_sol": virt,
           "rpc_http": rpc, "rpc_ws": derive_ws(rpc),
           "targets": wallets, "sizing": sizing, "risk": risk,
           "filters": {"enabled": filters_on},
           "telegram": {"enabled": tg_on},
           "min_target_sol": 0.5, "slippage_bps": 300, "max_price_impact_pct": 3,
           "max_priority_fee_lamports": 200000, "one_position_per_mint": True,
           "engine": "auto", "blacklist": [], "state_file": STATE}
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    if os.path.exists(STATE) and yes("\nНайден state.json от прошлого запуска (старые позиции и статистика). "
                                     "Начать с чистого листа?", "да"):
        os.remove(STATE)

    print(f"\n✓ Готово! Настройки сохранены в {CONFIG}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка прервана.")
        sys.exit(1)
