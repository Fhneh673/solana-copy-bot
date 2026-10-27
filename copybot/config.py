"""Загрузка config.json, значения по умолчанию, проверка, описание простыми словами."""
import difflib
import json
import os
import re

ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

DEFAULTS = {
    "dry_run": True,
    "dry_run_balance_sol": 10.0,
    "jupiter_url": "",           # пусто = выбрать автоматически: api.jup.ag при наличии ключа, иначе lite-api
    "jupiter_api_key": "",       # бесплатный ключ с portal.jup.ag (или переменная JUPITER_API_KEY)
    "adopt_unknown_tokens": True,  # при старте брать под защиту токены на кошельке, которых нет в учёте
    "min_target_sol": 0.5,
    "slippage_bps": 300,
    "max_price_impact_pct": 3,
    "max_priority_fee_lamports": 200000,
    "one_position_per_mint": True,
    "sell_follow": "owner",  # owner: продаём вслед только тому, кто открыл позицию; any: любому
    "blacklist": [],
    "whitelist_mints": [],   # токены, для которых фильтры не применяются
    "state_file": "state.json",
    "journal_file": "trades.csv",
    "engine": "auto",  # auto | rust | python
    "rust_binary": "",
    "send_rpcs": [],
    "confirm_timeout_sec": 30,
    "warmup_sec": 20,
}
SIZING_DEFAULTS = {"mode": "ratio", "ratio": 0.1, "fixed_sol": 0.1, "percent": 2.0}
RISK_DEFAULTS = {
    "max_sol_per_trade": 0.2,
    "max_position_pct": 20,
    "max_open_positions": 10,
    "min_trade_sol": 0.01,
    "min_sol_reserve": 0.02,
    "stop_loss_pct": 30,
    "take_profit_pct": 100,
    "tp_sell_fraction": 0.5,
    "take_profit_levels": [],     # лесенка: [{"pnl_pct": 50, "sell_pct": 30}, ...] (sell_pct — от размера входа)
    "trailing_stop_pct": 0,       # 0 = выключено; продать, если цена откатила на N% от максимума
    "trailing_activate_pct": 20,  # трейлинг включается после роста на N%
    "max_hold_minutes": 0,        # 0 = выключено; закрыть позицию, если держим дольше
    "wallet_max_loss_sol": 0,     # 0 = выключено; пауза кошелька, если он принёс нам убыток >= N SOL
    "wallet_max_consecutive_losses": 0,  # 0 = выключено; пауза после N убыточных сделок подряд
    "max_drawdown_pct": 25,
    "on_drawdown": "stop",  # stop | close_all
    "check_interval_sec": 10,
    "no_route_cycles": 3,         # сколько проверок подряд без маршрута продажи -> оцениваем токен в 0
}
FILTER_DEFAULTS = {
    "enabled": True,
    "block_mint_authority": True,
    "block_freeze_authority": True,
    "require_sell_route": True,
    "max_roundtrip_loss_pct": 35,  # 0 = выключено
    "max_price_deviation_pct": 20,  # 0 = выключено; не покупать, если цена хуже цены цели на N%
    "cache_sec": 600,
}
LITE_URL = "https://lite-api.jup.ag/swap/v1"
API_URL = "https://api.jup.ag/swap/v1"
TELEGRAM_DEFAULTS = {"enabled": False, "notify_skips": False, "allowed_user_ids": []}
TARGET_KEYS = {"address", "name", "enabled", "mode", "ratio", "fixed_sol", "percent", "max_sol_per_trade"}
SECTION_KEYS = {"rpc_http", "rpc_ws", "targets", "sizing", "risk", "filters", "jito", "telegram"}
# допустимые диапазоны числовых настроек: (мин, макс); None = без границы
RANGES = {
    "slippage_bps": (0, 10000), "max_price_impact_pct": (0, 100), "min_target_sol": (0, None),
    "max_priority_fee_lamports": (0, None), "confirm_timeout_sec": (1, 600), "warmup_sec": (0, None),
    "dry_run_balance_sol": (0, None), "ratio": (0, None), "fixed_sol": (0, None), "percent": (0, 100),
    "max_sol_per_trade": (0, None), "max_position_pct": (0, 100), "max_open_positions": (0, None),
    "min_trade_sol": (0, None), "min_sol_reserve": (0, None), "stop_loss_pct": (0, 100),
    "take_profit_pct": (0, None), "tp_sell_fraction": (0, 1), "trailing_stop_pct": (0, 100),
    "trailing_activate_pct": (0, None), "max_hold_minutes": (0, None), "wallet_max_loss_sol": (0, None),
    "wallet_max_consecutive_losses": (0, None), "max_drawdown_pct": (0, 100), "check_interval_sec": (1, None),
    "no_route_cycles": (1, None), "max_roundtrip_loss_pct": (0, 100), "max_price_deviation_pct": (0, 100),
    "cache_sec": (0, None), "tip_lamports": (0, None),
}
JITO_DEFAULTS = {"enabled": False, "tip_lamports": 100000,
                 "url": "https://mainnet.block-engine.jito.wtf/api/v1/transactions"}


def load_dotenv(path: str = ".env"):
    """Читает KEY=VALUE из .env (без сторонних библиотек)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def derive_ws(http_url: str) -> str:
    return re.sub(r"^http", "ws", http_url, count=1)  # https->wss, http->ws


def _check_section(label: str, raw: dict, defaults: dict, extra=()):
    """Неизвестные ключи (опечатки) и значения не того типа/вне диапазона — ошибка, а не молчаливое игнорирование."""
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: ожидается объект {{...}}, а не {type(raw).__name__}")
    known = set(defaults) | set(extra)
    for k, v in raw.items():
        where = f"{label}.{k}" if label else k
        if k not in known:
            hint = difflib.get_close_matches(k, known, n=1)
            raise ValueError(f"неизвестная настройка «{where}»" + (f" — возможно, имелось в виду «{hint[0]}»" if hint else "")
                             + ". Проверьте опечатки в config.json.")
        if k not in defaults:
            continue
        d = defaults[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"«{where}» должно быть true или false, а не {v!r}")
        elif isinstance(d, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"«{where}» должно быть числом, а не {v!r}")
            lo, hi = RANGES.get(k, (None, None))
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ValueError(f"«{where}» = {v}: допустимо от {lo if lo is not None else '-∞'} "
                                 f"до {hi if hi is not None else '∞'}")
        elif isinstance(d, str):
            if not isinstance(v, str):
                raise ValueError(f"«{where}» должно быть строкой, а не {v!r}")
        elif isinstance(d, list) and not isinstance(v, list):
            raise ValueError(f"«{where}» должно быть списком [...], а не {v!r}")


def _check_target(addr: str, o: dict):
    """Свои настройки кошелька проверяем так же строго, как общие: иначе ошибка вылезла бы уже во время сделки."""
    who = f"кошелёк {addr[:8]}.."
    for k in ("ratio", "fixed_sol", "percent", "max_sol_per_trade"):
        if k not in o:
            continue
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{who}: «{k}» должно быть числом, а не {v!r}")
        lo, hi = RANGES[k]
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"{who}: «{k}» = {v}: допустимо от {lo if lo is not None else '-∞'} "
                             f"до {hi if hi is not None else '∞'}")
    if "mode" in o and o["mode"] not in ("ratio", "fixed", "percent_balance"):
        raise ValueError(f"{who}: mode должен быть: ratio, fixed или percent_balance")
    if "enabled" in o and not isinstance(o["enabled"], bool):
        raise ValueError(f"{who}: «enabled» должно быть true или false, а не {o['enabled']!r}")
    if "name" in o and not isinstance(o["name"], str):
        raise ValueError(f"{who}: «name» должно быть строкой")


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"файл {path} повреждён (строка {e.lineno}): {e.msg}. "
                         "Проще всего запустить настройку заново.") from None
    if not isinstance(raw, dict):
        raise ValueError(f"файл {path}: ожидается объект {{...}}")
    _check_section("", raw, DEFAULTS, SECTION_KEYS)
    for name, defaults in (("sizing", SIZING_DEFAULTS), ("risk", RISK_DEFAULTS), ("filters", FILTER_DEFAULTS),
                           ("jito", JITO_DEFAULTS), ("telegram", TELEGRAM_DEFAULTS)):
        if name in raw:
            _check_section(name, raw[name], defaults)
    for key in ("rpc_http", "rpc_ws"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"«{key}» должно быть строкой-ссылкой")
    cfg = {**DEFAULTS, **raw}
    cfg["sizing"] = {**SIZING_DEFAULTS, **cfg.get("sizing", {})}
    cfg["risk"] = {**RISK_DEFAULTS, **cfg.get("risk", {})}
    cfg["filters"] = {**FILTER_DEFAULTS, **cfg.get("filters", {})}
    cfg["jito"] = {**JITO_DEFAULTS, **cfg.get("jito", {})}
    cfg["telegram"] = {**TELEGRAM_DEFAULTS, **cfg.get("telegram", {})}
    cfg["jupiter_api_key"] = (cfg.get("jupiter_api_key") or os.environ.get("JUPITER_API_KEY", "")).strip()
    if not cfg["jupiter_url"]:   # api.jup.ag требует ключ; lite-api работает без него, но Jupiter его выводит из эксплуатации
        cfg["jupiter_url"] = API_URL if cfg["jupiter_api_key"] else LITE_URL

    if not cfg.get("rpc_http") or "YOUR_KEY" in cfg["rpc_http"]:
        raise ValueError("не указана ссылка RPC (rpc_http). Запустите настройку заново.")
    if not cfg.get("rpc_ws") or "YOUR_KEY" in cfg["rpc_ws"]:
        cfg["rpc_ws"] = derive_ws(cfg["rpc_http"])

    # кошельки: строка-адрес ИЛИ объект {"address":..., "name":..., "ratio":..., "enabled":...}
    addrs, opts = [], {}
    for t in cfg.get("targets") or []:
        if not isinstance(t, (str, dict)):
            raise ValueError(f"элемент targets должен быть адресом или объектом, а не {t!r}")
        if isinstance(t, dict):
            bad = set(t) - TARGET_KEYS
            if bad:
                raise ValueError(f"неизвестные настройки кошелька {t.get('address', '?')[:8]}: {', '.join(sorted(bad))}")
        addr, o = (t, {}) if isinstance(t, str) else (t.get("address", ""), {k: v for k, v in t.items() if k != "address"})
        if not ADDR_RE.match(addr) or addr.startswith("WALLET_ADDRESS"):
            raise ValueError(f"адрес кошелька выглядит неправильно: «{addr}». "
                             "Это должна быть строка из 32–44 букв и цифр.")
        _check_target(addr, o)
        addrs.append(addr)
        opts[addr] = o
    if not addrs:
        raise ValueError("не указано ни одного кошелька для копирования (targets).")
    cfg["targets"], cfg["target_opts"] = addrs, opts

    r = cfg["risk"]
    if not r["take_profit_levels"] and r["take_profit_pct"]:
        r["take_profit_levels"] = [{"pnl_pct": r["take_profit_pct"], "sell_pct": r["tp_sell_fraction"] * 100}]

    for tl in r["take_profit_levels"]:
        if not (isinstance(tl, dict) and isinstance(tl.get("pnl_pct"), (int, float)) and tl["pnl_pct"] > 0
                and isinstance(tl.get("sell_pct"), (int, float)) and 0 < tl["sell_pct"] <= 100):
            raise ValueError('risk.take_profit_levels: каждый уровень — {"pnl_pct": число > 0, "sell_pct": от 0 до 100}')
    if cfg["engine"] not in ("auto", "rust", "python"):
        raise ValueError("engine должен быть: auto, rust или python")
    if cfg["sell_follow"] not in ("owner", "any"):
        raise ValueError("sell_follow должен быть: owner или any")
    if cfg["sizing"]["mode"] not in ("ratio", "fixed", "percent_balance"):
        raise ValueError("sizing.mode должен быть: ratio, fixed или percent_balance")
    if r["on_drawdown"] not in ("stop", "close_all"):
        raise ValueError("risk.on_drawdown должен быть: stop или close_all")
    return cfg


def describe(cfg: dict) -> str:
    """Что бот собирается делать — человеческим языком."""
    s, r, f = cfg["sizing"], cfg["risk"], cfg["filters"]
    mode = ("ТЕСТ — реальные деньги НЕ тратятся, сделки только имитируются"
            if cfg["dry_run"] else "РЕАЛЬНЫЙ — будут тратиться настоящие SOL!")
    if s["mode"] == "ratio":
        how = f"{s['ratio'] * 100:g}% от суммы, которую вложил он"
    elif s["mode"] == "fixed":
        how = f"всегда {s['fixed_sol']:g} SOL"
    else:
        how = f"{s['percent']:g}% от моего свободного баланса"

    def onoff(v, text, label):
        return text.format(v) if v else f"{label}: выключено"

    names = [cfg["target_opts"][a].get("name") or a[:6] + ".." for a in cfg["targets"]]
    lines = [
        f"  Режим: {mode}",
        f"  Слежу за кошельками ({len(names)}): {', '.join(names)}",
        f"  Когда кошелёк ПОКУПАЕТ токен, я покупаю на: {how}",
        "    …но " + (f"не больше {r['max_sol_per_trade']:g} SOL за раз" if r["max_sol_per_trade"]
                     else "без лимита на сумму за раз") + " и " + (
            f"не больше {r['max_position_pct']:g}% капитала в одной монете" if r["max_position_pct"]
            else "без лимита на долю капитала в монете"),
        "  Когда он ПРОДАЁТ — я продаю такую же долю своей позиции",
        "  " + onoff(r["stop_loss_pct"], "Стоп-лосс: продаю всё, если позиция упала на {:g}%", "Стоп-лосс"),
    ]
    if r["take_profit_levels"]:
        lv = ", ".join(f"+{x['pnl_pct']:g}% → {x['sell_pct']:g}%" for x in r["take_profit_levels"])
        lines.append(f"  Тейк-профит (при росте → продать долю входа): {lv}")
    else:
        lines.append("  Тейк-профит: выключено")
    if r["trailing_stop_pct"]:
        lines.append(f"  Трейлинг-стоп: продаю при откате на {r['trailing_stop_pct']:g}% от максимума "
                     f"(после роста на {r['trailing_activate_pct']:g}%)")
    if r["max_hold_minutes"]:
        lines.append(f"  Максимальное время удержания: {r['max_hold_minutes']:g} мин")
    lines.append("  " + onoff(r["max_drawdown_pct"], "Защита портфеля: при общей потере {:g}% перестаю покупать", "Защита портфеля"))
    if r["wallet_max_loss_sol"] or r["wallet_max_consecutive_losses"]:
        lines.append("  Кошельки с плохой статистикой ставлю на паузу автоматически")
    if f["enabled"]:
        lines.append("  Фильтры токенов: включены (права токена, возможность продажи, цена против цены цели)")
    else:
        lines.append("  Фильтры токенов: ВЫКЛЮЧЕНЫ")
    if cfg["telegram"]["enabled"]:
        lines.append("  Telegram: уведомления и команды включены")
    return "\n".join(lines)
