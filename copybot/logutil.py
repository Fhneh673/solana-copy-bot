"""Маскирование секретов в логах (токен Telegram, ключи RPC/Jupiter, приватный ключ)."""
import logging
import os
import re

_SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "PRIVATE_KEY", "JUPITER_API_KEY")
_URL_KEY = re.compile(r"(api[-_]?key=)[^&\s\"']+", re.I)
_TG_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s]+")


_RUNTIME_SECRETS: set[str] = set()


def register_secret(value) -> None:
    """Запоминает значение (ссылка RPC с ключом, ключ из config.json), которое нельзя показывать в выводе."""
    if isinstance(value, str) and len(value) >= 8:
        _RUNTIME_SECRETS.add(value)


def redact(text: str, extra=()) -> str:
    for name in _SECRET_ENV:
        v = os.environ.get(name, "")
        if len(v) >= 8:
            text = text.replace(v, "***")
    for v in sorted(_RUNTIME_SECRETS, key=len, reverse=True):   # длинные первыми: ссылка раньше своего ключа
        text = text.replace(v, "***")
    for v in extra:
        if v and len(v) >= 8:
            text = text.replace(v, "***")
    text = _TG_URL.sub(r"\1***", text)
    return _URL_KEY.sub(r"\1***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True
