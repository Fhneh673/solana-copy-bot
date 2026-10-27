"""Типы ошибок, по которым торговая логика решает, что делать дальше."""


class SkipTrade(RuntimeError):
    """Сделку сознательно не делаем (цена ушла, слишком большой price impact для покупки): это не авария."""


class NoRoute(RuntimeError):
    """У Jupiter нет маршрута для этой пары (токен не продаётся / нет ликвидности)."""


class TxFailed(RuntimeError):
    """Транзакция точно не прошла в сети."""


class ConfirmTimeout(RuntimeError):
    """Транзакция отправлена, но подтверждение не пришло вовремя: она МОГЛА пройти позже.

    sig — подпись транзакции, out — сколько токенов/лампортов ожидалось по котировке."""

    def __init__(self, msg: str, sig: str | None = None, out: int | None = None):
        super().__init__(msg)
        self.sig, self.out = sig, out
