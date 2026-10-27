import asyncio
import logging

import aiohttp

from ..constants import LAMPORTS

log = logging.getLogger(__name__)

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # обычные токены
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")   # Token-2022


HTTP_TIMEOUT_SEC = 15


def request_timeout(sec: float = HTTP_TIMEOUT_SEC) -> dict:
    """Таймаут одного запроса. По умолчанию у aiohttp он 5 минут: зависший запрос заморозил бы стоп-лоссы."""
    ct = getattr(aiohttp, "ClientTimeout", None)
    return {"timeout": ct(total=sec)} if ct else {}


class Rpc:
    def __init__(self, session: aiohttp.ClientSession, url: str):
        self.s, self.url = session, url

    async def call_url(self, url: str, method: str, params: list):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self.s.post(url, json=body, **request_timeout()) as r:
            d = await r.json()
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error']}")
        return d["result"]

    async def call(self, method: str, params: list):
        return await self.call_url(self.url, method, params)

    async def get_tx(self, sig: str, tries: int = 6):
        for _ in range(tries):
            try:
                tx = await self.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0}])
                if tx:
                    return tx
            except Exception as e:  # noqa: BLE001
                log.debug("get_tx retry %s: %s", sig[:8], e)
            await asyncio.sleep(0.5)
        return None

    async def token_balance(self, owner: str, mint: str) -> int:
        res = await self.call("getTokenAccountsByOwner", [
            owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                   for a in res["value"])

    async def token_balances(self, owner: str) -> dict[str, int]:
        """Все токены кошелька (mint -> количество в минимальных единицах), обе программы токенов."""
        out: dict[str, int] = {}
        for prog in TOKEN_PROGRAMS:
            res = await self.call("getTokenAccountsByOwner", [
                owner, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for a in res["value"]:
                info = a["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
                if amt > 0:
                    out[info["mint"]] = out.get(info["mint"], 0) + amt
        return out

    async def sol_balance(self, owner: str) -> float:
        res = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return res["value"] / LAMPORTS
