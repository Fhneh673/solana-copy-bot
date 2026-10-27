"""Тест Telegram-интерфейса на подставной сессии:  python tests/test_telegram.py"""
import asyncio

from _helpers import B, Controller, buy, make
from copybot.notify.telegram import Telegram


class Resp:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        if self.data is None:
            await asyncio.sleep(3600)  # имитация долгого опроса
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.sent, self.polls = [], 0

    def post(self, url, json=None):
        method = url.rsplit("/", 1)[1]
        if method == "sendMessage":
            self.sent.append(json)
            return Resp({"ok": True})
        if method == "getMe":
            return Resp({"ok": True, "result": {"username": "testbot"}})
        self.polls += 1
        if self.polls == 1:   # накопившиеся до запуска сообщения: НЕ должны выполниться
            return Resp({"ok": True, "result": [{"update_id": 5, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/closeall ДА"}}]})
        if self.polls == 2:
            return Resp({"ok": True, "result": [
                {"update_id": 6, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/status"}},
                {"update_id": 7, "message": {"chat": {"id": 999}, "from": {"id": 1}, "text": "/closeall ДА"}},  # чужой чат
                {"update_id": 8, "message": {"chat": {"id": 1}, "from": {"id": 42}, "text": "/closeall ДА"}}]})  # чужой человек в чате
        return Resp(None)


async def scenario():
    tr, mon, _, _ = make()
    await tr.init()
    await tr.copy(B, buy("M1"))
    s = FakeSession()
    tg = Telegram("TOKEN", "1", s)
    assert await tg.check() == "testbot"
    task = asyncio.create_task(tg.poll(Controller(tr, mon)))
    await asyncio.sleep(0.3)
    task.cancel()
    assert "M1" in tr.state.positions, "старые и чужие команды не должны выполняться"
    texts = [m["text"] for m in s.sent]
    assert len(texts) == 1 and texts[0].startswith("Режим:") and s.sent[0]["chat_id"] == "1", texts
    print("OK: Telegram — свой чат отвечает, чужой чат/человек и старые команды игнорируются")


class LeakySession:
    """Имитация aiohttp: текст исключения содержит полный URL с токеном."""
    def post(self, url, json=None):
        raise RuntimeError(f"Cannot connect to host {url}")


async def redaction():
    tg = Telegram("123456:SECRET-TOKEN", "1", LeakySession())
    try:
        await tg.check()
        raise SystemExit("ожидалась ошибка")
    except RuntimeError as e:
        assert "SECRET-TOKEN" not in str(e), e


def test_commands_only_from_allowed_users():
    asyncio.run(scenario())


def test_token_not_leaked_in_errors():
    asyncio.run(redaction())


def test_group_chat_needs_explicit_allowlist():
    assert Telegram("t", "-100500", None).allowed == set()
    assert Telegram("t", "-100500", None, [7]).allowed == {"7"}
    assert Telegram("t", "55", None).allowed == {"55"}


def test_error_texts_with_rpc_url_are_masked_in_telegram():
    """Исключения aiohttp / reqwest содержат полный URL RPC; он не должен попасть в чат."""
    from copybot.logutil import register_secret
    url = "https://rpc.example.test/v2/PATHSECRET123456"
    register_secret(url)
    s = FakeSession()
    tg = Telegram("123456:SECRET-TOKEN", "1", s)
    asyncio.run(tg.send(f"не удалось: error sending request for url ({url}) и https://x.test/?api-key=ABCDEF123456"))
    text = s.sent[0]["text"]
    assert "PATHSECRET" not in text and "ABCDEF123456" not in text and "***" in text, text


if __name__ == "__main__":
    test_commands_only_from_allowed_users()
    test_token_not_leaked_in_errors()
    test_group_chat_needs_explicit_allowlist()
    test_error_texts_with_rpc_url_are_masked_in_telegram()
    print("OK: Telegram — токен не утекает, группа требует allowlist")
