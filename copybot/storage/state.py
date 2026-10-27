"""Состояние бота: позиции, статистика кошельков, флаги паузы/остановки (state.json)."""
import json
import logging
import os
import time

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str, dry_sol: float):
        self.path = path
        self.positions: dict[str, dict] = {}
        self.dry_sol = float(dry_sol)
        self.start_equity: float | None = None
        self.halted = False   # остановка по просадке портфеля
        self.paused = False   # ручная пауза (Telegram /pause)
        self.realized = 0.0
        self.wallets: dict[str, dict] = {}  # статистика по кошелькам-целям
        self.mode: str | None = None        # "dry" | "live": в каком режиме создано состояние
        self.pending: dict[str, dict] = {}  # sig -> покупка, отправленная без подтверждения (могла пройти)
        self._load()

    @staticmethod
    def new_position(amount: int, cost: float, target: str) -> dict:
        return {"amount": amount, "initial": amount, "cost": cost, "target": target,
                "opened": time.time(), "peak": 1.0, "tp_done": [], "pnl_acc": 0.0, "fees": 0.0}

    @staticmethod
    def _norm(p: dict) -> dict:
        p.setdefault("initial", p.get("amount", 0))
        p.setdefault("target", "")
        p.setdefault("opened", time.time())
        p.setdefault("peak", 1.0)
        p.setdefault("pnl_acc", 0.0)
        p.setdefault("fees", 0.0)  # комиссии и аренда токен-аккаунта сверх цены свопа (SOL)
        if not isinstance(p.get("tp_done"), list):  # старый формат (bool)
            p["tp_done"] = []
        return p

    def wallet(self, addr: str) -> dict:
        return self.wallets.setdefault(addr, {"realized": 0.0, "trades": 0, "wins": 0,
                                              "losses": 0, "streak": 0, "paused": False})

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                st = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"файл состояния {self.path} повреждён (строка {e.lineno}): {e.msg}. "
                             "Если открытых позиций нет, просто удалите его; иначе сначала проверьте позиции вручную.") from None
        self.positions = {m: self._norm(p) for m, p in st.get("positions", {}).items()}
        self.dry_sol = st.get("dry_sol", self.dry_sol)
        self.start_equity = st.get("start_equity")
        self.halted = st.get("halted", False)
        self.paused = st.get("paused", False)
        self.realized = st.get("realized", 0.0)
        self.wallets = st.get("wallets", {})
        self.mode = st.get("mode")
        self.pending = st.get("pending", {})
        log.info("состояние загружено: %d позиций, halted=%s, paused=%s",
                 len(self.positions), self.halted, self.paused)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"positions": self.positions, "dry_sol": self.dry_sol,
                       "start_equity": self.start_equity, "halted": self.halted,
                       "paused": self.paused, "realized": self.realized,
                       "wallets": self.wallets, "mode": self.mode,
                       "pending": self.pending}, f, indent=1)
        os.replace(tmp, self.path)  # атомарно: файл не повредится при сбое

    def cost_total(self) -> float:
        return sum(p["cost"] for p in self.positions.values())
