class NullNotifier:
    """Заглушка, когда Telegram выключен."""

    async def send(self, text: str):
        return None
