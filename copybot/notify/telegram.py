"""Telegram: уведомления и управление ботом командами. Только свой chat_id; без сторонних библиотек."""
import asyncio
import logging

from ..logutil import redact

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, chat_id: str, session, allowed_users=()):
        self.token, self.chat_id, self.s = token, str(chat_id), session
        # Команды принимаем только от конкретных людей. В личном чате chat_id и есть id человека;
        # в группе нужен явный список (иначе любой участник группы смог бы выполнить /closeall).
        self.allowed = {str(u) for u in allowed_users}
        if not self.chat_id.startswith("-"):
            self.allowed.add(self.chat_id)
        if not self.allowed:
            log.warning("Telegram: чат %s — группа, а allowed_user_ids пуст: команды выключены, "
                        "только уведомления", self.chat_id)

    async def _call(self, method: str, **params) -> dict:
        try:
            async with self.s.post(API.format(token=self.token, method=method), json=params) as r:
                data = await r.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # в тексте исключений aiohttp бывает полный URL с токеном бота
            raise RuntimeError(redact(f"{type(e).__name__}: {e}", (self.token,))) from None
        if not data.get("ok", True):
            raise RuntimeError(data.get("description", "ошибка Telegram"))
        return data

    async def check(self) -> str:
        return (await self._call("getMe"))["result"].get("username", "?")

    async def send(self, text: str):
        try:
            await self._call("sendMessage", chat_id=self.chat_id, text=redact(text, (self.token,))[:4000],
                             disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram: не отправилось (%s)", e)

    async def poll(self, controller):
        """Принимает команды только из своего чата. Старые команды (до запуска) игнорирует."""
        offset = None
        try:  # пропускаем накопившиеся сообщения, чтобы старая «/closeall» не сработала снова
            data = await self._call("getUpdates", offset=-1, timeout=0)
            if data.get("result"):
                offset = data["result"][-1]["update_id"] + 1
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram: %s", redact(str(e), (self.token,)))
        while True:
            try:
                params = {"timeout": 25}
                if offset:
                    params["offset"] = offset
                data = await self._call("getUpdates", **params)
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue  # чужие чаты игнорируем
                    if str((msg.get("from") or {}).get("id")) not in self.allowed:
                        continue  # в нашем чате, но не от нашего человека
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    cmd, *args = text.split()
                    try:
                        reply = await controller.handle(cmd, args)
                    except Exception as e:  # noqa: BLE001
                        reply = f"Ошибка: {e}"
                    await self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram poll: %s", redact(str(e), (self.token,)))
                await asyncio.sleep(5)
